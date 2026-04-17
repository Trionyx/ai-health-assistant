from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from .models import (
    AuthSession,
    AuthUser,
    CheckInSession,
    Conversation,
    GPReportOutput,
    HealthSnapshot,
    MemoryItem,
    MockScenarioProfile,
    TaskItem,
    UserProfile,
    WorkflowTrace,
)


class SQLiteStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        default_root = Path(__file__).resolve().parents[2] / "data" / "ha.db"
        env_root = os.getenv("HA_DB_PATH")
        root = Path(db_path or env_root or default_root)
        root.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(root, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cursor = self.connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_health_snapshot (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gp_report (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_item (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_item (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_trace (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkin_session (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_profile (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_user (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_session (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mock_scenario_profile (
                user_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def _dump(self, model: BaseModel) -> str:
        return model.model_dump_json()

    def save_conversation(self, conversation: Conversation) -> Conversation:
        self.connection.execute(
            "INSERT OR REPLACE INTO conversation (id, payload) VALUES (?, ?)",
            (conversation.conversation_id, self._dump(conversation)),
        )
        self.connection.commit()
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self.connection.execute("SELECT payload FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
        return Conversation.model_validate_json(row["payload"]) if row else None

    def save_snapshot(self, snapshot: HealthSnapshot) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO daily_health_snapshot (id, payload) VALUES (?, ?)",
            (snapshot.snapshot_id, self._dump(snapshot)),
        )
        self.connection.commit()

    def get_recent_snapshots(
        self,
        limit: int = 7,
        kind: str | None = None,
        user_id: str | None = None,
        as_of: str | None = None,
    ) -> list[HealthSnapshot]:
        rows = self.connection.execute(
            "SELECT payload FROM daily_health_snapshot ORDER BY json_extract(payload, '$.created_at') DESC LIMIT ?",
            (limit,),
        ).fetchall()
        snapshots = [HealthSnapshot.model_validate_json(row["payload"]) for row in rows]
        if kind is None:
            by_kind = snapshots
        else:
            by_kind = [snapshot for snapshot in snapshots if snapshot.kind.value == kind]
        if user_id is None:
            by_user = by_kind
        else:
            by_user = [snapshot for snapshot in by_kind if snapshot.user_id == user_id]
        if as_of is None:
            return by_user
        return [snapshot for snapshot in by_user if (snapshot.simulated_date or snapshot.created_at.date().isoformat()) <= as_of]

    def save_report(self, report: GPReportOutput) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO gp_report (id, payload) VALUES (?, ?)",
            (report.report_id, self._dump(report)),
        )
        self.connection.execute("DELETE FROM task_item WHERE report_id = ?", (report.report_id,))
        for task in report.tasks:
            self.connection.execute(
                "INSERT OR REPLACE INTO task_item (id, report_id, payload) VALUES (?, ?, ?)",
                (task.task_id, report.report_id, json.dumps(task.model_dump(mode="json"))),
            )
        self.connection.commit()

    def get_latest_report(self, user_id: str | None = None) -> GPReportOutput | None:
        row = self.connection.execute(
            "SELECT payload FROM gp_report ORDER BY json_extract(payload, '$.created_at') DESC LIMIT 1"
        ).fetchone()
        if user_id is None:
            return GPReportOutput.model_validate_json(row["payload"]) if row else None
        rows = self.connection.execute(
            "SELECT payload FROM gp_report ORDER BY json_extract(payload, '$.created_at') DESC LIMIT 50"
        ).fetchall()
        reports = [GPReportOutput.model_validate_json(item["payload"]) for item in rows]
        return next((report for report in reports if report.user_id == user_id), None)

    def get_report(self, report_id: str) -> GPReportOutput | None:
        row = self.connection.execute("SELECT payload FROM gp_report WHERE id = ?", (report_id,)).fetchone()
        return GPReportOutput.model_validate_json(row["payload"]) if row else None

    def get_tasks(self) -> list[TaskItem]:
        rows = self.connection.execute("SELECT payload FROM task_item ORDER BY rowid DESC").fetchall()
        return [TaskItem.model_validate_json(row["payload"]) for row in rows]

    def save_memory_items(self, memory_items: list[MemoryItem]) -> list[MemoryItem]:
        for item in memory_items:
            self.connection.execute(
                "INSERT OR REPLACE INTO memory_item (id, payload) VALUES (?, ?)",
                (item.memory_id, self._dump(item)),
            )
        self.connection.commit()
        return memory_items

    def replace_memory_items_for_user(self, user_id: str, memory_items: list[MemoryItem]) -> list[MemoryItem]:
        token = f"user:{user_id}"
        self.connection.execute(
            "DELETE FROM memory_item WHERE instr(json_extract(payload, '$.source'), ?) > 0",
            (token,),
        )
        for item in memory_items:
            self.connection.execute(
                "INSERT OR REPLACE INTO memory_item (id, payload) VALUES (?, ?)",
                (item.memory_id, self._dump(item)),
            )
        self.connection.commit()
        return memory_items

    def get_memory_items(self, user_id: str | None = None) -> list[MemoryItem]:
        rows = self.connection.execute("SELECT payload FROM memory_item ORDER BY rowid DESC").fetchall()
        items = [MemoryItem.model_validate_json(row["payload"]) for row in rows]
        if user_id is None:
            return items
        return [item for item in items if item.source.endswith(f":{user_id}") or f"user:{user_id}" in item.source]

    def save_trace(self, trace: WorkflowTrace) -> WorkflowTrace:
        self.connection.execute(
            "INSERT OR REPLACE INTO workflow_trace (id, payload) VALUES (?, ?)",
            (trace.run_id, self._dump(trace)),
        )
        self.connection.commit()
        return trace

    def get_trace(self, run_id: str) -> WorkflowTrace | None:
        row = self.connection.execute("SELECT payload FROM workflow_trace WHERE id = ?", (run_id,)).fetchone()
        return WorkflowTrace.model_validate_json(row["payload"]) if row else None

    def get_latest_trace(self, user_id: str | None = None) -> WorkflowTrace | None:
        if user_id is None:
            row = self.connection.execute(
                "SELECT payload FROM workflow_trace ORDER BY json_extract(payload, '$.started_at') DESC LIMIT 1"
            ).fetchone()
            return WorkflowTrace.model_validate_json(row["payload"]) if row else None
        rows = self.connection.execute(
            "SELECT payload FROM workflow_trace ORDER BY json_extract(payload, '$.started_at') DESC LIMIT 50"
        ).fetchall()
        traces = [WorkflowTrace.model_validate_json(item["payload"]) for item in rows]
        return next((trace for trace in traces if trace.user_id == user_id), None)

    def save_checkin_session(self, session: CheckInSession) -> CheckInSession:
        self.connection.execute(
            "INSERT OR REPLACE INTO checkin_session (id, payload) VALUES (?, ?)",
            (session.session_id, self._dump(session)),
        )
        self.connection.commit()
        return session

    def get_checkin_session(self, session_id: str) -> CheckInSession | None:
        row = self.connection.execute("SELECT payload FROM checkin_session WHERE id = ?", (session_id,)).fetchone()
        return CheckInSession.model_validate_json(row["payload"]) if row else None

    def get_latest_completed_checkin(self, user_id: str) -> CheckInSession | None:
        row = self.connection.execute(
            """
            SELECT payload
            FROM checkin_session
            WHERE json_extract(payload, '$.user_id') = ?
              AND json_extract(payload, '$.status') = 'completed'
              AND json_extract(payload, '$.completed_at') IS NOT NULL
            ORDER BY json_extract(payload, '$.completed_at') DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return CheckInSession.model_validate_json(row["payload"]) if row else None

    def save_user_profile(self, profile: UserProfile) -> UserProfile:
        self.connection.execute(
            "INSERT OR REPLACE INTO user_profile (id, payload) VALUES (?, ?)",
            (profile.user_id, self._dump(profile)),
        )
        self.connection.commit()
        return profile

    def get_user_profile(self, user_id: str = "default") -> UserProfile | None:
        row = self.connection.execute("SELECT payload FROM user_profile WHERE id = ?", (user_id,)).fetchone()
        return UserProfile.model_validate_json(row["payload"]) if row else None

    def save_auth_user(self, user: AuthUser) -> AuthUser:
        self.connection.execute(
            "INSERT OR REPLACE INTO auth_user (id, email, payload) VALUES (?, ?, ?)",
            (user.user_id, user.email.lower(), self._dump(user)),
        )
        self.connection.commit()
        return user

    def get_auth_user_by_email(self, email: str) -> AuthUser | None:
        row = self.connection.execute("SELECT payload FROM auth_user WHERE email = ?", (email.lower(),)).fetchone()
        return AuthUser.model_validate_json(row["payload"]) if row else None

    def save_auth_session(self, session: AuthSession) -> AuthSession:
        self.connection.execute(
            "INSERT OR REPLACE INTO auth_session (id, user_id, payload) VALUES (?, ?, ?)",
            (session.session_id, session.user_id, self._dump(session)),
        )
        self.connection.commit()
        return session

    def get_auth_session(self, session_id: str) -> AuthSession | None:
        row = self.connection.execute("SELECT payload FROM auth_session WHERE id = ?", (session_id,)).fetchone()
        return AuthSession.model_validate_json(row["payload"]) if row else None

    def delete_auth_session(self, session_id: str) -> None:
        self.connection.execute("DELETE FROM auth_session WHERE id = ?", (session_id,))
        self.connection.commit()

    def save_mock_scenario_profile(self, profile: MockScenarioProfile) -> MockScenarioProfile:
        self.connection.execute(
            "INSERT OR REPLACE INTO mock_scenario_profile (user_id, payload) VALUES (?, ?)",
            (profile.user_id, self._dump(profile)),
        )
        self.connection.commit()
        return profile

    def get_mock_scenario_profile(self, user_id: str) -> MockScenarioProfile | None:
        row = self.connection.execute(
            "SELECT payload FROM mock_scenario_profile WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return MockScenarioProfile.model_validate_json(row["payload"]) if row else None
