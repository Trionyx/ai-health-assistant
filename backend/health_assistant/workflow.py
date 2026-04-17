from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import logging
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import apply_report_policies, run_gp_agent, run_specialist_agent
from .evidence import EvidenceProvider
from .mock_data import get_simulated_today, load_mock_snapshot
from .models import (
    CheckInSession,
    CheckInSessionStatus,
    ConfidenceLevel,
    Conversation,
    ConversationMessage,
    GPReportOutput,
    HealthSnapshot,
    MemoryItem,
    RunKind,
    SeverityLevel,
    SpecialistAgentOutput,
    SubjectiveSignals,
    TrendDirection,
    WorkflowTrace,
    WorkflowTraceStep,
)
from .storage import SQLiteStore

logger = logging.getLogger("health_assistant.workflow")

DISCLAIMER = (
    "This assistant supports reflection and wellness planning only. It does not provide medical "
    "diagnosis or treatment advice. For urgent or clinical concerns, consult a qualified clinician."
)


@dataclass
class WorkflowResult:
    snapshot: HealthSnapshot
    conversation: Conversation
    specialist_outputs: list[SpecialistAgentOutput]
    report: GPReportOutput
    memory_items: list[MemoryItem]
    trace: WorkflowTrace
    execution_error: str | None = None
    used_fallback: bool = False


class CheckInState(TypedDict):
    current_step: int
    user_response: str
    subjective: SubjectiveSignals
    needs_clarification: bool
    next_question: str
    done: bool


class ProviderGateway:
    def __init__(self) -> None:
        self.base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        self.api_key = os.getenv("OPENROUTER_API_KEY", "")

    def model_label(self) -> str:
        return f"openrouter:{self.model}"

    def configured(self) -> bool:
        return bool(self.api_key.strip())


def _router(snapshot: HealthSnapshot) -> list[str]:
    candidates: list[tuple[str, object]] = [
        ("sleep", snapshot.sleep),
        ("activity", snapshot.activity),
        ("recovery", snapshot.recovery),
    ]
    chosen = [name for name, signal in candidates if signal.freshness.value != "stale"]
    if snapshot.kind == RunKind.WEEKLY and snapshot.summary_confidence == ConfidenceLevel.HIGH:
        chosen = [name for name in chosen if name != "recovery"] or chosen
    return chosen or [name for name, _ in candidates]


def _questions() -> list[str]:
    return [
        "How is your mood today on a 1-10 scale?",
        "How stressed do you feel right now on a 1-10 scale?",
        "How is your energy level on a 1-10 scale?",
        "How would you describe your sleep quality last night in a short phrase?",
        "Any symptoms or discomfort to track today? You can list comma separated items or say 'none'.",
    ]


def _int_or_none(raw: str) -> int | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    value = int(digits)
    if 1 <= value <= 10:
        return value
    return None


def _parse_checkin_step(step: int, response: str, subjective: SubjectiveSignals) -> tuple[SubjectiveSignals, bool]:
    updated = subjective.model_copy(deep=True)
    normalized = response.strip()
    if step == 0:
        value = _int_or_none(normalized)
        if value is None:
            return updated, True
        updated.mood_score = value
        return updated, False
    if step == 1:
        value = _int_or_none(normalized)
        if value is None:
            return updated, True
        updated.stress_score = value
        return updated, False
    if step == 2:
        value = _int_or_none(normalized)
        if value is None:
            return updated, True
        updated.energy_score = value
        return updated, False
    if step == 3:
        if len(normalized) < 3:
            return updated, True
        updated.sleep_quality = normalized
        return updated, False
    if step == 4:
        if not normalized:
            return updated, True
        if normalized.lower() in {"none", "no", "nothing"}:
            updated.symptom_flags = []
        else:
            updated.symptom_flags = [part.strip() for part in normalized.split(",") if part.strip()]
        return updated, False
    return updated, False


def _checkin_parse_node(state: CheckInState) -> CheckInState:
    subjective, needs_clarification = _parse_checkin_step(state["current_step"], state["user_response"], state["subjective"])
    return {**state, "subjective": subjective, "needs_clarification": needs_clarification}


def _checkin_route_node(state: CheckInState) -> CheckInState:
    if state["needs_clarification"]:
        prompt = f"Please clarify. {_questions()[state['current_step']]}"
        return {**state, "next_question": prompt, "done": False}
    next_step = state["current_step"] + 1
    if next_step >= len(_questions()):
        summary = (
            f"Mood {state['subjective'].mood_score}/10, stress {state['subjective'].stress_score}/10, "
            f"energy {state['subjective'].energy_score}/10. Sleep: {state['subjective'].sleep_quality}."
        )
        updated = state["subjective"].model_copy(update={"summary": summary})
        return {**state, "current_step": next_step, "subjective": updated, "next_question": "", "done": True}
    return {**state, "current_step": next_step, "next_question": _questions()[next_step], "done": False}


def _build_checkin_graph():
    graph = StateGraph(CheckInState)
    graph.add_node("parse", _checkin_parse_node)
    graph.add_node("route", _checkin_route_node)
    graph.add_edge(START, "parse")
    graph.add_edge("parse", "route")
    graph.add_edge("route", END)
    return graph.compile()


CHECKIN_GRAPH = _build_checkin_graph()


def _make_agent_output_fallback(agent_name: str, signal, run_kind: RunKind) -> SpecialistAgentOutput:
    uncertainty: list[str] = []
    flags: list[str] = []
    recommendations: list[str] = []

    if signal.freshness.value == "stale":
        uncertainty.append(f"{agent_name.title()} data is stale.")
        flags.append("stale-data")
    if signal.confidence.value == "low":
        uncertainty.append(f"{agent_name.title()} confidence is low due to sparse samples.")
        flags.append("low-confidence")

    if run_kind == RunKind.DAILY:
        recommendations.append(f"Review today's {agent_name} trend with caution.")
    else:
        recommendations.append(f"Use the weekly {agent_name} trend to guide small adjustments.")

    return SpecialistAgentOutput(
        agent_name=agent_name,
        summary=f"{agent_name.title()} review completed for {run_kind.value} workflow.",
        finding=(
            f"{agent_name.title()} is below baseline and needs a cautious approach."
            if signal.confidence.value != "high"
            else f"{agent_name.title()} is near baseline with acceptable stability."
        ),
        severity=SeverityLevel.MODERATE if signal.confidence.value != "high" else SeverityLevel.LOW,
        trend=signal.trend if hasattr(signal, "trend") else TrendDirection.STABLE,
        supporting_signals={
            "hrv_vs_baseline": signal.values.get("hrv_vs_baseline", 0) if hasattr(signal, "values") else 0,
            "resting_hr_vs_baseline": signal.values.get("resting_hr_vs_baseline", 0) if hasattr(signal, "values") else 0,
        },
        interpretive_summary=(
            f"{agent_name.title()} trend appears {signal.trend.value if hasattr(signal, 'trend') else 'stable'} "
            f"with confidence {signal.confidence.value}."
        ),
        findings=[
            f"{agent_name.title()} trend is {signal.trend.value if hasattr(signal, 'trend') else 'stable'}.",
            (
                f"HRV vs baseline: {signal.values.get('hrv_vs_baseline', 0)}"
                if hasattr(signal, "values")
                else f"{agent_name.title()} confidence is {signal.confidence.value}."
            ),
        ],
        flags=flags,
        recommendations_draft=recommendations,
        confidence=signal.confidence,
        uncertainty_notes=uncertainty,
        used_data_points=signal.data_points,
    )


def _overall_confidence(outputs: list[SpecialistAgentOutput]) -> ConfidenceLevel:
    confidences = [output.confidence for output in outputs]
    if ConfidenceLevel.LOW in confidences:
        return ConfidenceLevel.LOW
    if ConfidenceLevel.MEDIUM in confidences:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.HIGH


def _make_report_fallback(snapshot: HealthSnapshot, outputs: list[SpecialistAgentOutput]):
    overall_confidence = _overall_confidence(outputs)
    uncertainty_notes = [note for output in outputs for note in output.uncertainty_notes]
    task_prefix = "Daily" if snapshot.kind == RunKind.DAILY else "Weekly"
    key_priorities = [output.summary for output in outputs[:2]]
    if key_priorities:
        narrative = (
            "Today's picture is mixed. "
            + " ".join(key_priorities)
            + " Keep priorities practical and avoid over-interpreting single data points."
        )
    else:
        narrative = (
            "Today's picture is still taking shape. Focus on steady routines, and treat conclusions as provisional."
        )
    from .models import GPReportOutput, TaskItem

    tasks: list[TaskItem] = []
    sleep_delta = (snapshot.sleep.total_sleep_hours or 0) - (snapshot.sleep.sleep_baseline_hours or 0)
    recovery_strain = (snapshot.recovery.values.get("hrv_vs_baseline", 0) if snapshot.recovery.values else 0)
    resting_hr_delta = (snapshot.recovery.values.get("resting_hr_vs_baseline", 0) if snapshot.recovery.values else 0)
    stress_level = snapshot.recovery.stress or snapshot.activity.stress or 0
    if sleep_delta <= -1.0:
        tasks.append(
            TaskItem(
                title="Prioritize earlier bedtime",
                description="Shift tonight's bedtime earlier and protect a full sleep opportunity.",
            )
        )
        tasks.append(
            TaskItem(
                title="Avoid high-intensity load",
                description="Keep activity light tomorrow morning and reassess fatigue before harder efforts.",
            )
        )
    elif recovery_strain < -8 or resting_hr_delta > 4 or stress_level >= 7:
        tasks.append(
            TaskItem(
                title="Take a lighter activity day",
                description="Reduce training intensity today and prioritize low-strain movement.",
            )
        )
        tasks.append(
            TaskItem(
                title="Add a decompression block",
                description="Schedule one dedicated decompression session (breathing, walk, or quiet reset) today.",
            )
        )
    else:
        tasks.append(
            TaskItem(
                title=f"{task_prefix} recovery check-in",
                description="Track how energy and stress evolve later today to confirm current trajectory.",
            )
        )
        tasks.append(
            TaskItem(
                title=f"{task_prefix} routine review",
                description="Keep routines steady and reassess tomorrow before making bigger changes.",
            )
        )

    report = GPReportOutput(
        user_id=snapshot.user_id,
        snapshot_id=snapshot.snapshot_id,
        kind=snapshot.kind,
        problem_list=[
            f"Sleep signal is {snapshot.sleep.confidence.value}.",
            f"Activity signal is {snapshot.activity.confidence.value}.",
            f"Recovery signal is {snapshot.recovery.confidence.value}.",
            *(["Subjective summary captured from guided check-in."] if snapshot.subjective else []),
        ],
        daily_brief=narrative if snapshot.kind == RunKind.DAILY else None,
        weekly_report=narrative if snapshot.kind == RunKind.WEEKLY else None,
        priority_flags=[
            flag
            for flag in ["stale-data", "low-confidence"]
            if any(flag in output.flags for output in outputs)
        ],
        next_steps=[
            "Focus first on the dominant pattern shown in sleep, activity, and recovery together.",
            "Use certainty limits to calibrate intensity, not to ignore meaningful signals.",
        ],
        tasks=tasks,
        overall_confidence=overall_confidence,
        uncertainty_notes=uncertainty_notes,
        safety_notes=[DISCLAIMER, "Outputs use non-diagnostic language by design."],
    )
    return apply_report_policies(snapshot, report, outputs)


def _extract_memory(
    store: SQLiteStore,
    snapshot: HealthSnapshot,
    report: GPReportOutput,
    specialist_outputs: list[SpecialistAgentOutput],
) -> list[MemoryItem]:
    recent = store.get_recent_snapshots(limit=5, kind=RunKind.DAILY.value, user_id=snapshot.user_id, as_of=snapshot.simulated_date)
    window = list(reversed(recent))
    if not any(item.snapshot_id == snapshot.snapshot_id for item in window):
        window.append(snapshot)
    window = window[-5:]

    insights: list[tuple[str, str]] = []

    sleep_values = [item.sleep.total_sleep_hours for item in window if item.sleep.total_sleep_hours is not None]
    sleep_baselines = [item.sleep.sleep_baseline_hours for item in window if item.sleep.sleep_baseline_hours is not None]
    if sleep_values and sleep_baselines:
        avg_sleep = sum(sleep_values) / len(sleep_values)
        avg_baseline = sum(sleep_baselines) / len(sleep_baselines)
        delta = avg_sleep - avg_baseline
        if delta <= -0.8:
            insights.append(("pattern", f"Over the last {len(window)} days, your sleep is about {abs(delta):.1f}h below baseline on average."))
        elif delta >= 0.4:
            insights.append(("pattern", f"Over the last {len(window)} days, your sleep is slightly above baseline by about {delta:.1f}h."))

    stress_values = [item.recovery.stress or item.activity.stress for item in window if (item.recovery.stress or item.activity.stress) is not None]
    if stress_values:
        avg_stress = sum(stress_values) / len(stress_values)
        if avg_stress >= 7:
            insights.append(("pattern", f"Stress has stayed elevated (about {avg_stress:.1f}/10) across recent days."))
        elif avg_stress <= 4:
            insights.append(("pattern", f"Stress has stayed in a manageable range (about {avg_stress:.1f}/10) recently."))

    hrv_deltas = [float(item.recovery.values.get("hrv_vs_baseline", 0)) for item in window if item.recovery.values]
    if hrv_deltas:
        avg_hrv_delta = sum(hrv_deltas) / len(hrv_deltas)
        if avg_hrv_delta <= -8:
            insights.append(("persistent_fact", "Recovery has been under baseline for several days, so a lighter pacing pattern fits better."))
        elif avg_hrv_delta >= 3:
            insights.append(("persistent_fact", "Recovery signals have been trending near or above baseline in recent days."))

    if report.top_priority:
        insights.append(("preference", f"You respond best to a clear first focus: {report.top_priority}"))

    specialist_clues = [output.interpretive_summary for output in specialist_outputs if output.interpretive_summary]
    if specialist_clues:
        insights.append(("pattern", specialist_clues[0]))

    filtered: list[tuple[str, str]] = []
    blocked_tokens = ["latest report kind", "llm gateway", "workflow initialized", "system", "report id", "snapshot id"]
    for category, content in insights:
        cleaned = " ".join(content.split()).strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if any(token in lowered for token in blocked_tokens):
            continue
        if any(cleaned.lower() == existing.lower() for _, existing in filtered):
            continue
        filtered.append((category, cleaned))
        if len(filtered) >= 5:
            break

    if len(filtered) < 3:
        fallback = [
            ("pattern", "Your recent signals suggest keeping decisions anchored to multi-day trends, not a single day spike."),
            ("persistent_fact", "You benefit from balancing activity plans with recovery signals before increasing load."),
            ("preference", "A single practical priority each day appears easier to follow than many equal tasks."),
        ]
        for category, content in fallback:
            if len(filtered) >= 3:
                break
            if any(content.lower() == existing.lower() for _, existing in filtered):
                continue
            filtered.append((category, content))

    return [
        MemoryItem(
            category=category,  # type: ignore[arg-type]
            content=content,
            source=f"insights:{report.report_id}:user:{report.user_id}",
            confidence=report.overall_confidence,
        )
        for category, content in filtered[:5]
    ]


def _merge_subjective(snapshot: HealthSnapshot, subjective: SubjectiveSignals | None) -> HealthSnapshot:
    if subjective is None:
        return snapshot
    return snapshot.model_copy(update={"subjective": subjective})


def _build_weekly_snapshot(store: SQLiteStore, user_id: str, simulated_date: str, scenario_start_date: str) -> HealthSnapshot:
    recent_daily = store.get_recent_snapshots(limit=7, kind=RunKind.DAILY.value, user_id=user_id, as_of=simulated_date)
    profile = store.get_mock_scenario_profile(user_id)
    scenario_id = profile.scenario_id if profile else "baseline"
    if not recent_daily:
        return load_mock_snapshot(
            RunKind.WEEKLY,
            scenario_id=scenario_id,
            simulated_day=simulated_date,
            scenario_start_date=scenario_start_date,
        ).model_copy(update={"user_id": user_id})
    sleep_points = sum(item.sleep.data_points for item in recent_daily)
    activity_points = sum(item.activity.data_points for item in recent_daily)
    recovery_points = sum(item.recovery.data_points for item in recent_daily)
    weekly = load_mock_snapshot(
        RunKind.WEEKLY,
        scenario_id=scenario_id,
        simulated_day=simulated_date,
        scenario_start_date=scenario_start_date,
    )
    subjective_notes = [item.subjective.summary for item in recent_daily if item.subjective and item.subjective.summary]
    subjective = SubjectiveSignals(summary=" | ".join(subjective_notes[:3])) if subjective_notes else None
    return weekly.model_copy(
        update={
            "user_id": user_id,
            "window_label": f"Visible window through {simulated_date}",
            "sleep": weekly.sleep.model_copy(update={"data_points": max(weekly.sleep.data_points, sleep_points)}),
            "activity": weekly.activity.model_copy(update={"data_points": max(weekly.activity.data_points, activity_points)}),
            "recovery": weekly.recovery.model_copy(update={"data_points": max(weekly.recovery.data_points, recovery_points)}),
            "subjective": subjective,
        }
    )


def _select_weekly_evidence_targets(outputs: list[SpecialistAgentOutput], max_items: int) -> list[SpecialistAgentOutput]:
    severity_weight = {
        SeverityLevel.HIGH: 3,
        SeverityLevel.MODERATE: 2,
        SeverityLevel.LOW: 1,
    }
    ranked = sorted(
        outputs,
        key=lambda item: (
            severity_weight.get(item.severity, 1),
            1 if item.trend == TrendDirection.DECLINING else 0,
            1 if item.evidence_query_key else 0,
        ),
        reverse=True,
    )
    selected: list[SpecialistAgentOutput] = []
    seen: set[str] = set()
    for output in ranked:
        key = output.evidence_query_key
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(output)
        if len(selected) >= max_items:
            break
    return selected


def _format_execution_error(exc: Exception) -> str:
    raw = f"{exc.__class__.__name__}: {exc}"
    lowered = raw.lower()
    if "429" in lowered or "rate" in lowered and "limit" in lowered:
        return (
            "Rate limit reached for the current model/provider (commonly seen on free-tier models). "
            "Wait a short time and retry, switch to a less busy model, or use your own OpenRouter key "
            "to get higher personal rate limits."
        )
    return raw


def _format_specialist_excerpt(output: SpecialistAgentOutput) -> str:
    return json.dumps(
        {
            "agent_name": output.agent_name,
            "summary": output.summary,
            "finding": output.finding,
            "severity": output.severity.value,
            "trend": output.trend.value,
            "evidence_query_key": output.evidence_query_key,
            "evidence_topic": output.evidence_topic,
            "evidence_relevance_reason": output.evidence_relevance_reason,
            "supporting_signals": output.supporting_signals,
            "interpretive_summary": output.interpretive_summary,
            "findings": output.findings,
            "flags": output.flags,
            "recommendations_draft": output.recommendations_draft,
            "confidence": output.confidence.value,
            "uncertainty_notes": output.uncertainty_notes,
            "used_data_points": output.used_data_points,
        },
        indent=2,
    )


def _format_gp_excerpt(report: GPReportOutput) -> str:
    primary_text = report.daily_brief or report.weekly_report
    return json.dumps(
        {
            "primary_text": primary_text or "",
            "problem_list": report.problem_list,
            "evidence_support": [item.model_dump(mode="json") for item in report.evidence_support],
            "priority_flags": report.priority_flags,
            "next_steps": report.next_steps,
            "uncertainty_notes": report.uncertainty_notes,
            "overall_confidence": report.overall_confidence.value,
        },
        indent=2,
    )


def run_workflow(
    kind: RunKind,
    store: SQLiteStore,
    *,
    user_id: str = "default",
    subjective: SubjectiveSignals | None = None,
    conversation: Conversation | None = None,
    event_callback: Callable[[WorkflowTraceStep, str], None] | None = None,
) -> WorkflowResult:
    profile = store.get_mock_scenario_profile(user_id)
    scenario_id = profile.scenario_id if profile else "baseline"
    simulated_date = get_simulated_today(profile.current_simulated_date if profile else None).isoformat()
    scenario_start_date = profile.scenario_start_date if profile else simulated_date
    snapshot = (
        _build_weekly_snapshot(store, user_id, simulated_date, scenario_start_date)
        if kind == RunKind.WEEKLY
        else load_mock_snapshot(
            kind,
            scenario_id=scenario_id,
            simulated_day=simulated_date,
            scenario_start_date=scenario_start_date,
        )
    )
    if snapshot is None:  # defensive guard; keep worker alive even if snapshot provider fails unexpectedly
        snapshot = load_mock_snapshot(
            kind,
            scenario_id=scenario_id,
            simulated_day=simulated_date,
            scenario_start_date=scenario_start_date,
        )
    snapshot = _merge_subjective(snapshot, subjective).model_copy(update={"user_id": user_id})
    provider = ProviderGateway()
    conversation = store.save_conversation(
        (conversation.model_copy(update={"user_id": user_id}) if conversation is not None else conversation)
        or Conversation(
            user_id=user_id,
            messages=[
                ConversationMessage(
                    role="system",
                    content=f"LLM gateway: {provider.model_label()} (configured={provider.configured()})",
                ),
                ConversationMessage(role="assistant", content="Workflow initialized with mock health data."),
            ]
        )
    )
    memory_context = [item.content for item in store.get_memory_items(user_id)[:12]]
    live_steps: list[WorkflowTraceStep] = []
    current_node = "start"
    node_started_at: dict[str, datetime] = {}

    def emit(
        *,
        name: str,
        status: str,
        graph_node: str,
        detail: str,
        warning_reason: str | None = None,
        freshness=None,
        confidence=None,
        llm_excerpt: str | None = None,
    ) -> None:
        nonlocal current_node
        current_node = graph_node
        step = WorkflowTraceStep(
            name=name,
            status=status,  # type: ignore[arg-type]
            graph_node=graph_node,
            detail=detail,
            warning_reason=warning_reason,
            freshness=freshness,
            confidence=confidence,
            llm_excerpt=llm_excerpt,
        )
        if status == "running":
            node_started_at[graph_node] = step.occurred_at
        else:
            started_at = node_started_at.pop(graph_node, None)
            if started_at is not None:
                step.duration_ms = int(max((step.occurred_at - started_at).total_seconds() * 1000, 0))
        live_steps.append(step)
        if event_callback is not None:
            event_callback(step, current_node)

    profile = store.get_user_profile(user_id)
    user_label = (profile.email or profile.display_name or user_id) if profile else user_id
    emit(name="load_context", status="completed", graph_node="load_context", detail=f"Loaded user context for `{user_label}`.")
    emit(
        name="load_mock_data",
        status="completed",
        graph_node="load_data",
        detail=f"Loaded {snapshot.window_label} data source: {snapshot.source}.",
    )
    emit(
        name="freshness_confidence",
        status="warning" if snapshot.summary_confidence == ConfidenceLevel.LOW else "completed",
        graph_node="validate_signals",
        warning_reason=(
            "Some domain signals are low confidence, so interpretation should stay cautious."
            if snapshot.summary_confidence == ConfidenceLevel.LOW
            else None
        ),
        freshness=snapshot.summary_freshness,
        confidence=snapshot.summary_confidence,
        detail=(
            f"Computed freshness={snapshot.summary_freshness.value}, confidence={snapshot.summary_confidence.value} "
            "from sleep/activity/recovery signals."
        ),
    )
    outputs: list[SpecialistAgentOutput] = []
    execution_error: str | None = None
    used_fallback = False
    routed_agents = _router(snapshot)
    emit(
        name="router",
        status="completed",
        graph_node="route_agents",
        detail=f"Router selected agents: {', '.join(routed_agents) or 'none'}.",
    )
    try:
        outputs = []
        for agent_name in routed_agents:
            emit(
                name=f"agent_{agent_name}",
                status="running",
                graph_node=f"agent_{agent_name}",
                detail=f"Running {agent_name} specialist synthesis...",
            )
            output = run_specialist_agent(agent_name, snapshot, memory_context)
            outputs.append(output)
            emit(
                name=f"agent_{output.agent_name}",
                status="warning" if output.uncertainty_notes else "completed",
                graph_node=f"agent_{output.agent_name}",
                warning_reason=(output.uncertainty_notes[0] if output.uncertainty_notes else None),
                confidence=output.confidence,
                detail=(
                    f"{output.summary}\nFindings: {', '.join(output.findings[:2])}"
                    f"{' | Flags: ' + ', '.join(output.flags) if output.flags else ''}"
                ),
                llm_excerpt=_format_specialist_excerpt(output),
            )
        emit(
            name="gp_synthesis",
            status="running",
            graph_node="gp_synthesis",
            detail="Running GP synthesis...",
        )
        report = run_gp_agent(snapshot, outputs, memory_context)
        emit(
            name="gp_synthesis",
            status="warning" if report.overall_confidence == ConfidenceLevel.LOW else "completed",
            graph_node="gp_synthesis",
            warning_reason=(
                "Overall synthesis confidence is low; prioritize conservative interpretation."
                if report.overall_confidence == ConfidenceLevel.LOW
                else None
            ),
            confidence=report.overall_confidence,
            detail=(
                "Coordinator merged specialist outputs into final report. "
                f"Problem list items: {len(report.problem_list)}, tasks: {len(report.tasks)}."
            ),
            llm_excerpt=_format_gp_excerpt(report),
        )
    except Exception as exc:
        # Make root-cause visible in backend console logs (provider errors, schema validation, retry exhaustion).
        logger.exception(
            "Workflow LLM execution failed; using fallbacks. user_id=%s kind=%s snapshot_id=%s simulated_date=%s routed_agents=%s error=%s",
            snapshot.user_id,
            snapshot.kind.value,
            snapshot.snapshot_id,
            snapshot.simulated_date,
            ",".join(routed_agents),
            repr(exc),
        )
        used_fallback = True
        execution_error = _format_execution_error(exc)
        outputs = [
            _make_agent_output_fallback(agent_name, getattr(snapshot, agent_name), kind)
            for agent_name in routed_agents
        ]
        for output in outputs:
            emit(
                name=f"agent_{output.agent_name}",
                status="warning",
                graph_node=f"agent_{output.agent_name}",
                warning_reason=(output.uncertainty_notes[0] if output.uncertainty_notes else "Fallback specialist output used."),
                confidence=output.confidence,
                detail=(
                    f"{output.summary}\nFindings: {', '.join(output.findings[:2])}"
                    f"{' | Flags: ' + ', '.join(output.flags) if output.flags else ''}"
                ),
                llm_excerpt=_format_specialist_excerpt(output),
            )
        report = _make_report_fallback(snapshot, outputs)
        emit(
            name="gp_synthesis",
            status="warning",
            graph_node="gp_synthesis",
            warning_reason="Fallback GP synthesis used because model execution failed.",
            confidence=report.overall_confidence,
            detail="Fallback GP synthesis used because model execution failed.",
            llm_excerpt=_format_gp_excerpt(report),
        )
        conversation.messages.append(
            ConversationMessage(
                role="assistant",
                content=f"LLM agent execution unavailable, fallback synthesis used: {exc.__class__.__name__}",
            )
        )
        store.save_conversation(conversation)

    if kind == RunKind.WEEKLY:
        provider = EvidenceProvider()
        candidates = _select_weekly_evidence_targets(outputs, provider.config.weekly_max_items)
        if provider.is_configured() and candidates:
            references = provider.get_supporting_references(
                candidates,
                scenario_id=scenario_id,
                simulated_date=simulated_date,
            )
            report = report.model_copy(update={"evidence_support": references})
            emit(
                name="evidence_attach",
                status="completed" if references else "warning",
                graph_node="evidence_attach",
                warning_reason=None if references else "No supporting evidence was attached for the selected issues.",
                detail=f"Weekly evidence attachment completed. Added {len(references)} reference(s).",
            )
        else:
            emit(
                name="evidence_attach",
                status="warning",
                graph_node="evidence_attach",
                warning_reason="Evidence provider unavailable or no eligible weekly evidence keys.",
                detail="Skipped weekly evidence retrieval; core synthesis remains unchanged.",
            )

    memory_items = _extract_memory(store, snapshot, report, outputs)
    emit(name="memory_update", status="completed", graph_node="memory_update", detail="Controlled memory extraction completed with provenance.")
    current_node = "completed"
    trace = WorkflowTrace(user_id=snapshot.user_id, kind=snapshot.kind, current_node=current_node, steps=live_steps)

    store.save_snapshot(snapshot)
    store.save_report(report)
    store.replace_memory_items_for_user(snapshot.user_id, memory_items)
    store.save_trace(trace)

    return WorkflowResult(
        snapshot=snapshot,
        conversation=conversation,
        specialist_outputs=outputs,
        report=report,
        memory_items=memory_items,
        trace=trace,
        execution_error=execution_error,
        used_fallback=used_fallback,
    )


def start_guided_checkin(store: SQLiteStore, user_id: str = "default") -> tuple[CheckInSession, Conversation, str]:
    conversation = store.save_conversation(
        Conversation(
            user_id=user_id,
            messages=[
                ConversationMessage(role="assistant", content="Let's do a short guided check-in."),
            ]
        )
    )
    session = store.save_checkin_session(CheckInSession(user_id=user_id, conversation_id=conversation.conversation_id))
    first_question = _questions()[0]
    conversation.messages.append(ConversationMessage(role="assistant", content=first_question))
    store.save_conversation(conversation)
    return session, conversation, first_question


def submit_guided_checkin_response(
    store: SQLiteStore, session_id: str, user_message: str
) -> tuple[CheckInSession, Conversation, str | None, WorkflowResult | None]:
    session = store.get_checkin_session(session_id)
    if session is None:
        raise ValueError("Check-in session not found.")
    if session.status != CheckInSessionStatus.ACTIVE:
        raise ValueError("Check-in session is not active.")

    conversation = store.get_conversation(session.conversation_id)
    if conversation is None:
        raise ValueError("Conversation not found.")
    conversation.messages.append(ConversationMessage(role="user", content=user_message))

    state: CheckInState = {
        "current_step": session.current_step,
        "user_response": user_message,
        "subjective": session.subjective,
        "needs_clarification": False,
        "next_question": "",
        "done": False,
    }
    next_state = CHECKIN_GRAPH.invoke(state)
    updated_session = session.model_copy(
        update={
            "current_step": next_state["current_step"],
            "subjective": next_state["subjective"],
        }
    )
    if next_state["done"]:
        updated_session = updated_session.model_copy(
            update={
                "status": CheckInSessionStatus.COMPLETED,
                "completed_at": datetime.now(timezone.utc),
            }
        )
        store.save_checkin_session(updated_session)
        completed_result = run_workflow(
            RunKind.DAILY,
            store,
            user_id=updated_session.user_id,
            subjective=updated_session.subjective,
            conversation=conversation,
        )
        return updated_session, completed_result.conversation, None, completed_result

    next_question = next_state["next_question"]
    conversation.messages.append(ConversationMessage(role="assistant", content=next_question))
    store.save_conversation(conversation)
    store.save_checkin_session(updated_session)
    return updated_session, conversation, next_question, None
