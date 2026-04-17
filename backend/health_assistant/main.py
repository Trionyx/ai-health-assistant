from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import os
import logging
import threading
import time
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .evidence import EvidenceProvider
from .mock_data import get_scenario, list_scenarios
from .models import AuthSession, AuthUser, MockScenarioProfile, RunKind, UserProfile
from .storage import SQLiteStore
from .workflow import ProviderGateway, run_workflow, start_guided_checkin, submit_guided_checkin_response


store = SQLiteStore(os.getenv("HA_DB_PATH") or Path(__file__).resolve().parents[2] / "data" / "ha.db")
app = FastAPI(title="Health Assistant API", version="0.1.0")
logger = logging.getLogger("health_assistant.api")

# Minimal console logging. Uvicorn will still control handlers; we just set level + format if unset.
_log_level = os.getenv("HA_LOG_LEVEL", "INFO").upper()
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
logging.getLogger("health_assistant").setLevel(_log_level)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


class CheckInReplyRequest(BaseModel):
    response: str


class AuthRequest(BaseModel):
    email: str


class ScenarioSelectRequest(BaseModel):
    scenario_id: str


class SimulatedDateAdvanceRequest(BaseModel):
    days: int = 1


SESSION_COOKIE = "ha_session"
RUN_JOBS: dict[str, dict] = {}
RUN_JOBS_LOCK = threading.Lock()


def _scenario_payload(scenario, profile: MockScenarioProfile | None = None) -> dict:
    today = date.today()
    start = profile.scenario_start_date if profile else (today - timedelta(days=4)).isoformat()
    current = profile.current_simulated_date if profile else today.isoformat()
    return {
        "scenario_id": scenario.scenario_id,
        "title": scenario.title,
        "problem_case": scenario.problem_case,
        "description": scenario.description,
        "expected_agent_reactions": [{"text": item.text, "tone": item.tone} for item in scenario.expected_agent_reactions],
        "scenario_start_date": start,
        "current_simulated_date": current,
        "visible_window_start": start,
        "visible_window_end": current,
    }


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Invalid email format.")
    return email


def _resolve_user_id(request: Request, header_value: str | None) -> str:
    if header_value and header_value.strip():
        return header_value.strip()
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        session = store.get_auth_session(session_id)
        if session:
            return session.user_id
    return "default"


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid4())
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/meta")
def meta() -> dict[str, str | bool]:
    gateway = ProviderGateway()
    evidence = EvidenceProvider()
    return {
        "llm_provider": "openrouter",
        "llm_model": gateway.model,
        "llm_configured": gateway.configured(),
        "evidence_enabled": evidence.config.enabled,
        "evidence_configured": evidence.is_configured(),
        "evidence_transport": evidence.config.transport,
    }


@app.get("/mock/scenarios")
def get_mock_scenarios() -> list[dict]:
    return [_scenario_payload(scenario) for scenario in list_scenarios()]


@app.get("/mock/scenarios/current")
def get_current_mock_scenario(request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    profile = store.get_mock_scenario_profile(user_id)
    scenario = get_scenario(profile.scenario_id if profile else "baseline")
    return _scenario_payload(scenario, profile)


@app.post("/mock/scenarios/select")
def select_mock_scenario(payload: ScenarioSelectRequest, request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    scenario = get_scenario(payload.scenario_id)
    today = date.today()
    start = (today - timedelta(days=4)).isoformat()
    today_iso = today.isoformat()
    profile = store.save_mock_scenario_profile(
        MockScenarioProfile(
            user_id=user_id,
            scenario_id=scenario.scenario_id,
            scenario_start_date=start,
            current_simulated_date=today_iso,
        )
    )
    return _scenario_payload(scenario, profile)


@app.post("/mock/simulated-date/advance")
def advance_mock_simulated_date(
    payload: SimulatedDateAdvanceRequest,
    request: Request,
    x_user_id: str | None = Header(default=None),
) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    profile = store.get_mock_scenario_profile(user_id) or MockScenarioProfile(
        user_id=user_id,
        scenario_start_date=(date.today() - timedelta(days=4)).isoformat(),
        current_simulated_date=date.today().isoformat(),
    )
    current = date.fromisoformat(profile.current_simulated_date)
    next_day = (current + timedelta(days=max(payload.days, 1))).isoformat()
    updated = store.save_mock_scenario_profile(
        profile.model_copy(update={"current_simulated_date": next_day})
    )
    scenario = get_scenario(updated.scenario_id)
    return _scenario_payload(scenario, updated)


@app.post("/runs/daily")
def run_daily(request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    if store.get_user_profile(user_id) is None:
        store.save_user_profile(UserProfile(user_id=user_id, display_name=user_id))
    result = run_workflow(RunKind.DAILY, store, user_id=user_id)
    return {
        "run_id": result.trace.run_id,
        "conversation_id": result.conversation.conversation_id,
        "report": result.report.model_dump(mode="json"),
        "trace": result.trace.model_dump(mode="json"),
        "memory": [item.model_dump(mode="json") for item in result.memory_items],
        "error": result.execution_error,
        "used_fallback": result.used_fallback,
    }


@app.post("/runs/weekly")
def run_weekly(request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    if store.get_user_profile(user_id) is None:
        store.save_user_profile(UserProfile(user_id=user_id, display_name=user_id))
    result = run_workflow(RunKind.WEEKLY, store, user_id=user_id)
    return {
        "run_id": result.trace.run_id,
        "conversation_id": result.conversation.conversation_id,
        "report": result.report.model_dump(mode="json"),
        "trace": result.trace.model_dump(mode="json"),
        "memory": [item.model_dump(mode="json") for item in result.memory_items],
        "error": result.execution_error,
        "used_fallback": result.used_fallback,
    }


@app.post("/runs/{kind}/start")
def run_start(kind: RunKind, request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    if store.get_user_profile(user_id) is None:
        store.save_user_profile(UserProfile(user_id=user_id, display_name=user_id))
    run_id = str(uuid4())
    started_at = time.time()
    with RUN_JOBS_LOCK:
        RUN_JOBS[run_id] = {
            "status": "running",
            "current_node": "start",
            "current_node_started_at": started_at,
            "started_at": started_at,
            "events": [],
            "result": None,
            "error": None,
        }

    def on_event(step, current_node: str) -> None:
        with RUN_JOBS_LOCK:
            job = RUN_JOBS.get(run_id)
            if not job:
                return
            if job.get("current_node") != current_node:
                job["current_node"] = current_node
                job["current_node_started_at"] = time.time()
            job["events"].append(step.model_dump(mode="json"))

    def worker() -> None:
        try:
            result = run_workflow(kind, store, user_id=user_id, event_callback=on_event)
            payload = {
                "run_id": result.trace.run_id,
                "conversation_id": result.conversation.conversation_id,
                "report": result.report.model_dump(mode="json"),
                "trace": result.trace.model_dump(mode="json"),
                "memory": [item.model_dump(mode="json") for item in result.memory_items],
                "error": result.execution_error,
                "used_fallback": result.used_fallback,
            }
            with RUN_JOBS_LOCK:
                job = RUN_JOBS.get(run_id)
                if job:
                    job["status"] = "completed"
                    job["current_node"] = "completed"
                    job["result"] = payload
        except Exception as exc:  # pragma: no cover
            logger.exception(
                "Async run worker failed. run_id=%s user_id=%s kind=%s error=%s",
                run_id,
                user_id,
                kind.value,
                repr(exc),
            )
            with RUN_JOBS_LOCK:
                job = RUN_JOBS.get(run_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = f"{exc.__class__.__name__}: {exc}"

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": run_id}


@app.get("/runs/{run_id}/events")
def run_events(run_id: str) -> dict:
    with RUN_JOBS_LOCK:
        job = RUN_JOBS.get(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        now = time.time()
        current_started = float(job.get("current_node_started_at") or job.get("started_at") or now)
        started_at = float(job.get("started_at") or now)
        return {
            "run_id": run_id,
            "status": job["status"],
            "current_node": job["current_node"],
            "current_node_elapsed_ms": int(max((now - current_started) * 1000, 0)),
            "running_for_ms": int(max((now - started_at) * 1000, 0)),
            "events": job["events"],
            "result": job["result"],
            "error": job["error"],
        }


@app.get("/reports/latest")
def get_latest_report(request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    report = store.get_latest_report(_resolve_user_id(request, x_user_id))
    if report is None:
        raise HTTPException(status_code=404, detail="No reports yet.")
    return report.model_dump(mode="json")


@app.get("/reports/{report_id}")
def get_report(report_id: str) -> dict:
    report = store.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found.")
    return report.model_dump(mode="json")


@app.get("/tasks")
def get_tasks() -> list[dict]:
    return [task.model_dump(mode="json") for task in store.get_tasks()]


@app.get("/memory")
def get_memory(request: Request, x_user_id: str | None = Header(default=None)) -> list[dict]:
    return [item.model_dump(mode="json") for item in store.get_memory_items(_resolve_user_id(request, x_user_id))]


@app.post("/checkins/start")
def checkin_start(request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    if store.get_user_profile(user_id) is None:
        store.save_user_profile(UserProfile(user_id=user_id, display_name=user_id))
    session, conversation, question = start_guided_checkin(store, user_id=user_id)
    return {
        "session_id": session.session_id,
        "conversation_id": conversation.conversation_id,
        "question": question,
    }


@app.get("/checkins/last")
def get_last_checkin(request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(request, x_user_id)
    # Prefer the latest *guided check-in* completion time.
    latest_checkin = store.get_latest_completed_checkin(user_id)
    # Fallback: if user never completed a guided check-in, use last daily synthesis time.
    latest_snapshot = store.get_recent_snapshots(limit=1, kind=RunKind.DAILY.value, user_id=user_id)
    if not latest_checkin and not latest_snapshot:
        return {"hours_since_last": None}

    now = datetime.now(timezone.utc)

    if latest_checkin and latest_checkin.completed_at is not None:
        completed_at = latest_checkin.completed_at
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        hours = (now - completed_at).total_seconds() / 3600
    else:
        created_at = latest_snapshot[0].created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        hours = (now - created_at).total_seconds() / 3600

    return {"hours_since_last": max(round(hours, 1), 0.0)}


@app.post("/checkins/{session_id}/reply")
def checkin_reply(session_id: str, payload: CheckInReplyRequest) -> dict:
    try:
        session, conversation, next_question, result = submit_guided_checkin_response(store, session_id, payload.response)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    response: dict = {
        "session_id": session.session_id,
        "conversation_id": conversation.conversation_id,
        "status": session.status.value,
        "next_question": next_question,
    }
    if result is not None:
        response["run"] = {
            "run_id": result.trace.run_id,
            "report": result.report.model_dump(mode="json"),
            "trace": result.trace.model_dump(mode="json"),
            "memory": [item.model_dump(mode="json") for item in result.memory_items],
            "error": result.execution_error,
            "used_fallback": result.used_fallback,
        }
    return response


@app.get("/trace/latest")
def get_latest_trace(request: Request, x_user_id: str | None = Header(default=None)) -> dict:
    trace = store.get_latest_trace(_resolve_user_id(request, x_user_id))
    if trace is None:
        raise HTTPException(status_code=404, detail="No trace yet.")
    return trace.model_dump(mode="json")


@app.get("/trace/{run_id}")
def get_trace(run_id: str) -> dict:
    trace = store.get_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found.")
    return trace.model_dump(mode="json")


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.model_dump(mode="json")


@app.get("/users/{user_id}")
def get_user_profile(user_id: str) -> dict:
    profile = store.get_user_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="User profile not found.")
    return profile.model_dump(mode="json")


@app.post("/auth/register")
def auth_register(payload: AuthRequest, response: Response) -> dict:
    email = _normalize_email(payload.email)
    existing = store.get_auth_user_by_email(email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email already registered.")
    user = store.save_auth_user(AuthUser(email=email))
    store.save_user_profile(UserProfile(user_id=user.user_id, display_name=email, email=email))
    session = store.save_auth_session(AuthSession(session_id=secrets.token_urlsafe(32), user_id=user.user_id))
    response.set_cookie(SESSION_COOKIE, session.session_id, httponly=True, samesite="lax", path="/")
    return {"user_id": user.user_id, "email": user.email}


@app.post("/auth/login")
def auth_login(payload: AuthRequest, response: Response) -> dict:
    email = _normalize_email(payload.email)
    user = store.get_auth_user_by_email(email)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    session = store.save_auth_session(AuthSession(session_id=secrets.token_urlsafe(32), user_id=user.user_id))
    response.set_cookie(SESSION_COOKIE, session.session_id, httponly=True, samesite="lax", path="/")
    return {"user_id": user.user_id, "email": user.email}


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        store.delete_auth_session(session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    session = store.get_auth_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid session.")
    profile = store.get_user_profile(session.user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="User profile not found.")
    return {"user_id": profile.user_id, "email": profile.email, "display_name": profile.display_name}
