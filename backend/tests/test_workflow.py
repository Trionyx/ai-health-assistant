from pathlib import Path

from health_assistant.evidence import EvidenceProvider
from health_assistant.mock_data import Freshness
from health_assistant.models import ConfidenceLevel, FreshnessStatus, RunKind
from health_assistant.storage import SQLiteStore
from health_assistant.workflow import run_workflow


def test_freshness_rules() -> None:
    assert Freshness(12, 8).status() == FreshnessStatus.FRESH
    assert Freshness(30, 4).status() == FreshnessStatus.ACCEPTABLE
    assert Freshness(72, 1).status() == FreshnessStatus.STALE


def test_confidence_rules() -> None:
    assert Freshness(12, 7).confidence() == ConfidenceLevel.HIGH
    assert Freshness(12, 3).confidence() == ConfidenceLevel.MEDIUM
    assert Freshness(12, 1).confidence() == ConfidenceLevel.LOW


def test_daily_workflow_persists_report_trace_and_memory(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    result = run_workflow(RunKind.DAILY, store)

    assert result.report.daily_brief is not None
    assert "picture" in result.report.daily_brief.lower() or "today" in result.report.daily_brief.lower()
    assert result.report.top_priority is not None
    assert result.report.today_status in {"steady", "caution", "recovery_focus", "overload"}
    assert result.report.overall_confidence in {ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH}
    assert store.get_latest_report() is not None
    assert store.get_trace(result.trace.run_id) is not None
    assert 1 <= len(store.get_tasks()) <= 3
    assert len([task for task in result.report.tasks if task.is_recommended_first]) == 1
    assert len(store.get_memory_items()) >= 1
    memory_text = " ".join(item.content.lower() for item in result.memory_items)
    assert "latest report kind" not in memory_text
    assert "llm gateway" not in memory_text
    assert len(result.memory_items) <= 5


def test_weekly_workflow_uses_weekly_report(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    result = run_workflow(RunKind.WEEKLY, store)

    assert result.report.weekly_report is not None
    assert result.report.daily_brief is None
    assert result.trace.kind == RunKind.WEEKLY


def test_weekly_workflow_can_attach_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HA_ENABLE_EVIDENCE", "true")
    monkeypatch.setenv("HA_WEEKLY_EVIDENCE_MAX_ITEMS", "2")
    store = SQLiteStore(tmp_path / "test.db")

    calls = {"count": 0}

    def fake_get_supporting_references(self, findings, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return [
            {
                "title": "Exercise recovery and fatigue",
                "pmid": "99999",
                "journal": "Sports Med",
                "year": 2021,
                "short_summary": "Literature often links this pattern with fatigue/recovery dynamics.",
                "query_key": "recovery_strain",
            }
        ]

    monkeypatch.setattr(EvidenceProvider, "get_supporting_references", fake_get_supporting_references)
    # Keep this test independent from LLM availability; we only need to verify evidence attach flow.
    monkeypatch.setattr("health_assistant.workflow._select_weekly_evidence_targets", lambda outputs, max_items: [object()])
    result = run_workflow(RunKind.WEEKLY, store)
    assert calls["count"] >= 1
    assert len(result.report.evidence_support) <= 2


def test_daily_workflow_does_not_call_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HA_ENABLE_EVIDENCE", "true")
    store = SQLiteStore(tmp_path / "test.db")

    def fail_if_called(self, findings, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Evidence retrieval should not run during daily flow")

    monkeypatch.setattr(EvidenceProvider, "get_supporting_references", fail_if_called)
    result = run_workflow(RunKind.DAILY, store)
    assert result.report.evidence_support == []

