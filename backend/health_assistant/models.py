from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    ACCEPTABLE = "acceptable"
    STALE = "stale"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RunKind(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class CheckInSessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SeverityLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class TrendDirection(str, Enum):
    IMPROVING = "improving"
    STABLE = "stable"
    DECLINING = "declining"
    VOLATILE = "volatile"


class DomainSignal(BaseModel):
    hours_since_last: int
    data_points: int
    freshness: FreshnessStatus
    confidence: ConfidenceLevel
    notes: list[str] = Field(default_factory=list)
    trend: TrendDirection = TrendDirection.STABLE
    # Sleep metrics
    total_sleep_hours: float | None = None
    sleep_baseline_hours: float | None = None
    sleep_regularity_score: float | None = None
    # Activity metrics
    steps: int | None = None
    steps_baseline: int | None = None
    activity_minutes: int | None = None
    # Recovery metrics
    hrv: int | None = None
    hrv_baseline: int | None = None
    resting_hr: int | None = None
    resting_hr_baseline: int | None = None
    # Subjective for direct specialist interpretation support
    energy: int | None = None
    stress: int | None = None
    soreness: int | None = None
    values: dict[str, float | int | str | bool] = Field(default_factory=dict)


class SubjectiveSignals(BaseModel):
    mood_score: int | None = None
    stress_score: int | None = None
    energy_score: int | None = None
    sleep_quality: str | None = None
    symptom_flags: list[str] = Field(default_factory=list)
    summary: str | None = None


class HealthSnapshot(BaseModel):
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = "default"
    kind: RunKind
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Literal["mock"] = "mock"
    window_label: str
    simulated_date: str | None = None
    scenario_start_date: str | None = None
    visible_window_start: str | None = None
    visible_window_end: str | None = None
    visible_days_count: int = 0
    sleep: DomainSignal
    activity: DomainSignal
    recovery: DomainSignal
    subjective: SubjectiveSignals | None = None
    summary_freshness: FreshnessStatus
    summary_confidence: ConfidenceLevel


class SpecialistAgentOutput(BaseModel):
    agent_name: str
    summary: str
    finding: str
    severity: SeverityLevel
    trend: TrendDirection
    supporting_signals: dict[str, float | int | str] = Field(default_factory=dict)
    interpretive_summary: str
    findings: list[str]
    flags: list[str]
    recommendations_draft: list[str]
    confidence: ConfidenceLevel
    uncertainty_notes: list[str]
    used_data_points: int
    evidence_query_key: Literal["sleep_deficit", "recovery_strain", "overload_pattern", "stress_fatigue"] | None = None
    evidence_topic: str | None = None
    evidence_relevance_reason: str | None = None


class TaskDraft(BaseModel):
    title: str
    description: str


class TaskItem(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    description: str
    status: Literal["export_candidate"] = "export_candidate"
    is_recommended_first: bool = False


class EvidenceReference(BaseModel):
    title: str
    pmid: str
    journal: str
    year: int | None = None
    short_summary: str
    query_key: Literal["sleep_deficit", "recovery_strain", "overload_pattern", "stress_fatigue"]


class GPReportOutput(BaseModel):
    report_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = "default"
    snapshot_id: str
    kind: RunKind
    problem_list: list[str]
    daily_brief: str | None = None
    weekly_report: str | None = None
    priority_flags: list[str]
    next_steps: list[str]
    tasks: list[TaskItem]
    evidence_support: list[EvidenceReference] = Field(default_factory=list)
    top_priority: str | None = None
    today_status: Literal["steady", "caution", "recovery_focus", "overload"] | None = None
    overall_confidence: ConfidenceLevel
    uncertainty_notes: list[str]
    safety_notes: list[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GPReportDraft(BaseModel):
    problem_list: list[str]
    daily_brief: str | None = None
    weekly_report: str | None = None
    priority_flags: list[str]
    next_steps: list[str]
    tasks: list[TaskDraft]
    overall_confidence: ConfidenceLevel
    uncertainty_notes: list[str]
    safety_notes: list[str]


class MemoryItem(BaseModel):
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    category: Literal["preference", "pattern", "persistent_fact"]
    content: str
    source: str
    confidence: ConfidenceLevel
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowTraceStep(BaseModel):
    name: str
    status: Literal["running", "completed", "warning"]
    freshness: FreshnessStatus | None = None
    confidence: ConfidenceLevel | None = None
    detail: str
    warning_reason: str | None = None
    graph_node: str | None = None
    llm_excerpt: str | None = None
    duration_ms: int | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowTrace(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = "default"
    kind: RunKind
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_node: str | None = None
    steps: list[WorkflowTraceStep]


class ConversationMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Conversation(BaseModel):
    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = "default"
    messages: list[ConversationMessage] = Field(default_factory=list)


class CheckInSession(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = "default"
    conversation_id: str
    status: CheckInSessionStatus = CheckInSessionStatus.ACTIVE
    current_step: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    subjective: SubjectiveSignals = Field(default_factory=SubjectiveSignals)


class UserProfile(BaseModel):
    user_id: str = "default"
    display_name: str = "Default User"
    email: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuthUser(BaseModel):
    user_id: str = Field(default_factory=lambda: str(uuid4()))
    email: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuthSession(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MockScenarioProfile(BaseModel):
    user_id: str
    scenario_id: str = "baseline"
    current_simulated_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).date().isoformat())
    scenario_start_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).date().isoformat())
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
