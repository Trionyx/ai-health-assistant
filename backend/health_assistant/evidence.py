from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import shlex
import subprocess
from typing import Any
from urllib import error, request

from .models import EvidenceReference, SpecialistAgentOutput

logger = logging.getLogger("health_assistant.evidence")

EVIDENCE_QUERY_MAP: dict[str, str] = {
    "sleep_deficit": "sleep deprivation recovery fatigue",
    "recovery_strain": "heart rate variability stress recovery exercise",
    "overload_pattern": "exercise recovery fatigue overreaching",
    "stress_fatigue": "psychological stress fatigue recovery",
}

_KNOWN_SCENARIOS = frozenset(
    {"baseline", "severe_sleep_debt", "high_stress_overload", "overtraining_recovery_crash"}
)

# Demo-friendly: same evidence keys stay on-template, but each mock scenario nudges PubMed
# toward a different slice of literature (query terms + pagination in get_evidence_for_key).
EVIDENCE_SCENARIO_QUERY_EXTRAS: dict[tuple[str, str], str] = {
    ("baseline", "sleep_deficit"): "circadian timing athletes",
    ("severe_sleep_debt", "sleep_deficit"): "chronic sleep loss metabolism",
    ("high_stress_overload", "sleep_deficit"): "sleep disruption stress hormones",
    ("overtraining_recovery_crash", "sleep_deficit"): "sleep quality training load",
    ("baseline", "recovery_strain"): "heart rate variability training",
    ("severe_sleep_debt", "recovery_strain"): "slow wave sleep recovery",
    ("high_stress_overload", "recovery_strain"): "sympathetic arousal recovery",
    ("overtraining_recovery_crash", "recovery_strain"): "parasympathetic reactivation fatigue",
    ("baseline", "overload_pattern"): "progressive overload adaptation",
    ("severe_sleep_debt", "overload_pattern"): "inadequate recovery sessions",
    ("high_stress_overload", "overload_pattern"): "non functional overreaching",
    ("overtraining_recovery_crash", "overload_pattern"): "overtraining syndrome markers",
    ("baseline", "stress_fatigue"): "mental fatigue cognition",
    ("severe_sleep_debt", "stress_fatigue"): "sleepiness daytime impairment",
    ("high_stress_overload", "stress_fatigue"): "burnout workload recovery",
    ("overtraining_recovery_crash", "stress_fatigue"): "mood disturbance athletes",
}

SCENARIO_DEFAULT_QUERY_EXTRAS: dict[str, str] = {
    "baseline": "cohort observational",
    "severe_sleep_debt": "sleep debt sustained restriction",
    "high_stress_overload": "psychological strain autonomic",
    "overtraining_recovery_crash": "functional overreaching markers",
}

PUBMED_SORT_MODES: tuple[str, ...] = ("relevance", "pub_date", "author", "journal")


def _canonical_scenario_id(scenario_id: str) -> str:
    cleaned = (scenario_id or "").strip()
    return cleaned if cleaned in _KNOWN_SCENARIOS else "baseline"


def _build_scenario_pubmed_query(base: str, scenario_id: str, evidence_query_key: str) -> str:
    canon = _canonical_scenario_id(scenario_id)
    extra = EVIDENCE_SCENARIO_QUERY_EXTRAS.get((canon, evidence_query_key)) or SCENARIO_DEFAULT_QUERY_EXTRAS.get(
        canon, ""
    )
    parts = [base.strip()]
    if extra:
        parts.append(extra.strip())
    return " ".join(parts)


def _pubmed_pagination(scenario_id: str, simulated_date: str, evidence_query_key: str) -> tuple[int, str]:
    raw = hashlib.sha256(f"{scenario_id}|{simulated_date}|{evidence_query_key}".encode("utf-8")).digest()
    marker = int.from_bytes(raw[:4], "big")
    offset = (marker % 18) * 3
    sort = PUBMED_SORT_MODES[(marker >> 8) % len(PUBMED_SORT_MODES)]
    return offset, sort


@dataclass(frozen=True)
class EvidenceConfig:
    enabled: bool
    weekly_max_items: int
    transport: str
    mcp_command: str
    mcp_args: list[str]
    mcp_url: str
    timeout_seconds: float

    @staticmethod
    def from_env() -> "EvidenceConfig":
        command = os.getenv("HA_PUBMED_MCP_COMMAND", "pubmed-mcp-server")
        args_raw = os.getenv("HA_PUBMED_MCP_ARGS", "")
        parsed_args = shlex.split(args_raw) if args_raw else []
        return EvidenceConfig(
            enabled=os.getenv("HA_ENABLE_EVIDENCE", "false").strip().lower() == "true",
            weekly_max_items=max(int(os.getenv("HA_WEEKLY_EVIDENCE_MAX_ITEMS", "2") or "2"), 1),
            transport=os.getenv("HA_MCP_TRANSPORT", "stdio").strip().lower(),
            mcp_command=command,
            mcp_args=parsed_args,
            mcp_url=os.getenv("HA_PUBMED_MCP_URL", "http://pubmed_mcp:8081").strip(),
            timeout_seconds=float(os.getenv("HA_MCP_TIMEOUT_SECONDS", "10") or "10"),
        )


class EvidenceProvider:
    def __init__(self, config: EvidenceConfig | None = None) -> None:
        self.config = config or EvidenceConfig.from_env()

    def is_configured(self) -> bool:
        if not self.config.enabled:
            return False
        if self.config.transport == "http":
            return bool(self.config.mcp_url)
        return bool(self.config.mcp_command)

    def get_evidence_for_key(
        self,
        evidence_query_key: str,
        *,
        scenario_id: str = "",
        simulated_date: str = "",
    ) -> list[EvidenceReference]:
        if not self.is_configured():
            return []
        base = EVIDENCE_QUERY_MAP.get(evidence_query_key)
        if not base:
            return []
        query = _build_scenario_pubmed_query(base, scenario_id, evidence_query_key)
        offset, sort = _pubmed_pagination(scenario_id or "baseline", simulated_date or "", evidence_query_key)
        attempts: list[tuple[str, int, str]] = [
            (query, offset, sort),
            (query, 0, "relevance"),
            (base, 0, "relevance"),
        ]
        for attempt_query, attempt_offset, attempt_sort in attempts:
            try:
                raw = self._search_pubmed(
                    attempt_query,
                    self.config.weekly_max_items,
                    offset=attempt_offset,
                    sort=attempt_sort,
                )
            except Exception:
                logger.exception("Evidence retrieval failed for key=%s", evidence_query_key)
                continue
            normalized = self._normalize_results(raw, evidence_query_key)
            if normalized:
                return normalized[: self.config.weekly_max_items]
        return []

    def get_supporting_references(
        self,
        findings: list[SpecialistAgentOutput],
        *,
        scenario_id: str = "",
        simulated_date: str = "",
    ) -> list[EvidenceReference]:
        if not self.is_configured():
            return []
        keys: list[str] = []
        for finding in findings:
            key = finding.evidence_query_key
            if key and key in EVIDENCE_QUERY_MAP and key not in keys:
                keys.append(key)
            if len(keys) >= self.config.weekly_max_items:
                break
        references: list[EvidenceReference] = []
        seen_pmids: set[str] = set()
        for key in keys:
            for ref in self.get_evidence_for_key(key, scenario_id=scenario_id, simulated_date=simulated_date):
                if ref.pmid in seen_pmids:
                    continue
                seen_pmids.add(ref.pmid)
                references.append(ref)
                if len(references) >= self.config.weekly_max_items:
                    return references
        return references

    def _search_pubmed(self, query: str, max_results: int, *, offset: int = 0, sort: str = "relevance") -> list[dict[str, Any]]:
        if self.config.transport == "http":
            return self._search_http(query, max_results, offset=offset, sort=sort)
        return self._search_stdio(query, max_results)

    def _search_http(self, query: str, max_results: int, *, offset: int = 0, sort: str = "relevance") -> list[dict[str, Any]]:
        session_id = self._mcp_initialize_session()
        # MCP initialization acknowledgment notification.
        self._mcp_post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            session_id=session_id,
        )
        result = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "pubmed_search_articles",
                    "arguments": {
                        "query": query,
                        "maxResults": max_results,
                        "offset": max(offset, 0),
                        "sort": sort if sort in PUBMED_SORT_MODES else "relevance",
                        "summaryCount": max_results,
                    },
                },
            },
            session_id=session_id,
        )
        structured = result.get("result", {}).get("structuredContent", {}) if isinstance(result, dict) else {}
        summaries = structured.get("summaries", []) if isinstance(structured, dict) else []
        if isinstance(summaries, list):
            return [item for item in summaries if isinstance(item, dict)]
        return []

    def _search_stdio(self, query: str, max_results: int) -> list[dict[str, Any]]:
        payload = json.dumps(
            {
                "tool": "search_pubmed",
                "arguments": {
                    "query": query,
                    "limit": max_results,
                },
            }
        )
        process = subprocess.run(
            [self.config.mcp_command, *self.config.mcp_args],
            input=payload,
            text=True,
            capture_output=True,
            timeout=self.config.timeout_seconds,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr.strip() or f"MCP process exited with {process.returncode}")
        data = json.loads((process.stdout or "{}").strip() or "{}")
        return self._extract_result_list(data)

    def _mcp_initialize_session(self) -> str:
        response, session_id = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "ha-evidence", "version": "0.1.0"},
                    "capabilities": {},
                },
            },
            session_id=None,
            include_session_id=True,
        )
        if not session_id or "result" not in response:
            raise RuntimeError("MCP initialize did not return a valid session.")
        return session_id

    def _mcp_post(
        self,
        payload: dict[str, Any],
        *,
        session_id: str | None,
        include_session_id: bool = False,
    ) -> Any:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        req = request.Request(
            url=f"{self.config.mcp_url.rstrip('/')}/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                parsed = self._parse_mcp_response(body)
                if include_session_id:
                    return parsed, resp.headers.get("Mcp-Session-Id")
                return parsed
        except error.URLError as exc:
            raise RuntimeError(f"Evidence MCP HTTP unavailable: {exc}") from exc

    def _parse_mcp_response(self, raw: str) -> Any:
        text = (raw or "").strip()
        if not text:
            return {}
        if text.startswith("{"):
            return json.loads(text)
        # Streamable HTTP often returns SSE frames. We parse first "data:" JSON payload.
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload:
                return json.loads(payload)
        return {}

    def _extract_result_list(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(raw.get("results"), list):
            return [item for item in raw["results"] if isinstance(item, dict)]
        if isinstance(raw.get("data"), list):
            return [item for item in raw["data"] if isinstance(item, dict)]
        if isinstance(raw.get("items"), list):
            return [item for item in raw["items"] if isinstance(item, dict)]
        return []

    def _normalize_results(self, raw_results: list[dict[str, Any]], query_key: str) -> list[EvidenceReference]:
        normalized: list[EvidenceReference] = []
        for item in raw_results:
            title = str(item.get("title") or "").strip()
            pmid = str(item.get("pmid") or item.get("id") or "").strip()
            if not title or not pmid:
                continue
            journal = str(item.get("journal") or item.get("source") or "PubMed").strip()
            year_value = item.get("year")
            if year_value is None and isinstance(item.get("pubDate"), str):
                year_text = str(item.get("pubDate")).split("-")[0]
                year_value = int(year_text) if year_text.isdigit() else None
            year = int(year_value) if isinstance(year_value, (int, float)) else None
            summary = self._safe_support_line(
                str(item.get("abstract") or item.get("summary") or item.get("title") or ""),
                title=title,
            )
            normalized.append(
                EvidenceReference(
                    title=title,
                    pmid=pmid,
                    journal=journal,
                    year=year,
                    short_summary=summary,
                    query_key=query_key,  # type: ignore[arg-type]
                )
            )
        return normalized

    def _safe_support_line(self, text: str, *, title: str = "") -> str:
        cleaned = " ".join(text.split()).strip()
        title_norm = " ".join(title.split()).strip()
        if title_norm and cleaned.lower() == title_norm.lower():
            cleaned = ""
        if cleaned:
            trimmed = cleaned[:220] + ("..." if len(cleaned) > 220 else "")
            lowered = trimmed.lower()
            banned = ["proves", "guarantees", "confirms diagnosis"]
            if any(token in lowered for token in banned):
                return "Literature often links this pattern with recovery and fatigue dynamics, but individual interpretation should remain cautious."
            return f"Literature often links this pattern with fatigue/recovery dynamics: {trimmed}"
        return "This pattern is commonly associated with changes in fatigue, stress, and recovery markers."
