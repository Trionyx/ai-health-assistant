from health_assistant.evidence import EVIDENCE_QUERY_MAP, EvidenceConfig, EvidenceProvider, _build_scenario_pubmed_query
from health_assistant.models import ConfidenceLevel, SeverityLevel, SpecialistAgentOutput, TrendDirection


def _specialist(agent_name: str, key: str | None = None) -> SpecialistAgentOutput:
    return SpecialistAgentOutput(
        agent_name=agent_name,
        summary="summary",
        finding="finding",
        severity=SeverityLevel.MODERATE,
        trend=TrendDirection.STABLE,
        supporting_signals={},
        interpretive_summary="interpretive",
        findings=["one"],
        flags=[],
        recommendations_draft=[],
        confidence=ConfidenceLevel.MEDIUM,
        uncertainty_notes=[],
        used_data_points=7,
        evidence_query_key=key,
        evidence_topic=None,
        evidence_relevance_reason=None,
    )


def test_query_key_mapping_contains_expected_templates() -> None:
    assert EVIDENCE_QUERY_MAP["sleep_deficit"] == "sleep deprivation recovery fatigue"
    assert EVIDENCE_QUERY_MAP["recovery_strain"] == "heart rate variability stress recovery exercise"
    assert EVIDENCE_QUERY_MAP["overload_pattern"] == "exercise recovery fatigue overreaching"
    assert EVIDENCE_QUERY_MAP["stress_fatigue"] == "psychological stress fatigue recovery"


def test_evidence_provider_configuration_check() -> None:
    provider = EvidenceProvider(
        EvidenceConfig(
            enabled=True,
            weekly_max_items=2,
            transport="http",
            mcp_command="pubmed-mcp-server",
            mcp_args=[],
            mcp_url="http://pubmed_mcp:8081",
            timeout_seconds=5,
        )
    )
    assert provider.is_configured() is True


def test_get_evidence_for_key_normalizes_results(monkeypatch) -> None:
    provider = EvidenceProvider(
        EvidenceConfig(
            enabled=True,
            weekly_max_items=2,
            transport="stdio",
            mcp_command="pubmed-mcp-server",
            mcp_args=[],
            mcp_url="http://example",
            timeout_seconds=5,
        )
    )
    monkeypatch.setattr(
        provider,
        "_search_pubmed",
        lambda query, max_results, **kwargs: [
            {
                "title": "Sleep restriction and fatigue",
                "pmid": "12345",
                "journal": "Sleep",
                "year": 2020,
                "abstract": "Sleep restriction is associated with higher fatigue.",
            }
        ],
    )
    refs = provider.get_evidence_for_key("sleep_deficit")
    assert len(refs) == 1
    assert refs[0].pmid == "12345"
    assert refs[0].query_key == "sleep_deficit"
    assert "associated" in refs[0].short_summary.lower() or "links" in refs[0].short_summary.lower()


def test_mcp_unavailable_returns_empty_list(monkeypatch) -> None:
    provider = EvidenceProvider(
        EvidenceConfig(
            enabled=True,
            weekly_max_items=2,
            transport="stdio",
            mcp_command="pubmed-mcp-server",
            mcp_args=[],
            mcp_url="http://example",
            timeout_seconds=5,
        )
    )
    monkeypatch.setattr(
        provider,
        "_search_pubmed",
        lambda query, max_results, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert provider.get_evidence_for_key("sleep_deficit") == []


def test_supporting_references_deduplicates_and_limits(monkeypatch) -> None:
    provider = EvidenceProvider(
        EvidenceConfig(
            enabled=True,
            weekly_max_items=2,
            transport="stdio",
            mcp_command="pubmed-mcp-server",
            mcp_args=[],
            mcp_url="http://example",
            timeout_seconds=5,
        )
    )
    monkeypatch.setattr(
        provider,
        "get_evidence_for_key",
        lambda key, **kwargs: [
            provider._normalize_results(
                [
                    {
                        "title": f"Title {key}",
                        "pmid": "same-pmid" if key == "sleep_deficit" else f"pmid-{key}",
                        "journal": "J",
                        "year": 2022,
                        "summary": "Linked in literature",
                    }
                ],
                key,
            )[0]
        ],
    )
    refs = provider.get_supporting_references(
        [
            _specialist("sleep", "sleep_deficit"),
            _specialist("recovery", "recovery_strain"),
            _specialist("activity", "overload_pattern"),
        ]
    )
    assert len(refs) == 2


def test_scenario_pubmed_query_differs_by_scenario() -> None:
    base = EVIDENCE_QUERY_MAP["sleep_deficit"]
    q_baseline = _build_scenario_pubmed_query(base, "baseline", "sleep_deficit")
    q_severe = _build_scenario_pubmed_query(base, "severe_sleep_debt", "sleep_deficit")
    assert q_baseline != q_severe
    assert base in q_baseline and base in q_severe


def test_get_evidence_for_key_passes_scenario_to_search(monkeypatch) -> None:
    provider = EvidenceProvider(
        EvidenceConfig(
            enabled=True,
            weekly_max_items=2,
            transport="http",
            mcp_command="pubmed-mcp-server",
            mcp_args=[],
            mcp_url="http://pubmed_mcp:8081",
            timeout_seconds=5,
        )
    )
    captured: dict[str, object] = {}

    def fake_search(query, max_results, *, offset=0, sort="relevance"):  # type: ignore[no-untyped-def]
        captured["query"] = query
        captured["offset"] = offset
        captured["sort"] = sort
        return [
            {
                "title": "Paper",
                "pmid": "1",
                "journal": "J",
                "year": 2020,
                "summary": "Unique summary text for support line.",
            }
        ]

    monkeypatch.setattr(provider, "_search_http", fake_search)
    provider.get_evidence_for_key("sleep_deficit", scenario_id="high_stress_overload", simulated_date="2026-04-01")
    q = str(captured["query"]).lower()
    assert "sleep disruption" in q or "stress hormones" in q
    assert isinstance(captured["offset"], int)
    assert captured["sort"] in {"relevance", "pub_date", "author", "journal"}


def test_support_line_skips_title_only_duplicate() -> None:
    provider = EvidenceProvider(EvidenceConfig(enabled=False, weekly_max_items=2, transport="http", mcp_command="", mcp_args=[], mcp_url="", timeout_seconds=1))
    line = provider._safe_support_line("Identical Title", title="Identical Title")
    assert "Identical Title" not in line
    assert "fatigue" in line.lower() or "stress" in line.lower()


def test_get_evidence_for_key_falls_back_when_offset_returns_no_results(monkeypatch) -> None:
    provider = EvidenceProvider(
        EvidenceConfig(
            enabled=True,
            weekly_max_items=2,
            transport="http",
            mcp_command="pubmed-mcp-server",
            mcp_args=[],
            mcp_url="http://pubmed_mcp:8081",
            timeout_seconds=5,
        )
    )

    calls: list[tuple[str, int, str]] = []

    def fake_search(query, max_results, *, offset=0, sort="relevance"):  # type: ignore[no-untyped-def]
        calls.append((str(query), int(offset), str(sort)))
        if len(calls) == 1:
            return []
        return [
            {
                "title": "Recovered fallback evidence",
                "pmid": "fallback-1",
                "journal": "J",
                "year": 2024,
                "summary": "Fallback query returned data.",
            }
        ]

    monkeypatch.setattr(provider, "_search_http", fake_search)
    refs = provider.get_evidence_for_key(
        "sleep_deficit",
        scenario_id="overtraining_recovery_crash",
        simulated_date="2026-04-16",
    )

    assert len(refs) == 1
    assert refs[0].pmid == "fallback-1"
    assert len(calls) >= 2
    assert calls[1][1] == 0
