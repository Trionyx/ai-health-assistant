from __future__ import annotations

import asyncio
import json
import os
import logging
import re
from functools import lru_cache
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel

from .models import (
    ConfidenceLevel,
    GPReportDraft,
    GPReportOutput,
    HealthSnapshot,
    RunKind,
    SpecialistAgentOutput,
    TaskDraft,
    TaskItem,
)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
logger = logging.getLogger("health_assistant.agents")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def _build_openrouter_model() -> OpenAIModel:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured.")

    return OpenAIModel(
        model_name=os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini"),
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=api_key,
    )


def _prompt_excerpt(prompt: str, limit: int = 1200) -> str:
    text = (prompt or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


def _run_agent(agent: Agent, prompt: str, *, agent_label: str) -> any:
    try:
        # Prefer a dedicated loop for this thread so uvloop/pydantic-ai has a "current" loop.
        try:
            asyncio.get_running_loop()
            # If we already have a running loop (unlikely in this app), fall back to sync API.
            return agent.run_sync(prompt)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(agent.run(prompt))
            finally:
                try:
                    loop.close()
                finally:
                    asyncio.set_event_loop(None)
    except Exception as exc:
        # Includes: result validation failures / retry exhaustion / provider errors.
        logger.exception(
            "LLM agent execution failed. agent=%s model=%s error=%s prompt_excerpt=%s",
            agent_label,
            getattr(getattr(agent, "model", None), "model_name", None),
            repr(exc),
            _prompt_excerpt(prompt),
        )
        raise


def _quality_signal_payload(signal) -> dict:
    freshness = signal.freshness.value
    confidence = signal.confidence.value
    return {
        "trend": signal.trend.value,
        "hours_since_last": signal.hours_since_last,
        "data_points": signal.data_points,
        "data_recent": signal.hours_since_last <= 24,
        "data_complete": signal.data_points >= 7,
        "sleep": {
            "total_sleep_hours": signal.total_sleep_hours,
            "sleep_baseline_hours": signal.sleep_baseline_hours,
            "sleep_regularity_score": signal.sleep_regularity_score,
        },
        "activity": {
            "steps": signal.steps,
            "steps_baseline": signal.steps_baseline,
            "activity_minutes": signal.activity_minutes,
        },
        "recovery": {
            "hrv": signal.hrv,
            "hrv_baseline": signal.hrv_baseline,
            "resting_hr": signal.resting_hr,
            "resting_hr_baseline": signal.resting_hr_baseline,
        },
        "subjective": {
            "energy": signal.energy,
            "stress": signal.stress,
            "soreness": signal.soreness,
        },
        "deltas": signal.values,
        "certainty": {
            "freshness": freshness,
            "confidence": confidence,
        },
    }


def _snapshot_quality_payload(snapshot: HealthSnapshot) -> dict:
    return {
        "simulated_date": snapshot.simulated_date,
        "visible_window_start": snapshot.visible_window_start,
        "visible_window_end": snapshot.visible_window_end,
        "visible_days_count": snapshot.visible_days_count,
        "sleep_data_recent": snapshot.sleep.hours_since_last <= 24,
        "sleep_data_complete": snapshot.sleep.data_points >= 7,
        "activity_data_recent": snapshot.activity.hours_since_last <= 24,
        "activity_data_complete": snapshot.activity.data_points >= 7,
        "recovery_data_recent": snapshot.recovery.hours_since_last <= 24,
        "recovery_data_complete": snapshot.recovery.data_points >= 7,
    }


def _sentence_split(text: str) -> list[str]:
    parts = [item.strip() for item in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if item.strip()]
    return parts


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_near_duplicate(a: str, b: str) -> bool:
    sa = set(_normalize_text(a).split())
    sb = set(_normalize_text(b).split())
    if not sa or not sb:
        return False
    overlap = len(sa & sb) / max(min(len(sa), len(sb)), 1)
    return overlap >= 0.75


def _unique_lines(lines: list[str], against: list[str] | None = None) -> list[str]:
    kept: list[str] = []
    pool = [line for line in (against or []) if line]
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if any(_is_near_duplicate(value, existing) for existing in (kept + pool)):
            continue
        kept.append(value)
    return kept


def _dominant_pattern(snapshot: HealthSnapshot) -> str:
    sleep_delta = (snapshot.sleep.total_sleep_hours or 0.0) - (snapshot.sleep.sleep_baseline_hours or 0.0)
    hrv_delta = float(snapshot.recovery.values.get("hrv_vs_baseline", 0) if snapshot.recovery.values else 0)
    resting_hr_delta = float(snapshot.recovery.values.get("resting_hr_vs_baseline", 0) if snapshot.recovery.values else 0)
    stress_level = int(snapshot.recovery.stress or snapshot.activity.stress or 0)
    if sleep_delta <= -1.0:
        return "sleep_debt"
    if stress_level >= 7 or hrv_delta <= -8 or resting_hr_delta >= 4:
        return "overload"
    return "steady_mixed"


def _evidence_sentence(snapshot: HealthSnapshot, pattern: str) -> str:
    sleep_delta = (snapshot.sleep.total_sleep_hours or 0.0) - (snapshot.sleep.sleep_baseline_hours or 0.0)
    hrv_delta = float(snapshot.recovery.values.get("hrv_vs_baseline", 0) if snapshot.recovery.values else 0)
    stress_level = int(snapshot.recovery.stress or snapshot.activity.stress or 0)
    if pattern == "sleep_debt":
        return f"Sleep is about {abs(sleep_delta):.1f}h below your baseline, so recovery should lead today."
    if pattern == "overload":
        return f"Stress is elevated ({stress_level}/10) with recovery strain signals, so pacing matters most."
    return "Core signals are mostly steady, with mild caution where recovery confidence is not fully strong."


def _summary_with_cap(summary: str, pattern: str, snapshot: HealthSnapshot) -> str:
    sentences = _sentence_split(summary)
    if not sentences:
        sentences = ["Today's picture is mixed but manageable."]
    if pattern == "sleep_debt":
        preface = "Today needs a recovery-first focus."
    elif pattern == "overload":
        preface = "Today looks pressured, so keep pacing conservative."
    else:
        preface = "Today is mostly steady, with mild caution."
    if not any(_is_near_duplicate(preface, item) for item in sentences):
        sentences.insert(0, preface)
    evidence = _evidence_sentence(snapshot, pattern)
    if not any(_is_near_duplicate(evidence, item) for item in sentences):
        sentences.append(evidence)
    # Keep concise: 2-4 sentences max.
    sentences = _unique_lines(sentences)
    if len(sentences) < 2:
        sentences.append(evidence)
    return " ".join(sentences[:4]).strip()


def _task_score(task: TaskItem, pattern: str) -> int:
    text = _normalize_text(f"{task.title} {task.description}")
    if pattern == "sleep_debt":
        if any(k in text for k in ["sleep", "bedtime", "rest"]):
            return 100
        if any(k in text for k in ["light", "de load", "decompression", "low strain"]):
            return 80
    if pattern == "overload":
        if any(k in text for k in ["decompression", "lighter", "low strain", "reduce", "pacing"]):
            return 100
        if any(k in text for k in ["sleep", "bedtime", "recovery"]):
            return 80
    if any(k in text for k in ["monitor", "check in", "track"]):
        return 40
    return 60


def _infer_evidence_metadata(agent_name: str, snapshot: HealthSnapshot) -> tuple[str | None, str | None, str | None]:
    sleep_delta = (snapshot.sleep.total_sleep_hours or 0.0) - (snapshot.sleep.sleep_baseline_hours or 0.0)
    hrv_delta = float(snapshot.recovery.values.get("hrv_vs_baseline", 0) if snapshot.recovery.values else 0)
    stress_level = int(snapshot.recovery.stress or snapshot.activity.stress or 0)
    if sleep_delta <= -1.0 and agent_name == "sleep":
        return (
            "sleep_deficit",
            "Sleep deficit and recovery pressure",
            "Sleep remains below baseline and may be driving reduced resilience.",
        )
    if (stress_level >= 7 or hrv_delta <= -8) and agent_name in {"recovery", "activity"}:
        return (
            "recovery_strain" if agent_name == "recovery" else "overload_pattern",
            "Recovery strain and overload pattern",
            "Stress and recovery markers suggest accumulated strain over recent days.",
        )
    if stress_level >= 6:
        return (
            "stress_fatigue",
            "Stress-fatigue interaction",
            "Stress load may be amplifying fatigue signals and slowing recovery.",
        )
    return (None, None, None)


def apply_report_policies(
    snapshot: HealthSnapshot,
    report: GPReportOutput,
    specialist_outputs: list[SpecialistAgentOutput],
) -> GPReportOutput:
    pattern = _dominant_pattern(snapshot)
    status = "steady"
    if pattern == "sleep_debt":
        status = "recovery_focus"
    elif pattern == "overload":
        status = "overload"
    elif report.overall_confidence == ConfidenceLevel.LOW:
        status = "caution"

    top_priority = {
        "sleep_debt": "Prioritize sleep recovery tonight.",
        "overload": "Treat today as a decompression day.",
        "steady_mixed": "Keep today lighter than usual.",
    }[pattern]

    task_candidates = list(report.tasks)
    task_candidates.sort(key=lambda task: _task_score(task, pattern), reverse=True)
    ranked_tasks: list[TaskItem] = []
    seen_task: list[str] = []
    for index, task in enumerate(task_candidates):
        title = task.title.strip()
        if not title or any(_is_near_duplicate(title, existing) for existing in seen_task):
            continue
        seen_task.append(title)
        ranked_tasks.append(task.model_copy(update={"is_recommended_first": index == 0}))
    if ranked_tasks:
        ranked_tasks[0] = ranked_tasks[0].model_copy(update={"is_recommended_first": True})
        for i in range(1, len(ranked_tasks)):
            ranked_tasks[i] = ranked_tasks[i].model_copy(update={"is_recommended_first": False})

    summary = report.daily_brief if snapshot.kind == RunKind.DAILY else report.weekly_report
    polished_summary = _summary_with_cap(summary or "", pattern, snapshot)
    task_titles = [task.title for task in ranked_tasks]
    problem_list = _unique_lines(report.problem_list, against=[polished_summary, *task_titles])[:4]
    uncertainty_notes = _unique_lines(report.uncertainty_notes)[:4]
    next_steps = _unique_lines(report.next_steps, against=task_titles)[:3]
    priority_flags = _unique_lines(report.priority_flags)

    return report.model_copy(
        update={
            "daily_brief": polished_summary if snapshot.kind == RunKind.DAILY else None,
            "weekly_report": polished_summary if snapshot.kind == RunKind.WEEKLY else None,
            "top_priority": top_priority,
            "today_status": status,
            "problem_list": problem_list,
            "tasks": ranked_tasks[:3],
            "uncertainty_notes": uncertainty_notes,
            "next_steps": next_steps,
            "priority_flags": priority_flags,
        }
    )


@lru_cache(maxsize=1)
def _sleep_agent() -> Agent[None, SpecialistAgentOutput]:
    return Agent(
        _build_openrouter_model(),
        result_type=SpecialistAgentOutput,
        system_prompt=load_prompt("sleep"),
        name="sleep_specialist",
        retries=4,
    )


@lru_cache(maxsize=1)
def _activity_agent() -> Agent[None, SpecialistAgentOutput]:
    return Agent(
        _build_openrouter_model(),
        result_type=SpecialistAgentOutput,
        system_prompt=load_prompt("activity"),
        name="activity_specialist",
        retries=4,
    )


@lru_cache(maxsize=1)
def _recovery_agent() -> Agent[None, SpecialistAgentOutput]:
    return Agent(
        _build_openrouter_model(),
        result_type=SpecialistAgentOutput,
        system_prompt=load_prompt("recovery"),
        name="recovery_specialist",
        retries=4,
    )


@lru_cache(maxsize=1)
def _gp_agent() -> Agent[None, GPReportDraft]:
    return Agent(
        _build_openrouter_model(),
        result_type=GPReportDraft,
        system_prompt=load_prompt("gp"),
        name="gp_coordinator",
        retries=4,
    )


def _specialist_prompt(agent_name: str, snapshot: HealthSnapshot, signal, memory_context: list[str]) -> str:
    return f"""
Run kind: {snapshot.kind.value}
Window: {snapshot.window_label}
Agent: {agent_name}

Signal payload:
{json.dumps(_quality_signal_payload(signal), indent=2)}

Snapshot quality signals:
{json.dumps(
    _snapshot_quality_payload(snapshot),
    indent=2,
)}

Instructions:
- Analyze only the assigned domain.
- Base interpretation on current values vs baseline and trend.
- Return human-meaningful structured findings including finding, severity, trend, supporting_signals, and interpretive_summary.
- Data-quality can soften certainty, but it should not replace interpretation.
- Keep output actionable but cautious.
- Include this historical context when relevant:
{json.dumps(memory_context, indent=2)}
""".strip()


def _gp_prompt(snapshot: HealthSnapshot, specialist_outputs: list[SpecialistAgentOutput], memory_context: list[str]) -> str:
    specialist_payload = [output.model_dump(mode="json") for output in specialist_outputs]
    return f"""
Run kind: {snapshot.kind.value}
Window: {snapshot.window_label}
Snapshot id: {snapshot.snapshot_id}

Snapshot summary:
{json.dumps(
    {
        "source": snapshot.source,
        "quality_signals": _snapshot_quality_payload(snapshot),
        "sleep": _quality_signal_payload(snapshot.sleep),
        "activity": _quality_signal_payload(snapshot.activity),
        "recovery": _quality_signal_payload(snapshot.recovery),
    },
    indent=2,
)}

Specialist outputs:
{json.dumps(specialist_payload, indent=2)}

Instructions:
- Synthesize across all specialist outputs.
- Treat specialist outputs as the primary source for interpretation and prioritization.
- If the run is daily, populate `daily_brief`; if weekly, populate `weekly_report`.
- In `daily_brief` / `weekly_report`, focus on: what matters now, likely meaning/pattern, and what the user should keep in mind.
- Keep uncertainty concise when needed; do not let data-availability language dominate the summary.
- Avoid technical/monitoring phrasing (for example: "inputs are recent and complete", "well-positioned for monitoring", "snapshot includes data quality/availability", "we can’t comment yet").
- Keep concrete action items brief in summary text; reserve the main action detail for the separate tasks block.
- Generate tasks that respond to scenario meaning (load reduction, decompression, sleep restoration, recovery-first pacing) before data-collection tasks.
- Consider this memory context for continuity:
{json.dumps(memory_context, indent=2)}
""".strip()


def run_specialist_agent(agent_name: str, snapshot: HealthSnapshot, memory_context: list[str] | None = None) -> SpecialistAgentOutput:
    signal = getattr(snapshot, agent_name)
    prompt = _specialist_prompt(agent_name, snapshot, signal, memory_context or [])
    runners = {
        "sleep": _sleep_agent,
        "activity": _activity_agent,
        "recovery": _recovery_agent,
    }
    result = _run_agent(runners[agent_name](), prompt, agent_label=f"specialist:{agent_name}")
    evidence_query_key, evidence_topic, evidence_reason = _infer_evidence_metadata(agent_name, snapshot)
    return result.data.model_copy(
        update={
            "agent_name": agent_name,
            "confidence": signal.confidence,
            "used_data_points": signal.data_points,
            "trend": signal.trend,
            "evidence_query_key": evidence_query_key,
            "evidence_topic": evidence_topic,
            "evidence_relevance_reason": evidence_reason,
        }
    )


def run_gp_agent(
    snapshot: HealthSnapshot,
    specialist_outputs: list[SpecialistAgentOutput],
    memory_context: list[str] | None = None,
) -> GPReportOutput:
    prompt = _gp_prompt(snapshot, specialist_outputs, memory_context or [])
    draft = _run_agent(_gp_agent(), prompt, agent_label="gp").data
    tasks = [TaskItem(title=task.title, description=task.description) for task in draft.tasks]
    report = GPReportOutput(
        user_id=snapshot.user_id,
        snapshot_id=snapshot.snapshot_id,
        kind=snapshot.kind,
        problem_list=draft.problem_list,
        daily_brief=draft.daily_brief if snapshot.kind == RunKind.DAILY else None,
        weekly_report=draft.weekly_report if snapshot.kind == RunKind.WEEKLY else None,
        priority_flags=draft.priority_flags,
        next_steps=draft.next_steps,
        tasks=tasks,
        overall_confidence=draft.overall_confidence,
        uncertainty_notes=draft.uncertainty_notes,
        safety_notes=draft.safety_notes,
    )
    return apply_report_policies(snapshot, report, specialist_outputs)
