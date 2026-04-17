from datetime import date

from health_assistant.mock_data import get_visible_mock_data, load_mock_snapshot
from health_assistant.models import RunKind


def test_future_data_invisibility() -> None:
    start = date(2026, 4, 15)
    simulated_day = date(2026, 4, 16)
    visible = get_visible_mock_data(
        "high_stress_overload",
        simulated_day=simulated_day,
        scenario_start_date=start,
        horizon_days=5,
    )
    assert visible
    assert all(item["date"] <= simulated_day.isoformat() for item in visible)
    assert not any(item["date"] > simulated_day.isoformat() for item in visible)


def test_high_stress_overload_has_lower_recovery_than_baseline() -> None:
    simulated = "2026-04-16"
    start = "2026-04-15"
    baseline = load_mock_snapshot(RunKind.DAILY, "baseline", simulated_day=simulated, scenario_start_date=start)
    stress = load_mock_snapshot(RunKind.DAILY, "high_stress_overload", simulated_day=simulated, scenario_start_date=start)
    assert stress.recovery.hrv is not None and baseline.recovery.hrv is not None
    assert stress.recovery.hrv < baseline.recovery.hrv
    assert stress.recovery.resting_hr is not None and baseline.recovery.resting_hr is not None
    assert stress.recovery.resting_hr > baseline.recovery.resting_hr


def test_severe_sleep_debt_has_lower_sleep_than_baseline() -> None:
    simulated = "2026-04-16"
    start = "2026-04-15"
    baseline = load_mock_snapshot(RunKind.DAILY, "baseline", simulated_day=simulated, scenario_start_date=start)
    debt = load_mock_snapshot(RunKind.DAILY, "severe_sleep_debt", simulated_day=simulated, scenario_start_date=start)
    assert debt.sleep.total_sleep_hours is not None and baseline.sleep.total_sleep_hours is not None
    assert debt.sleep.total_sleep_hours < baseline.sleep.total_sleep_hours
    assert debt.sleep.energy is not None and baseline.sleep.energy is not None
    assert debt.sleep.energy <= baseline.sleep.energy
