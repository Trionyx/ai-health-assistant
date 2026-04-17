from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .models import ConfidenceLevel, DomainSignal, FreshnessStatus, HealthSnapshot, RunKind, TrendDirection


@dataclass(frozen=True)
class Freshness:
    hours_since_last: int
    data_points: int

    def status(self) -> FreshnessStatus:
        if self.hours_since_last <= 24:
            return FreshnessStatus.FRESH
        if self.hours_since_last <= 48:
            return FreshnessStatus.ACCEPTABLE
        return FreshnessStatus.STALE

    def confidence(self) -> ConfidenceLevel:
        if self.data_points >= 7:
            return ConfidenceLevel.HIGH
        if self.data_points >= 3:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW


@dataclass(frozen=True)
class ScenarioReaction:
    text: str
    tone: str


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    title: str
    problem_case: str
    description: str
    expected_agent_reactions: list[ScenarioReaction]


SCENARIOS: dict[str, ScenarioSpec] = {
    "baseline": ScenarioSpec(
        scenario_id="baseline",
        title="Baseline mixed quality",
        problem_case="Mostly acceptable state with mild recovery uncertainty",
        description="Moderate sleep/activity with slightly sparse recovery context.",
        expected_agent_reactions=[
            ScenarioReaction(text="Sleep profile is near baseline with minor variability.", tone="neutral"),
            ScenarioReaction(text="Activity remains moderate and generally stable.", tone="positive"),
            ScenarioReaction(text="Recovery confidence is slightly limited versus other domains.", tone="warning"),
        ],
    ),
    "severe_sleep_debt": ScenarioSpec(
        scenario_id="severe_sleep_debt",
        title="Severe sleep debt",
        problem_case="Sleep duration and regularity are below baseline with fatigue pattern",
        description="Persistent sleep deficit with reduced resilience and elevated fatigue.",
        expected_agent_reactions=[
            ScenarioReaction(text="Sleep is materially below baseline and remains irregular.", tone="warning"),
            ScenarioReaction(text="Recovery profile is impaired after repeated short nights.", tone="warning"),
            ScenarioReaction(text="Guidance should prioritize sleep restoration and load reduction.", tone="warning"),
        ],
    ),
    "high_stress_overload": ScenarioSpec(
        scenario_id="high_stress_overload",
        title="High stress overload",
        problem_case="Stress elevated with degraded recovery and volatile activity strain",
        description="High stress and lower energy with clear recovery suppression versus baseline.",
        expected_agent_reactions=[
            ScenarioReaction(text="Recovery is below baseline with strain accumulation signs.", tone="warning"),
            ScenarioReaction(text="Activity pattern is volatile and should be paced.", tone="warning"),
            ScenarioReaction(text="A lighter day plus decompression should improve trajectory.", tone="positive"),
        ],
    ),
    "overtraining_recovery_crash": ScenarioSpec(
        scenario_id="overtraining_recovery_crash",
        title="Overtraining and recovery crash",
        problem_case="Load intensity remains high while recovery drops well below baseline",
        description="High-volume activity with under-recovered physiology and elevated soreness.",
        expected_agent_reactions=[
            ScenarioReaction(text="Activity load appears too high for the current recovery state.", tone="warning"),
            ScenarioReaction(text="Recovery is materially suppressed relative to baseline.", tone="warning"),
            ScenarioReaction(text="Immediate deload and recovery-first pacing are recommended.", tone="warning"),
        ],
    ),
}


def list_scenarios() -> list[ScenarioSpec]:
    return list(SCENARIOS.values())


def get_scenario(scenario_id: str) -> ScenarioSpec:
    return SCENARIOS.get(scenario_id, SCENARIOS["baseline"])


def _parse_day(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    return datetime.now(timezone.utc).date()


def get_simulated_today(current_simulated_date: str | date | None = None) -> date:
    return _parse_day(current_simulated_date)


def _trend(delta: float, *, volatile: bool = False) -> TrendDirection:
    if volatile:
        return TrendDirection.VOLATILE
    if delta > 0.05:
        return TrendDirection.IMPROVING
    if delta < -0.05:
        return TrendDirection.DECLINING
    return TrendDirection.STABLE


def _daily_values(scenario_id: str, day_index: int) -> dict[str, float | int | str]:
    # Day index is relative to scenario_start_date (0..4 visible horizon)
    if scenario_id == "high_stress_overload":
        return {
            "total_sleep_hours": 6.8 - 0.1 * day_index,
            "sleep_baseline_hours": 7.6,
            "sleep_regularity_score": 58 - 2 * day_index,
            "steps": 8200 + (1200 if day_index % 2 == 0 else -1800),
            "steps_baseline": 9000,
            "activity_minutes": 52 + (18 if day_index % 2 == 0 else -16),
            "hrv": 39 - day_index,
            "hrv_baseline": 53,
            "resting_hr": 71 + day_index,
            "resting_hr_baseline": 64,
            "energy": 4 - (1 if day_index >= 3 else 0),
            "stress": 8,
            "soreness": 6,
            "subjective_notes": "Stress load remains high and energy feels compressed.",
        }
    if scenario_id == "severe_sleep_debt":
        return {
            "total_sleep_hours": 5.3 - 0.15 * day_index,
            "sleep_baseline_hours": 7.7,
            "sleep_regularity_score": 44 - day_index,
            "steps": 6100 - 250 * day_index,
            "steps_baseline": 8500,
            "activity_minutes": 29 - day_index,
            "hrv": 36 - day_index,
            "hrv_baseline": 50,
            "resting_hr": 72 + day_index,
            "resting_hr_baseline": 63,
            "energy": 3,
            "stress": 7,
            "soreness": 5,
            "subjective_notes": "Fatigue is persistent and mornings feel heavy.",
        }
    if scenario_id == "overtraining_recovery_crash":
        return {
            "total_sleep_hours": 6.4 - 0.1 * day_index,
            "sleep_baseline_hours": 7.5,
            "sleep_regularity_score": 50 - day_index,
            "steps": 13000 + 350 * day_index,
            "steps_baseline": 9500,
            "activity_minutes": 82 + 4 * day_index,
            "hrv": 32 - day_index,
            "hrv_baseline": 52,
            "resting_hr": 74 + day_index,
            "resting_hr_baseline": 62,
            "energy": 3,
            "stress": 8,
            "soreness": 8,
            "subjective_notes": "Body feels taxed despite sustained training volume.",
        }
    # baseline
    return {
        "total_sleep_hours": 7.0 + 0.05 * day_index,
        "sleep_baseline_hours": 7.4,
        "sleep_regularity_score": 72 - day_index,
        "steps": 8600 + 200 * day_index,
        "steps_baseline": 9000,
        "activity_minutes": 44 + day_index,
        "hrv": 48 - (1 if day_index >= 3 else 0),
        "hrv_baseline": 50,
        "resting_hr": 65 + (1 if day_index >= 3 else 0),
        "resting_hr_baseline": 64,
        "energy": 6,
        "stress": 5,
        "soreness": 4,
        "subjective_notes": "Generally okay day with manageable strain.",
    }


def get_visible_mock_data(
    scenario_id: str,
    *,
    simulated_day: str | date,
    scenario_start_date: str | date,
    horizon_days: int = 5,
) -> list[dict]:
    simulated = _parse_day(simulated_day)
    start = _parse_day(scenario_start_date)
    records: list[dict] = []
    for idx in range(horizon_days):
        day = start + timedelta(days=idx)
        values = _daily_values(scenario_id, idx)
        records.append({"date": day.isoformat(), "values": values})
    return [record for record in records if date.fromisoformat(record["date"]) <= simulated]


def get_trend_window(records: list[dict], days: int = 5) -> list[dict]:
    return records[-days:]


def _signal(
    *,
    hours_since_last: int,
    data_points: int,
    note: str,
    trend: TrendDirection,
    values: dict[str, float | int | str | bool],
    total_sleep_hours: float | None = None,
    sleep_baseline_hours: float | None = None,
    sleep_regularity_score: float | None = None,
    steps: int | None = None,
    steps_baseline: int | None = None,
    activity_minutes: int | None = None,
    hrv: int | None = None,
    hrv_baseline: int | None = None,
    resting_hr: int | None = None,
    resting_hr_baseline: int | None = None,
    energy: int | None = None,
    stress: int | None = None,
    soreness: int | None = None,
) -> DomainSignal:
    freshness = Freshness(hours_since_last, data_points)
    return DomainSignal(
        hours_since_last=hours_since_last,
        data_points=data_points,
        freshness=freshness.status(),
        confidence=freshness.confidence(),
        notes=[note],
        trend=trend,
        values=values,
        total_sleep_hours=total_sleep_hours,
        sleep_baseline_hours=sleep_baseline_hours,
        sleep_regularity_score=sleep_regularity_score,
        steps=steps,
        steps_baseline=steps_baseline,
        activity_minutes=activity_minutes,
        hrv=hrv,
        hrv_baseline=hrv_baseline,
        resting_hr=resting_hr,
        resting_hr_baseline=resting_hr_baseline,
        energy=energy,
        stress=stress,
        soreness=soreness,
    )


def _aggregate_freshness(signals: list[DomainSignal]) -> FreshnessStatus:
    if any(signal.freshness == FreshnessStatus.STALE for signal in signals):
        return FreshnessStatus.STALE
    if any(signal.freshness == FreshnessStatus.ACCEPTABLE for signal in signals):
        return FreshnessStatus.ACCEPTABLE
    return FreshnessStatus.FRESH


def _aggregate_confidence(signals: list[DomainSignal]) -> ConfidenceLevel:
    if any(signal.confidence == ConfidenceLevel.LOW for signal in signals):
        return ConfidenceLevel.LOW
    if any(signal.confidence == ConfidenceLevel.MEDIUM for signal in signals):
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.HIGH


def load_mock_snapshot(
    kind: RunKind,
    scenario_id: str = "baseline",
    *,
    simulated_day: str | date | None = None,
    scenario_start_date: str | date | None = None,
) -> HealthSnapshot:
    scenario = get_scenario(scenario_id)
    simulated = get_simulated_today(simulated_day)
    start = _parse_day(scenario_start_date) if scenario_start_date else simulated
    visible = get_visible_mock_data(
        scenario_id,
        simulated_day=simulated,
        scenario_start_date=start,
        horizon_days=5,
    )
    if not visible:
        visible = get_visible_mock_data(
            scenario_id,
            simulated_day=start,
            scenario_start_date=start,
            horizon_days=5,
        )[:1]
    current = visible[-1]["values"]
    trend_window = get_trend_window(visible, days=5 if kind == RunKind.WEEKLY else 3)
    sleep_delta = (float(current["total_sleep_hours"]) - float(current["sleep_baseline_hours"])) / float(current["sleep_baseline_hours"])
    steps_delta = (int(current["steps"]) - int(current["steps_baseline"])) / max(int(current["steps_baseline"]), 1)
    hrv_delta = (int(current["hrv"]) - int(current["hrv_baseline"])) / max(int(current["hrv_baseline"]), 1)
    rhr_delta = (int(current["resting_hr_baseline"]) - int(current["resting_hr"])) / max(int(current["resting_hr_baseline"]), 1)

    recovery_points = len(trend_window) * (2 if scenario_id == "baseline" else 3)
    if scenario_id == "baseline":
        recovery_points = max(2, recovery_points - 3)
    sleep = _signal(
        hours_since_last=6,
        data_points=max(3, len(trend_window) * 2),
        note=f"Sleep relative to baseline is {sleep_delta:+.0%}.",
        trend=_trend(sleep_delta),
        total_sleep_hours=float(current["total_sleep_hours"]),
        sleep_baseline_hours=float(current["sleep_baseline_hours"]),
        sleep_regularity_score=float(current["sleep_regularity_score"]),
        energy=int(current["energy"]),
        stress=int(current["stress"]),
        soreness=int(current["soreness"]),
        values={
            "sleep_hours_delta": round(float(current["total_sleep_hours"]) - float(current["sleep_baseline_hours"]), 2),
            "sleep_points_last_7d": len(trend_window),
        },
    )
    activity = _signal(
        hours_since_last=8,
        data_points=max(3, len(trend_window) * 2),
        note=f"Activity relative to baseline is {steps_delta:+.0%}.",
        trend=_trend(steps_delta, volatile=scenario_id in {"high_stress_overload", "overtraining_recovery_crash"}),
        steps=int(current["steps"]),
        steps_baseline=int(current["steps_baseline"]),
        activity_minutes=int(current["activity_minutes"]),
        energy=int(current["energy"]),
        stress=int(current["stress"]),
        soreness=int(current["soreness"]),
        values={
            "steps_delta": int(current["steps"]) - int(current["steps_baseline"]),
            "activity_minutes": int(current["activity_minutes"]),
        },
    )
    recovery = _signal(
        hours_since_last=12 if scenario_id != "baseline" else 30,
        data_points=recovery_points,
        note="Recovery reflects HRV and resting heart-rate relative to baseline.",
        trend=_trend((hrv_delta + rhr_delta) / 2),
        hrv=int(current["hrv"]),
        hrv_baseline=int(current["hrv_baseline"]),
        resting_hr=int(current["resting_hr"]),
        resting_hr_baseline=int(current["resting_hr_baseline"]),
        energy=int(current["energy"]),
        stress=int(current["stress"]),
        soreness=int(current["soreness"]),
        values={
            "hrv_vs_baseline": int(current["hrv"]) - int(current["hrv_baseline"]),
            "resting_hr_vs_baseline": int(current["resting_hr"]) - int(current["resting_hr_baseline"]),
            "recovery_data_recent": scenario_id != "baseline",
        },
    )

    signals = [sleep, activity, recovery]
    visible_start = visible[0]["date"]
    visible_end = visible[-1]["date"]
    return HealthSnapshot(
        kind=kind,
        window_label=f"Visible window: {visible_start} to {visible_end}",
        simulated_date=simulated.isoformat(),
        scenario_start_date=start.isoformat(),
        visible_window_start=visible_start,
        visible_window_end=visible_end,
        visible_days_count=len(visible),
        sleep=sleep,
        activity=activity,
        recovery=recovery,
        summary_freshness=_aggregate_freshness(signals),
        summary_confidence=_aggregate_confidence(signals),
    )

