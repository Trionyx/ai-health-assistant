from pathlib import Path
import re

from health_assistant.models import MockScenarioProfile, RunKind
from health_assistant.storage import SQLiteStore
from health_assistant.workflow import run_workflow


def test_gp_brief_mentions_meaning_not_monitoring_language(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    store.save_mock_scenario_profile(
        MockScenarioProfile(
            user_id="default",
            scenario_id="high_stress_overload",
            scenario_start_date="2026-04-15",
            current_simulated_date="2026-04-16",
        )
    )
    result = run_workflow(RunKind.DAILY, store)
    brief = (result.report.daily_brief or "").lower()
    banned = [
        "inputs are recent and complete",
        "well-positioned for monitoring",
        "snapshot includes data quality/availability",
        "we can’t comment yet",
    ]
    assert all(token not in brief for token in banned)
    assert any(token in brief for token in ["mixed", "strain", "sleep", "recovery", "stress", "pattern", "today"])


def test_tasks_align_with_scenario_meaning(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    store.save_mock_scenario_profile(
        MockScenarioProfile(
            user_id="default",
            scenario_id="severe_sleep_debt",
            scenario_start_date="2026-04-15",
            current_simulated_date="2026-04-16",
        )
    )
    result = run_workflow(RunKind.DAILY, store)
    task_text = " ".join(f"{task.title} {task.description}".lower() for task in result.report.tasks)
    assert any(token in task_text for token in ["bedtime", "sleep", "light", "intensity", "fatigue", "recovery"])


def test_top_priority_and_status_match_sleep_debt(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    store.save_mock_scenario_profile(
        MockScenarioProfile(
            user_id="default",
            scenario_id="severe_sleep_debt",
            scenario_start_date="2026-04-15",
            current_simulated_date="2026-04-16",
        )
    )
    result = run_workflow(RunKind.DAILY, store)
    assert result.report.today_status == "recovery_focus"
    assert result.report.top_priority is not None
    assert "sleep" in result.report.top_priority.lower() or "recovery" in result.report.top_priority.lower()


def test_summary_is_concise_and_has_single_recommended_first_task(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    store.save_mock_scenario_profile(
        MockScenarioProfile(
            user_id="default",
            scenario_id="high_stress_overload",
            scenario_start_date="2026-04-15",
            current_simulated_date="2026-04-16",
        )
    )
    result = run_workflow(RunKind.DAILY, store)
    brief = (result.report.daily_brief or "").strip()
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", brief) if part.strip()]
    assert 2 <= len(sentences) <= 4
    recommended = [task for task in result.report.tasks if task.is_recommended_first]
    assert len(recommended) == 1
