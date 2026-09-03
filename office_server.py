#!/usr/bin/env python3
"""Local The Office server: serves the UI and runs Claude/Codex workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from office_backend.agents import adapter_for
from office_backend.db import open_database
from office_backend.git_ops import split_diff_files
from office_backend.http import resolve_ui_asset
from office_backend.repos import normalized_path_claims, path_claims_overlap, scope_path_claims


ROOT = Path(__file__).resolve().parent
MAX_BODY = 1_000_000
MAX_STATE_BODY = 20_000_000
SUPPORTED_RUN_TYPES = (
    "work", "chat", "question", "onboard", "plan", "review", "orchestrate",
    "report", "reception", "floor_call", "floor_intent",
)
MAX_LOG_LINES = 5000
MAX_DIFF_BYTES = 4_000_000
MAX_FILE_CONTEXT_BYTES = 64_000
MAX_MCP_SERVERS = 20
PERSISTENT_RUN_TYPES = frozenset({"work", "plan", "question", "chat", "orchestrate", "review", "floor_call"})
LIGHTWEIGHT_RUN_TYPES = frozenset({"reception", "plan", "review", "floor_call", "floor_intent"})
ACTIVE_RUN_STATUSES = frozenset({"starting", "running", "waiting_for_lock", "awaiting_approval"})
lock = threading.RLock()
clone_lock = threading.Lock()
path_lock_condition = threading.Condition(threading.RLock())
active_path_claims: list[tuple[str, str, str]] = []
runs: dict[str, dict] = {}
shell_jobs: dict[str, dict] = {}
database_lock = threading.RLock()
database: sqlite3.Connection | None = None


def office_data_directory() -> Path:
    override = os.environ.get("TASK_OFFICE_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return (root / "the-office").resolve()


DATA_DIRECTORY = office_data_directory()
DATABASE_PATH = DATA_DIRECTORY / "office.db"
MAX_RUN_HISTORY = max(20, int(os.environ.get("TASK_OFFICE_RUN_HISTORY", "200")))


class StateConflictError(ValueError):
    pass


def initialize_database() -> None:
    global database
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    try:
        DATA_DIRECTORY.chmod(0o700)
    except OSError:
        pass
    existed = DATABASE_PATH.exists() and DATABASE_PATH.stat().st_size > 0
    connection = open_database(DATABASE_PATH)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS office_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS state_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            task TEXT NOT NULL,
            run_type TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            returncode INTEGER,
            result_json TEXT,
            session_id TEXT,
            final_message TEXT,
            error_message TEXT,
            activity_phase TEXT,
            activity_label TEXT,
            activity_updated_at INTEGER,
            checkpoint_ref TEXT,
            completion_ref TEXT,
            path_claims_json TEXT,
            repository_json TEXT,
            prompt TEXT,
            retry_of INTEGER,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS agent_runs_profile_started
            ON agent_runs(profile_id, started_at DESC);
        CREATE TABLE IF NOT EXISTS run_logs (
            run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            time INTEGER NOT NULL,
            stream TEXT NOT NULL,
            text TEXT NOT NULL,
            PRIMARY KEY (run_id, sequence)
        );
        """
    )
    # SQLite has no IF NOT EXISTS form for ADD COLUMN. Keep upgrades from older
    # office databases additive and idempotent.
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()
    }
    migrations = {
        "checkpoint_ref": "TEXT",
        "completion_ref": "TEXT",
        "path_claims_json": "TEXT",
        "repository_json": "TEXT",
        "prompt": "TEXT",
        "retry_of": "INTEGER",
        "input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "output_tokens": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in migrations.items():
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE agent_runs ADD COLUMN {column} {definition}")
    try:
        DATABASE_PATH.chmod(0o600)
    except OSError:
        pass
    with database_lock:
        database = connection
        interrupted_at = now_ms()
        connection.execute(
            """UPDATE agent_runs
               SET status = 'interrupted', ended_at = COALESCE(ended_at, ?),
                   error_message = COALESCE(error_message, 'The Office server restarted before this run finished.'),
                   activity_phase = 'failed', activity_label = 'Interrupted by server restart',
                   activity_updated_at = ?
               WHERE status IN ('starting', 'running', 'waiting_for_lock', 'awaiting_approval')""",
            (interrupted_at, interrupted_at),
        )
        connection.commit()
    if existed:
        create_database_backup()


def require_database() -> sqlite3.Connection:
    if database is None:
        raise RuntimeError("The Office database is not initialized.")
    return database


def create_database_backup() -> Path:
    backup_directory = DATA_DIRECTORY / "backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    try:
        backup_directory.chmod(0o700)
    except OSError:
        pass
    backup_path = backup_directory / f"office-{time.strftime('%Y%m%d-%H%M%S')}-{now_ms()%1000:03d}.db"
    with database_lock:
        source = require_database()
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    backups = sorted(backup_directory.glob("office-*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in backups[10:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return backup_path


def read_office_state() -> dict:
    with database_lock:
        row = require_database().execute(
            "SELECT schema_version, revision, state_json, updated_at FROM office_state WHERE singleton = 1"
        ).fetchone()
    if not row:
        return {"exists": False, "revision": 0, "state": None, "updatedAt": None}
    return {
        "exists": True,
        "schemaVersion": row["schema_version"],
        "revision": row["revision"],
        "state": json.loads(row["state_json"]),
        "updatedAt": row["updated_at"],
    }


def write_office_state(payload: dict) -> dict:
    state = payload.get("state")
    if not isinstance(state, dict):
        raise ValueError("State must be a JSON object.")
    schema_version = state.get("schemaVersion")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise ValueError("State must include a valid schemaVersion.")
    expected_revision = payload.get("expectedRevision", 0)
    if not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError("expectedRevision must be a non-negative integer.")
    encoded = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode()) > MAX_STATE_BODY:
        raise ValueError("Office state is too large to store.")
    updated_at = now_ms()
    with database_lock:
        connection = require_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute(
                "SELECT schema_version, revision, state_json, updated_at FROM office_state WHERE singleton = 1"
            ).fetchone()
            current_revision = current["revision"] if current else 0
            if expected_revision != current_revision:
                raise StateConflictError(
                    f"Office state changed in another tab (expected revision {expected_revision}, current revision {current_revision})."
                )
            last_snapshot = connection.execute(
                "SELECT MAX(created_at) AS created_at FROM state_snapshots"
            ).fetchone()["created_at"]
            if current and (last_snapshot is None or updated_at - last_snapshot >= 300_000):
                connection.execute(
                    "INSERT INTO state_snapshots(revision, schema_version, state_json, created_at) VALUES (?, ?, ?, ?)",
                    (current["revision"], current["schema_version"], current["state_json"], updated_at),
                )
                connection.execute(
                    "DELETE FROM state_snapshots WHERE id NOT IN (SELECT id FROM state_snapshots ORDER BY id DESC LIMIT 20)"
                )
            revision = current_revision + 1
            connection.execute(
                """INSERT INTO office_state(singleton, schema_version, revision, state_json, updated_at)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                       schema_version=excluded.schema_version,
                       revision=excluded.revision,
                       state_json=excluded.state_json,
                       updated_at=excluded.updated_at""",
                (schema_version, revision, encoded, updated_at),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"ok": True, "revision": revision, "schemaVersion": schema_version, "updatedAt": updated_at}

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reason", "workstreams", "floor_calls"],
    "properties": {
        "decision": {"type": "string", "enum": ["single", "multi"]},
        "reason": {"type": "string"},
        "workstreams": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "note", "repositories"],
                "properties": {
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "repositories": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                    "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 200},
                },
            },
        },
        "floor_calls": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["floor_id", "question", "reason"],
                "properties": {
                    "floor_id": {"type": "string"},
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

RECEPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reason", "routes"],
    "properties": {
        "reason": {"type": "string"},
        "routes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["floor_id", "title", "note", "role", "reason"],
                "properties": {
                    "floor_id": {"type": "string"},
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "role": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

FLOOR_CALL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "considerations", "relevant_files"],
    "properties": {
        "answer": {"type": "string"},
        "considerations": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "relevant_files": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
    },
}

FLOOR_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["floors", "unresolved"],
    "properties": {
        "floors": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["raw_path_or_url", "agent", "lead_name", "floor_name"],
                "properties": {
                    "raw_path_or_url": {"type": "string"},
                    "agent": {"type": ["string", "null"], "enum": ["claude", "codex", None]},
                    "lead_name": {"type": ["string", "null"]},
                    "floor_name": {"type": ["string", "null"]},
                },
            },
        },
        "unresolved": {"type": ["string", "null"]},
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ready", "summary", "issues", "suggested_branch", "pr_title", "pr_body", "repositories"],
    "properties": {
        "ready": {"type": "boolean"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "suggested_branch": {"type": "string"},
        "pr_title": {"type": "string"},
        "pr_body": {"type": "string"},
        "repositories": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
    },
}

REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "executive_summary", "recommendation", "implementation_steps", "risks", "relevant_files"],
    "properties": {
        "title": {"type": "string"},
        "executive_summary": {"type": "string"},
        "recommendation": {"type": "string"},
        "implementation_steps": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "relevant_files": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
    },
}

ONBOARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "architecture", "conventions", "test_commands", "risk_areas", "key_files", "repositories"],
    "properties": {
        "summary": {"type": "string"},
        "architecture": {"type": "string"},
        "conventions": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "test_commands": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "risk_areas": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "key_files": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
        "repositories": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "summary", "architecture", "conventions", "test_commands", "risk_areas", "key_files"],
                "properties": {
                    "path": {"type": "string"},
                    "summary": {"type": "string"},
                    "architecture": {"type": "string"},
                    "conventions": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                    "test_commands": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                    "risk_areas": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                    "key_files": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                },
            },
        },
    },
}


def cli_search_dirs() -> list[Path]:
    home = Path.home()
    directories = [
        home / ".local" / "bin",
        home / "bin",
        home / ".npm-global" / "bin",
        home / ".volta" / "bin",
        home / ".bun" / "bin",
    ]
    directories.extend(sorted((home / ".nvm" / "versions" / "node").glob("*/bin"), reverse=True))
    return directories


def find_cli(name: str) -> str | None:
    override = os.environ.get(f"TASK_OFFICE_{name.upper()}_BIN")
    candidates = [Path(override).expanduser()] if override else []
    located = shutil.which(name)
    if located:
        candidates.append(Path(located))
    candidates.extend(directory / name for directory in cli_search_dirs())
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def agent_environment() -> dict[str, str]:
    extra_path = os.pathsep.join(str(path) for path in cli_search_dirs())
    return {
        **os.environ,
        "PATH": extra_path + os.pathsep + os.environ.get("PATH", ""),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def now_ms() -> int:
    return int(time.time() * 1000)


HOOK_EVENTS = frozenset({"on_run_finished", "on_run_blocked", "on_review_ready"})


def emit_lifecycle_hook(event: str, payload: dict, run: dict | None = None) -> None:
    if event not in HOOK_EVENTS:
        raise ValueError("Unsupported lifecycle hook event.")
    configured = os.environ.get("TASK_OFFICE_HOOKS_DIR", "").strip()
    if not configured:
        return
    directory = Path(configured).expanduser().resolve()
    script = directory / event
    if not directory.is_dir() or script.parent != directory or not script.is_file() or not os.access(script, os.X_OK):
        if run:
            append_log(run, "hook", f"Skipped {event}: no executable hook at {script}")
        return
    event_payload = {"event": event, "time": now_ms(), **payload}
    try:
        result = subprocess.run(
            [str(script)], input=json.dumps(event_payload), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15, check=False,
            env=agent_environment(),
        )
        if run and result.stdout.strip():
            append_log(run, "hook", result.stdout.strip()[:4000])
        if run and result.returncode:
            append_log(run, "hook", f"{event} exited with code {result.returncode}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        if run:
            append_log(run, "hook", f"{event} failed: {exc}")


def persist_run_started(run: dict) -> None:
    if database is None:
        return
    with database_lock:
        connection = require_database()
        cursor = connection.execute(
            """INSERT INTO agent_runs(
                   profile_id, agent, task, run_type, status, started_at, session_id,
                   activity_phase, activity_label, activity_updated_at, checkpoint_ref,
                   completion_ref, path_claims_json, repository_json, prompt, retry_of, input_tokens, output_tokens
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run["profileId"], run["agent"], run["task"], run["runType"], run["status"],
                run["startedAt"], run.get("sessionId"), run.get("activityPhase"),
                run.get("activityLabel"), run.get("activityUpdatedAt"), run.get("checkpointRef"),
                run.get("completionRef"), json.dumps(run.get("requestedPathClaims") or []),
                json.dumps(run.get("repository") or {}, ensure_ascii=False), run.get("prompt"),
                run.get("retryOf"), run.get("inputTokens", 0), run.get("outputTokens", 0),
            ),
        )
        run["databaseRunId"] = cursor.lastrowid
        connection.execute(
            "DELETE FROM agent_runs WHERE id NOT IN (SELECT id FROM agent_runs ORDER BY started_at DESC LIMIT ?)",
            (MAX_RUN_HISTORY,),
        )
        connection.commit()


def persist_run_log(run: dict, entry: dict) -> None:
    run_id = run.get("databaseRunId")
    if database is None or not run_id:
        return
    with database_lock:
        connection = require_database()
        connection.execute(
            "INSERT OR REPLACE INTO run_logs(run_id, sequence, time, stream, text) VALUES (?, ?, ?, ?, ?)",
            (run_id, entry["sequence"], entry["time"], entry["stream"], entry["text"]),
        )
        connection.execute(
            "DELETE FROM run_logs WHERE run_id=? AND sequence<=?",
            (run_id, entry["sequence"] - MAX_LOG_LINES),
        )
        connection.commit()


def persist_run_status(run: dict) -> None:
    run_id = run.get("databaseRunId")
    if database is None or not run_id:
        return
    result = json.dumps(run.get("result"), ensure_ascii=False) if run.get("result") is not None else None
    with database_lock:
        connection = require_database()
        connection.execute(
            """UPDATE agent_runs SET
                   status=?, ended_at=?, returncode=?, result_json=?, session_id=?,
                   final_message=?, error_message=?, activity_phase=?, activity_label=?, activity_updated_at=?,
                   checkpoint_ref=?, completion_ref=?, input_tokens=?, output_tokens=?
               WHERE id=?""",
            (
                run["status"], run.get("endedAt"), run.get("returncode"), result,
                run.get("sessionId"), run.get("finalMessage"), run.get("errorMessage"),
                run.get("activityPhase"), run.get("activityLabel"), run.get("activityUpdatedAt"),
                run.get("checkpointRef"), run.get("completionRef"), run.get("inputTokens", 0), run.get("outputTokens", 0), run_id,
            ),
        )
        connection.commit()


def persisted_run(profile_id: str, since: int = 0) -> dict | None:
    with database_lock:
        connection = require_database()
        row = connection.execute(
            "SELECT * FROM agent_runs WHERE profile_id=? ORDER BY started_at DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
        if not row:
            return None
        log_rows = connection.execute(
            "SELECT sequence, time, stream, text FROM run_logs WHERE run_id=? AND sequence>? ORDER BY sequence",
            (row["id"], since),
        ).fetchall()
        sequence_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM run_logs WHERE run_id=?",
            (row["id"],),
        ).fetchone()
    session_input, session_output = session_token_usage(row["session_id"])
    repository_spec = json.loads(row["repository_json"] or "{}")
    return {
        "profileId": row["profile_id"],
        "agent": row["agent"],
        "task": row["task"],
        "status": "failed" if row["status"] == "interrupted" else row["status"],
        "persistedStatus": row["status"],
        "pid": None,
        "startedAt": row["started_at"],
        "endedAt": row["ended_at"],
        "returncode": row["returncode"],
        "runType": row["run_type"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "sessionId": row["session_id"],
        "finalMessage": row["final_message"],
        "errorMessage": row["error_message"],
        "activityPhase": row["activity_phase"] or "idle",
        "activityLabel": row["activity_label"] or "Available",
        "activityUpdatedAt": row["activity_updated_at"],
        "databaseRunId": row["id"],
        "checkpointRef": row["checkpoint_ref"],
        "completionRef": row["completion_ref"],
        "pathClaims": [
            {"repository": repository, "path": path}
            for repository, path in json.loads(row["path_claims_json"] or "[]")
        ],
        "inputTokens": row["input_tokens"] or 0,
        "outputTokens": row["output_tokens"] or 0,
        "sessionInputTokens": session_input,
        "sessionOutputTokens": session_output,
        "sessionTurns": session_turn_count(row["session_id"]),
        "approvalCategories": permission_categories(repository_spec, row["run_type"]),
        "logs": [dict(item) for item in log_rows],
        "sequence": sequence_row["sequence"],
        "persisted": True,
    }


def persisted_run_history(limit: int = 50) -> dict:
    limit = max(1, min(limit, 200))
    with database_lock:
        rows = require_database().execute(
            """SELECT id, profile_id, agent, task, run_type, status, started_at, ended_at,
                      returncode, session_id, error_message, checkpoint_ref, completion_ref, input_tokens, output_tokens
               FROM agent_runs ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {"runs": [{
        "id": row["id"], "profileId": row["profile_id"], "agent": row["agent"],
        "task": row["task"], "runType": row["run_type"], "status": row["status"],
        "startedAt": row["started_at"], "endedAt": row["ended_at"],
        "returncode": row["returncode"], "sessionId": row["session_id"],
        "errorMessage": row["error_message"], "checkpointRef": row["checkpoint_ref"],
        "completionRef": row["completion_ref"],
        "inputTokens": row["input_tokens"] or 0, "outputTokens": row["output_tokens"] or 0,
    } for row in rows]}


def latest_session_id(profile_id: str, agent: str) -> str | None:
    if database is None:
        return None
    with database_lock:
        row = require_database().execute(
            """SELECT session_id FROM agent_runs
               WHERE profile_id=? AND agent=? AND session_id IS NOT NULL AND session_id != ''
               ORDER BY started_at DESC LIMIT 1""",
            (profile_id, agent),
        ).fetchone()
    return str(row["session_id"]) if row else None


def session_token_usage(session_id: str | None, exclude_run_id: int | None = None) -> tuple[int, int]:
    if database is None or not session_id:
        return 0, 0
    query = "SELECT COALESCE(SUM(input_tokens),0) AS input, COALESCE(SUM(output_tokens),0) AS output FROM agent_runs WHERE session_id=?"
    parameters: list[object] = [session_id]
    if exclude_run_id:
        query += " AND id != ?"
        parameters.append(exclude_run_id)
    with database_lock:
        row = require_database().execute(query, parameters).fetchone()
    return int(row["input"]), int(row["output"])


def session_turn_count(session_id: str | None, exclude_run_id: int | None = None) -> int:
    if database is None or not session_id:
        return 0
    query = "SELECT COUNT(*) AS turns FROM agent_runs WHERE session_id=?"
    parameters: list[object] = [session_id]
    if exclude_run_id:
        query += " AND id != ?"
        parameters.append(exclude_run_id)
    with database_lock:
        row = require_database().execute(query, parameters).fetchone()
    return int(row["turns"])


def normalized_mcp_servers(spec: dict) -> list[dict]:
    raw_servers = spec.get("mcpServers") or []
    if not isinstance(raw_servers, list) or len(raw_servers) > MAX_MCP_SERVERS:
        raise ValueError(f"mcpServers must be a list of at most {MAX_MCP_SERVERS} entries.")
    servers = []
    seen = set()
    for raw in raw_servers:
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        name = str(raw.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in seen:
            raise ValueError("Each enabled MCP server needs a unique alphanumeric name (hyphens and underscores allowed).")
        server_type = str(raw.get("type", "stdio")).strip().lower()
        item = {"name": name, "type": server_type}
        if server_type == "stdio":
            command = str(raw.get("command", "")).strip()
            if not command:
                raise ValueError(f"MCP server {name} needs a command.")
            item["command"] = command
            args = raw.get("args") or []
            if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
                raise ValueError(f"MCP server {name} args must be a list of strings.")
            item["args"] = args
        elif server_type in ("http", "sse"):
            url = str(raw.get("url", "")).strip()
            if not re.match(r"^https?://", url):
                raise ValueError(f"MCP server {name} needs an http(s) URL.")
            item["url"] = url
        else:
            raise ValueError(f"MCP server {name} type must be stdio, http, or sse.")
        env = raw.get("env") or {}
        if not isinstance(env, dict) or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key or "") and isinstance(value, str) for key, value in env.items()):
            raise ValueError(f"MCP server {name} env must map variable names to strings.")
        if env:
            item["env"] = env
        for source, target in (("enabledTools", "enabledTools"), ("disabledTools", "disabledTools")):
            tools = raw.get(source) or []
            if not isinstance(tools, list) or not all(isinstance(value, str) for value in tools):
                raise ValueError(f"MCP server {name} {source} must be a list of strings.")
            if tools:
                item[target] = tools
        approval = str(raw.get("approvalMode", "")).strip()
        if approval:
            if approval not in ("auto", "prompt", "writes", "approve"):
                raise ValueError(f"MCP server {name} has an unsupported approval mode.")
            item["approvalMode"] = approval
        seen.add(name)
        servers.append(item)
    return servers


def normalized_plugin_paths(spec: dict) -> list[str]:
    values = spec.get("pluginPaths") or []
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError("pluginPaths must be a list of at most 20 directories.")
    paths = []
    for path in bundled_plugin_paths() + configured_office_plugin_paths():
        if path not in paths:
            paths.append(path)
    for value in values:
        path = Path(str(value)).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(f"Plugin directory is not an accessible absolute directory: {value}")
        resolved = str(path.resolve())
        if resolved not in paths:
            paths.append(resolved)
    return paths


def bundled_plugin_paths() -> list[str]:
    """Every locally bundled Office plugin is enabled for every floor."""
    root = (ROOT / "the-office-plugins" / "plugins").resolve()
    if not root.is_dir():
        return []
    return [
        str(path.parent.parent.resolve())
        for path in sorted(root.glob("*/.claude-plugin/plugin.json"))
        if path.is_file()
    ]


def bundled_plugins() -> list[dict]:
    result = []
    for value in bundled_plugin_paths():
        path = Path(value)
        try:
            manifest = json.loads((path / ".claude-plugin" / "plugin.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        result.append({"name": str(manifest.get("name") or path.name), "path": str(path), "description": str(manifest.get("description") or "")})
    return result


def configured_office_plugin_paths() -> list[str]:
    """Share valid custom plugin directories configured on any saved floor."""
    try:
        state = read_office_state().get("state") or {}
    except Exception:
        return []
    paths = []
    for floor in state.get("floors") or []:
        if not isinstance(floor, dict):
            continue
        for value in floor.get("pluginPaths") or []:
            path = Path(str(value)).expanduser()
            if path.is_absolute() and path.is_dir():
                resolved = str(path.resolve())
                if resolved not in paths:
                    paths.append(resolved)
    return paths


def permission_categories(spec: dict, run_type: str) -> list[str]:
    if run_type not in ("work", "orchestrate"):
        return []
    permissions = spec.get("permissions") or {}
    values = permissions.get("requireConfirmationFor") or [] if isinstance(permissions, dict) else []
    allowed = {"file_edits", "shell_commands", "network_access", "external_tools"}
    categories = {str(value) for value in values if str(value) in allowed}
    if isinstance(permissions, dict) and permissions.get("autoApproveEdits") is False:
        categories.add("file_edits")
    return sorted(categories)


def acquire_path_claims(owner: str, claims: list[tuple[str, str]], run: dict) -> bool:
    if not claims:
        return True
    with path_lock_condition:
        while any(path_claims_overlap(claim, (repo, path)) for claim in claims for held_owner, repo, path in active_path_claims if held_owner != owner):
            if run.get("status") in ("denied", "failed"):
                return False
            set_activity(run, "queued", "Waiting for overlapping file ownership")
            path_lock_condition.wait(timeout=2)
        active_path_claims.extend((owner, repository, path) for repository, path in claims)
        return True


def release_path_claims(owner: str) -> None:
    with path_lock_condition:
        active_path_claims[:] = [claim for claim in active_path_claims if claim[0] != owner]
        path_lock_condition.notify_all()


def toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ",".join(toml_literal(item) for item in value) + "]"
    return json.dumps(str(value))


def claude_mcp_config(servers: list[dict]) -> dict:
    result = {}
    for server in servers:
        value = {key: server[key] for key in ("command", "args", "url", "env") if key in server}
        if server["type"] in ("http", "sse"):
            value["type"] = server["type"]
        result[server["name"]] = value
    return {"mcpServers": result}


def claude_plugin_suggestion_settings() -> dict:
    marketplace = (ROOT / "the-office-plugins").resolve()
    return {
        "spinnerTipsEnabled": True,
        "extraKnownMarketplaces": {
            "the-office-plugins": {"source": {"source": "directory", "path": str(marketplace)}}
        },
    }


def add_codex_mcp_overrides(command: list[str], servers: list[dict]) -> None:
    for server in servers:
        prefix = f"mcp_servers.{server['name']}"
        mappings = {
            "command": "command", "args": "args", "url": "url", "env": "env",
            "enabledTools": "enabled_tools", "disabledTools": "disabled_tools",
            "approvalMode": "default_tools_approval_mode",
        }
        for source, target in mappings.items():
            if source not in server:
                continue
            value = server[source]
            if source == "env":
                for env_name, env_value in value.items():
                    command += ["-c", f"{prefix}.env.{env_name}={toml_literal(env_value)}"]
            else:
                command += ["-c", f"{prefix}.{target}={toml_literal(value)}"]


def append_log(run: dict, stream: str, text: str) -> None:
    with lock:
        run["sequence"] += 1
        entry = {
            "sequence": run["sequence"],
            "time": now_ms(),
            "stream": stream,
            "text": text.rstrip("\n"),
        }
        run["logs"].append(entry)
    persist_run_log(run, entry)


def set_activity(run: dict, phase: str, label: str) -> None:
    with lock:
        run["activityPhase"] = phase
        run["activityLabel"] = label
        run["activityUpdatedAt"] = now_ms()


def command_activity(command: object) -> tuple[str, str]:
    text = " ".join(str(part) for part in command) if isinstance(command, list) else str(command or "")
    lowered = text.lower()
    test_patterns = (
        "pytest", "npm test", "npm run test", "pnpm test", "yarn test", "bun test",
        "go test", "cargo test", "mvn test", "gradle test", "gradlew test", "rspec",
        "jest", "vitest", "phpunit", "dotnet test", "npm run lint", "pnpm lint",
        "yarn lint", "eslint", "ruff check", "pylint", "golangci-lint",
    )
    if any(pattern in lowered for pattern in test_patterns):
        return "testing", "Running tests"
    build_patterns = (
        "npm run build", "pnpm build", "yarn build", "bun run build", "cargo build",
        "go build", "mvn package", "gradle build", "gradlew build", "dotnet build", "tsc",
    )
    if any(pattern in lowered for pattern in build_patterns):
        return "building", "Building project"
    if re.search(r"(^|\s)git\s+(diff|status|show|log)\b", lowered):
        return "reviewing", "Reviewing changes"
    return "command", "Running a command"


def verification_counts(output: object) -> dict:
    text = str(output or "")[-100_000:]
    result = {}
    patterns = {
        "passed": (r"(?i)\b(\d+)\s+passed\b", r"(?i)\btests?:\s*(\d+)\s+passed\b"),
        "failed": (r"(?i)\b(\d+)\s+failed\b", r"(?i)\btests?:.*?\b(\d+)\s+failed\b"),
        "skipped": (r"(?i)\b(\d+)\s+skipped\b",),
    }
    for name, choices in patterns.items():
        for pattern in choices:
            match = re.search(pattern, text)
            if match:
                result[name] = int(match.group(1))
                break
    return result


def update_activity_from_event(run: dict, event: dict) -> None:
    activity = adapter_for(run["agent"]).parse_activity(event, command_activity)
    if activity:
        set_activity(run, *activity)
    item = event.get("item") or {}
    if event.get("type") == "item.completed" and item.get("type") == "command_execution":
        phase, _label = command_activity(item.get("command"))
        if phase in ("testing", "building"):
            run.setdefault("verification", []).append({
                "kind": phase, "command": item.get("command"), "exitCode": item.get("exit_code"),
                "counts": verification_counts(item.get("aggregated_output") or item.get("output")),
            })


def repo_path(spec: dict, run: dict) -> Path:
    mode = spec.get("mode")
    if mode == "office":
        return ROOT
    if mode == "local":
        path = Path(str(spec.get("path", ""))).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("The configured local repository must be an existing absolute directory.")
        return path.resolve()

    if mode != "clone":
        raise ValueError("Repository mode must be local or clone.")
    url = str(spec.get("url", "")).strip()
    destination = Path(str(spec.get("destination", ""))).expanduser()
    if not url or not destination.is_absolute():
        raise ValueError("Cloning requires a Git URL and an absolute destination directory.")
    destination = destination.resolve()
    with clone_lock:
        if destination.exists():
            if not destination.is_dir():
                raise ValueError("The clone destination exists and is not a directory.")
            if (destination / ".git").exists():
                return destination
            if any(destination.iterdir()):
                raise ValueError("The clone destination already exists, is not empty, and is not a Git repository.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        git = shutil.which("git")
        if not git:
            raise RuntimeError("git is not installed or is not on PATH.")
        append_log(run, "system", f"Cloning {url} into {destination}")
        result = subprocess.run(
            [git, "clone", "--", url, str(destination)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
            check=False,
        )
        for line in result.stdout.splitlines():
            append_log(run, "git", line)
        if result.returncode:
            raise RuntimeError(f"git clone exited with code {result.returncode}")
    return destination


def existing_repo_path(spec: dict) -> Path:
    raw = spec.get("path") if spec.get("mode") == "local" else spec.get("destination")
    path = Path(str(raw or "")).expanduser()
    if not path.is_absolute() or not path.is_dir() or not (path / ".git").exists():
        raise ValueError("Publishing requires an existing absolute Git repository directory.")
    return path.resolve()


IGNORED_CUPBOARD_DIRS = {
    ".cache", ".idea", ".tox", ".venv", "__pycache__", "build", "dist",
    "node_modules", "target", "vendor",
}


def discover_git_repositories(root: Path, max_depth: int = 6) -> list[Path]:
    """Find Git working trees beneath a cupboard without traversing bulky generated trees."""
    root = root.resolve()
    found: list[Path] = []
    for current, directory_names, _files in os.walk(root, followlinks=False):
        path = Path(current)
        try:
            depth = len(path.relative_to(root).parts)
        except ValueError:
            continue
        directory_names[:] = [
            name for name in directory_names
            if name != ".git" and name not in IGNORED_CUPBOARD_DIRS and not name.startswith(".")
        ]
        if (path / ".git").exists():
            found.append(path)
            if len(found) >= 100:
                break
        if depth >= max_depth:
            directory_names[:] = []
    return sorted(set(found), key=lambda value: str(value.relative_to(root)).lower())


GIT_URL_HOST_PATTERN = re.compile(r"(github\.com|gitlab\.com|bitbucket\.org)", re.IGNORECASE)
MAX_FLOOR_INTENT_ITEMS = 20


def looks_like_git_url(raw: str) -> bool:
    """True when a raw floor-intent string is shaped like a clone target, not a local path."""
    raw = raw.strip()
    if not raw:
        return False
    if re.match(r"^\w+://", raw):
        return True
    if re.match(r"^[\w.-]+@[\w.-]+:", raw):  # scp-style, e.g. git@github.com:org/repo.git
        return True
    return bool(GIT_URL_HOST_PATTERN.search(raw))


def resolve_floor_intent(raw_path_or_url: str) -> dict:
    """Deterministically resolve a floor-intent path/URL fragment to a repo shape.

    Never asks the model to decide local/clone/cupboard mode (spec 10.2) — this is a
    plain filesystem inspection, reusing discover_git_repositories() for cupboard hits.
    """
    raw = str(raw_path_or_url or "").strip()
    if not raw:
        return {"mode": "unresolved", "raw": raw, "summary": "No path or URL was mentioned."}
    if looks_like_git_url(raw):
        return {"mode": "clone", "raw": raw, "url": raw, "summary": f"Clone `{raw}`"}
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    exists = path.exists()
    resolved = path.resolve() if exists else path
    if exists and resolved.is_dir():
        if (resolved / ".git").exists():
            return {
                "mode": "local", "raw": raw, "resolved_path": str(resolved),
                "summary": f"Found a git repo at `{resolved}`",
            }
        repositories = discover_git_repositories(resolved)
        if repositories:
            return {
                "mode": "cupboard", "raw": raw, "resolved_path": str(resolved),
                "repositories": [str(item) for item in repositories],
                "summary": f"Found {len(repositories)} git repo(s) under `{resolved}` — treating it as a cupboard",
            }
        return {
            "mode": "ambiguous", "raw": raw, "resolved_path": str(resolved), "repositories": [],
            "summary": f"`{resolved}` has no git repos in it — is this a cupboard root, or a different path?",
        }
    return {
        "mode": "unresolved", "raw": raw,
        "summary": f"Couldn't find `{raw}` on disk — which folder did you mean?",
    }


def floor_intent_resolution(data: dict) -> dict:
    """Apply resolve_floor_intent() per floor over a parsed FLOOR_INTENT_SCHEMA payload.

    Handles the 10.5 multi-floor case: a single prompt may name several floors, so the
    response is always an array of resolved entries, one per floor in the input list.
    """
    floors = data.get("floors")
    if not isinstance(floors, list) or not floors:
        raise ValueError("floors must be a non-empty list.")
    if len(floors) > MAX_FLOOR_INTENT_ITEMS:
        raise ValueError(f"At most {MAX_FLOOR_INTENT_ITEMS} floors can be resolved at once.")
    resolved = []
    for item in floors:
        if not isinstance(item, dict):
            raise ValueError("Each floor entry must be an object.")
        raw = item.get("raw_path_or_url")
        resolved.append({
            "raw_path_or_url": raw,
            "agent": item.get("agent"),
            "lead_name": item.get("lead_name"),
            "floor_name": item.get("floor_name"),
            "resolution": resolve_floor_intent(str(raw) if raw is not None else ""),
        })
    return {"unresolved": data.get("unresolved"), "floors": resolved}


def cupboard_repositories(data: dict) -> tuple[Path, list[Path]]:
    raw = str(data.get("path", "")).strip()
    root = Path(raw).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("A cupboard requires an existing absolute folder.")
    root = root.resolve()
    return root, discover_git_repositories(root)


def cupboard_manifest(data: dict) -> dict:
    root, repositories = cupboard_repositories(data)
    return {
        "path": str(root),
        "repositories": [
            {
                "name": repository.name,
                "path": str(repository),
                "relativePath": "." if repository == root else str(repository.relative_to(root)),
            }
            for repository in repositories
        ],
    }


def checked_command(command: list[str], cwd: Path, timeout: int = 300) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        env=agent_environment(),
    )
    if result.returncode:
        raise RuntimeError(f"{' '.join(command[:2])} failed:\n{result.stdout.strip()}")
    return result.stdout.strip()


def git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("git is not installed or is not on PATH.")
    return executable


def git_repositories_for_spec(spec: dict) -> list[Path]:
    """Resolve the Git repositories affected by a run without cloning anything."""
    if spec.get("mode") != "local":
        path = existing_repo_path(spec)
        return [path]
    path = Path(str(spec.get("path", ""))).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("The configured local repository must be an existing absolute directory.")
    path = path.resolve()
    if (path / ".git").exists() and not spec.get("allowNonGit"):
        return [path]
    repositories = discover_git_repositories(path)
    if not repositories:
        raise ValueError(f"No Git repositories were found under {path}.")
    return repositories


def create_repository_checkpoint(repository: Path, run_id: int, kind: str = "checkpoint") -> dict:
    """Create a durable commit object containing the current non-ignored worktree."""
    git = git_executable()
    with tempfile.NamedTemporaryFile(prefix="office-index-", delete=False) as index_file:
        index_path = index_file.name
    try:
        os.unlink(index_path)
        environment = {
            **agent_environment(),
            "GIT_INDEX_FILE": index_path,
            "GIT_AUTHOR_NAME": "The Office",
            "GIT_AUTHOR_EMAIL": "the-office@localhost",
            "GIT_COMMITTER_NAME": "The Office",
            "GIT_COMMITTER_EMAIL": "the-office@localhost",
        }
        head = subprocess.run(
            [git, "rev-parse", "--verify", "HEAD"], cwd=repository, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, check=False,
        )
        parent = head.stdout.strip() if head.returncode == 0 else ""
        if parent:
            checked = subprocess.run([git, "read-tree", parent], cwd=repository, env=environment, check=False)
            if checked.returncode:
                raise RuntimeError(f"Could not initialize a checkpoint for {repository}.")
        add = subprocess.run(
            [git, "add", "-A", "--", "."], cwd=repository, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        if add.returncode:
            raise RuntimeError(f"Could not checkpoint {repository}: {add.stdout.strip()}")
        tree = subprocess.run(
            [git, "write-tree"], cwd=repository, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if tree.returncode:
            raise RuntimeError(f"Could not checkpoint {repository}: {tree.stdout.strip()}")
        command = [git, "commit-tree", tree.stdout.strip(), "-m", f"the-office {kind} for run {run_id}"]
        if parent:
            command[2:2] = ["-p", parent]
        commit = subprocess.run(
            command, cwd=repository, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if commit.returncode:
            raise RuntimeError(f"Could not checkpoint {repository}: {commit.stdout.strip()}")
        sha = commit.stdout.strip()
        ref = f"refs/the-office/{kind}s/run-{run_id}"
        checked_command([git, "update-ref", ref, sha], repository)
        return {"repository": str(repository), "sha": sha, "ref": ref}
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def create_run_checkpoint(spec: dict, run_id: int) -> str:
    checkpoints = [create_repository_checkpoint(repository, run_id) for repository in git_repositories_for_spec(spec)]
    return json.dumps(checkpoints, separators=(",", ":"))


def create_run_completion(spec: dict, run_id: int) -> str:
    snapshots = [create_repository_checkpoint(repository, run_id, "completion") for repository in git_repositories_for_spec(spec)]
    return json.dumps(snapshots, separators=(",", ":"))


def restore_run_checkpoint(checkpoint_ref: str) -> list[str]:
    try:
        checkpoints = json.loads(checkpoint_ref)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("This run does not have a usable checkpoint.") from exc
    if not isinstance(checkpoints, list) or not checkpoints:
        raise ValueError("This run does not have a usable checkpoint.")
    restored = []
    git = git_executable()
    for item in checkpoints:
        repository = Path(str(item.get("repository", ""))).resolve()
        sha = str(item.get("sha", ""))
        if not repository.is_dir() or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise ValueError("The checkpoint repository or object is no longer available.")
        exists = subprocess.run(
            [git, "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repository,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if exists.returncode:
            raise ValueError(f"Checkpoint {sha[:12]} is no longer available in {repository}.")
        checkpoint_files = set(checked_command([git, "ls-tree", "-r", "--name-only", sha], repository).splitlines())
        untracked_raw = subprocess.run(
            [git, "ls-files", "--others", "--exclude-standard", "-z"], cwd=repository,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        ).stdout
        for raw_path in untracked_raw.split(b"\0"):
            if not raw_path:
                continue
            relative = raw_path.decode(errors="surrogateescape")
            if relative in checkpoint_files:
                continue
            target = (repository / relative).resolve()
            if repository not in target.parents:
                continue
            if target.is_file() or target.is_symlink():
                target.unlink()
        checked_command([git, "restore", f"--source={sha}", "--staged", "--worktree", "--", "."], repository)
        # Restore content while leaving the user's original staged/unstaged distinction neutral.
        subprocess.run([git, "reset", "--quiet"], cwd=repository, check=False)
        restored.append(str(repository))
    return restored


def checkpoint_diff(checkpoint_ref: str, completion_ref: str | None = None) -> dict:
    checkpoints = json.loads(checkpoint_ref or "[]")
    if not isinstance(checkpoints, list) or not checkpoints:
        raise ValueError("This run has no usable checkpoint.")
    git = git_executable()
    completions = json.loads(completion_ref or "[]")
    completion_by_repository = {
        str(item.get("repository")): str(item.get("sha", ""))
        for item in completions if isinstance(item, dict)
    } if isinstance(completions, list) else {}
    repositories = []
    for item in checkpoints:
        repository = Path(str(item.get("repository", ""))).resolve()
        sha = str(item.get("sha", ""))
        if not repository.is_dir() or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise ValueError("The checkpoint repository or object is no longer available.")
        completed_sha = completion_by_repository.get(str(repository))
        if completed_sha and re.fullmatch(r"[0-9a-fA-F]{40,64}", completed_sha):
            patch = _run_git_diff([git, "diff", "--binary", "--no-ext-diff", "--find-renames", sha, completed_sha, "--"], repository)
        else:
            patch = _run_git_diff([git, "diff", "--binary", "--no-ext-diff", "--find-renames", sha, "--"], repository)
            current_untracked = subprocess.run(
                [git, "ls-files", "--others", "--exclude-standard", "-z"], cwd=repository,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            for raw_path in current_untracked.stdout.split(b"\0"):
                if raw_path:
                    patch += _run_git_diff([git, "diff", "--no-index", "--binary", "--", "/dev/null", raw_path.decode(errors="surrogateescape")], repository)
        encoded = patch.encode(errors="replace")
        if len(encoded) > MAX_DIFF_BYTES:
            patch = encoded[:MAX_DIFF_BYTES].decode(errors="replace") + "\n[diff truncated]\n"
        repositories.append({"path": str(repository), "patch": patch, "files": split_diff_files(patch)})
    return {"repositories": repositories, "fileCount": sum(len(item["files"]) for item in repositories)}


def _run_git_diff(command: list[str], repository: Path) -> str:
    result = subprocess.run(
        command, cwd=repository, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=60, check=False, env=agent_environment(),
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git diff failed:\n{result.stdout.strip()}")
    return result.stdout


def combined_repository_patch(repository: Path) -> str:
    git = git_executable()
    patch = _run_git_diff([git, "diff", "--binary", "--no-ext-diff", "--find-renames", "HEAD", "--"], repository)
    untracked = subprocess.run(
        [git, "ls-files", "--others", "--exclude-standard", "-z"], cwd=repository,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if untracked.returncode:
        raise RuntimeError(f"Could not list untracked files in {repository}.")
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode(errors="surrogateescape")
        patch += _run_git_diff([git, "diff", "--no-index", "--binary", "--", "/dev/null", relative], repository)
        if len(patch.encode(errors="replace")) > MAX_DIFF_BYTES:
            raise ValueError(f"The diff in {repository} is larger than {MAX_DIFF_BYTES // 1_000_000} MB.")
    return patch


def _text_at_revision(repository: Path, revision: str, path: str) -> str:
    git = git_executable()
    if path == "/dev/null":
        return ""
    if revision == "WORKTREE":
        target = (repository / path).resolve()
        if repository not in target.parents or not target.is_file():
            return ""
        data = target.read_bytes()[:MAX_DIFF_BYTES]
    else:
        result = subprocess.run(
            [git, "show", f"{revision}:{path}"], cwd=repository,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        data = result.stdout[:MAX_DIFF_BYTES] if result.returncode == 0 else b""
    if b"\0" in data:
        return ""
    return data.decode("utf-8", errors="replace")


def repository_diff(repository: Path) -> dict:
    patch = combined_repository_patch(repository)
    files = split_diff_files(patch)
    for item in files:
        item["oldText"] = _text_at_revision(repository, "HEAD", item["oldPath"])
        item["newText"] = _text_at_revision(repository, "WORKTREE", item["newPath"])
        item.pop("header", None)
        item.pop("patch", None)
    stat = checked_command([git_executable(), "diff", "--stat", "HEAD", "--"], repository)
    return {
        "repository": str(repository), "name": repository.name, "files": files,
        "stat": stat, "digest": hashlib.sha256(patch.encode()).hexdigest(),
    }


def repository_live_diff(repository: Path) -> dict:
    git = git_executable()
    raw = subprocess.run(
        [git, "status", "--porcelain=v1", "-z"], cwd=repository,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if raw.returncode:
        raise RuntimeError(f"Could not inspect changes in {repository}.")
    files = []
    for entry in raw.stdout.split(b"\0"):
        if len(entry) < 4:
            continue
        text = entry.decode("utf-8", errors="replace")
        files.append({"status": text[:2], "path": text[3:]})
    stat = checked_command([git, "diff", "--stat", "HEAD", "--"], repository)
    return {"repository": str(repository), "files": files, "fileCount": len(files), "stat": stat}


def update_repositories(spec: dict) -> dict:
    """Fast-forward repositories before refreshing onboarding context."""
    git = git_executable()
    repositories = git_repositories_for_spec(spec)
    prepared = []
    for repository in repositories:
        if checked_command([git, "status", "--porcelain"], repository):
            raise ValueError(
                f"Cannot pull {repository}: it has local changes. Commit, stash, publish, or revert them first."
            )
        branch = checked_command([git, "branch", "--show-current"], repository)
        upstream = subprocess.run(
            [git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            cwd=repository, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, env=agent_environment(),
        )
        prepared.append((repository, branch, upstream.stdout.strip() if upstream.returncode == 0 else ""))
    results = []
    for repository, branch, upstream in prepared:
        if not branch:
            results.append({"repository": str(repository), "status": "skipped", "message": "Detached HEAD; no branch was pulled."})
            continue
        if not upstream:
            results.append({"repository": str(repository), "status": "skipped", "message": "No upstream branch is configured."})
            continue
        output = checked_command([git, "pull", "--ff-only"], repository, timeout=900)
        results.append({
            "repository": str(repository), "status": "updated",
            "message": output or f"Fast-forwarded {branch} from {upstream}.",
        })
    return {"repositories": results, "updated": sum(item["status"] == "updated" for item in results)}


def repository_files(root: Path, query: str = "") -> list[str]:
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("An existing absolute repository path is required.")
    query = query.lower().strip()
    candidates = []
    repositories = [root] if (root / ".git").exists() else discover_git_repositories(root)
    git = git_executable()
    for repository in repositories:
        result = subprocess.run(
            [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repository, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        prefix = "" if repository == root else str(repository.relative_to(root)) + "/"
        for raw in result.stdout.split(b"\0"):
            if raw:
                path = prefix + raw.decode("utf-8", errors="replace")
                if not query or query in path.lower():
                    candidates.append(path)
    return sorted(set(candidates), key=lambda value: (len(value), value.lower()))[:500]


def repository_tree_files(root: Path) -> list[str]:
    root = workspace_root(root)
    ignored = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target"}
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in ignored]
        base = Path(current)
        for name in names:
            target = base / name
            if target.is_symlink():
                continue
            files.append(str(target.relative_to(root)))
            if len(files) >= 10_000:
                return sorted(files, key=lambda value: value.lower())
    return sorted(files, key=lambda value: value.lower())


def workspace_root(value: object) -> Path:
    """Resolve a configured workspace without allowing arbitrary filesystem roots."""
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser()
    if not raw or not candidate.is_absolute():
        raise ValueError("An existing absolute workspace path is required.")
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError("An existing absolute workspace path is required.")
    if not (root / ".git").exists() and not discover_git_repositories(root):
        raise ValueError("The workspace must be a Git repository or a cupboard containing repositories.")
    return root


def workspace_file(root: Path, relative: object, must_exist: bool = True) -> Path:
    value = str(relative or "").strip().replace("\\", "/")
    if not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("A safe workspace-relative file path is required.")
    target = (root / value).resolve()
    if root not in target.parents:
        raise ValueError("The file path points outside the workspace.")
    if must_exist and not target.is_file():
        raise ValueError("That workspace file does not exist.")
    return target


def read_workspace_file(root: Path, relative: object) -> dict:
    target = workspace_file(root, relative)
    data = target.read_bytes()
    if len(data) > MAX_BODY:
        raise ValueError("Files larger than 1 MB must be edited outside The Office.")
    if b"\0" in data:
        raise ValueError("Binary files cannot be opened in the text editor.")
    return {
        "path": str(target.relative_to(root)), "content": data.decode("utf-8", errors="replace"),
        "size": len(data), "modifiedAt": int(target.stat().st_mtime * 1000),
    }


def write_workspace_file(root: Path, relative: object, content: object) -> dict:
    target = workspace_file(root, relative)
    text = str(content if content is not None else "")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_BODY:
        raise ValueError("Editor saves are limited to 1 MB.")
    target.write_bytes(encoded)
    return {"ok": True, "path": str(target.relative_to(root)), "size": len(encoded), "modifiedAt": int(target.stat().st_mtime * 1000)}


def repository_commands(root: Path) -> list[dict]:
    """Detect conservative, human-invoked test/lint/build commands."""
    commands: list[dict] = []
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text()).get("scripts") or {}
            for name in ("test", "lint", "build", "check", "typecheck", "format"):
                if name in scripts:
                    commands.append({"name": f"npm {name}", "command": f"npm run {shlex.quote(name)}", "source": "package.json"})
        except (OSError, json.JSONDecodeError):
            pass
    makefile = next((path for path in (root / "Makefile", root / "makefile") if path.is_file()), None)
    if makefile:
        targets = set(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", makefile.read_text(errors="replace"), re.MULTILINE))
        for name in ("test", "lint", "build", "check", "format"):
            if name in targets:
                commands.append({"name": f"make {name}", "command": f"make {name}", "source": makefile.name})
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() or (root / "tests").is_dir():
        commands.append({"name": "pytest", "command": "python3 -m pytest", "source": "Python project"})
    if (root / "Cargo.toml").is_file():
        commands.extend([{"name": "cargo test", "command": "cargo test", "source": "Cargo.toml"}, {"name": "cargo check", "command": "cargo check", "source": "Cargo.toml"}])
    if (root / "go.mod").is_file():
        commands.append({"name": "go test", "command": "go test ./...", "source": "go.mod"})
    seen = set()
    return [item for item in commands if not (item["command"] in seen or seen.add(item["command"]))][:30]


def git_panel_state(repository: Path) -> dict:
    git = git_executable()
    porcelain = checked_command([git, "status", "--porcelain=v1"], repository)
    changes = []
    for line in porcelain.splitlines():
        if len(line) >= 4:
            changes.append({"status": line[:2], "path": line[3:].split(" -> ")[-1]})
    branches = checked_command([git, "for-each-ref", "--format=%(refname:short)", "refs/heads"], repository).splitlines()
    log_lines = checked_command([git, "log", "-12", "--date=short", "--pretty=format:%h%x09%ad%x09%s"], repository).splitlines()
    return {"branch": checked_command([git, "branch", "--show-current"], repository), "branches": branches, "changes": changes, "commits": [{"hash": parts[0], "date": parts[1], "subject": parts[2]} for line in log_lines if len(parts := line.split("\t", 2)) == 3]}


def git_panel_action(data: dict) -> dict:
    repository = existing_repo_path({"mode": "local", "path": str(data.get("path", ""))})
    action, git = str(data.get("action", "state")), git_executable()
    if action == "switch":
        branch = str(data.get("branch", "")).strip()
        if not branch or subprocess.run([git, "check-ref-format", "--branch", branch], cwd=repository, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ValueError("Choose a valid local branch.")
        checked_command([git, "switch", branch], repository)
    elif action == "discard":
        if data.get("confirmed") is not True:
            raise ValueError("Discard requires explicit confirmation.")
        target = workspace_file(repository, data.get("file"))
        relative = str(target.relative_to(repository))
        tracked = subprocess.run([git, "ls-files", "--error-unmatch", "--", relative], cwd=repository, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
        checked_command([git, "restore", "--staged", "--worktree", "--", relative] if tracked else [git, "clean", "-f", "--", relative], repository)
    elif action == "stash":
        checked_command([git, "stash", "push", "--include-untracked", "-m", "the-office manual stash"], repository)
    elif action == "pop":
        checked_command([git, "stash", "pop"], repository)
    elif action != "state":
        raise ValueError("Unsupported Git panel action.")
    return git_panel_state(repository)


def start_shell_job(data: dict) -> dict:
    root = workspace_root(data.get("path"))
    command = str(data.get("command", "")).strip()
    if not command or len(command) > 4000:
        raise ValueError("Enter a shell command of at most 4000 characters.")
    job_id = hashlib.sha256(f"{time.time_ns()}:{root}:{command}".encode()).hexdigest()[:16]
    shell = os.environ.get("SHELL") or "/bin/sh"
    process = subprocess.Popen([shell, "-lc", command], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=agent_environment(), start_new_session=True, bufsize=1)
    job = {"id": job_id, "path": str(root), "command": command, "status": "running", "startedAt": int(time.time() * 1000), "lines": [], "process": process}
    with lock:
        shell_jobs[job_id] = job
        for stale in sorted(shell_jobs.values(), key=lambda item: item["startedAt"])[:-50]:
            if stale["status"] != "running": shell_jobs.pop(stale["id"], None)
    def collect() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with lock: job["lines"] = (job["lines"] + [line.rstrip("\n")])[-5000:]
        process.stdout.close()
        returncode = process.wait()
        with lock:
            job["returncode"] = returncode; job["endedAt"] = int(time.time() * 1000); job["status"] = "completed" if returncode == 0 else "failed"; job.pop("process", None)
    threading.Thread(target=collect, name=f"office-shell-{job_id}", daemon=True).start()
    return {key: value for key, value in job.items() if key not in {"process", "lines"}}


def public_shell_job(job_id: str, since: int = 0) -> dict:
    with lock:
        job = shell_jobs.get(job_id)
        if not job: raise ValueError("Unknown shell job.")
        lines = list(job["lines"]); payload = {key: value for key, value in job.items() if key not in {"process", "lines"}}
    payload.update({"lines": lines[since:], "sequence": len(lines)})
    return payload


def file_context(root: Path, references: list) -> dict:
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("An existing absolute repository path is required.")
    files = []
    for value in references[:10]:
        relative = str(value).strip().lstrip("@/")
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            continue
        target = (root / relative).resolve()
        if root not in target.parents or not target.is_file():
            continue
        data = target.read_bytes()[:MAX_FILE_CONTEXT_BYTES]
        if b"\0" in data:
            files.append({"path": relative, "content": "[binary file]", "truncated": False})
        else:
            files.append({
                "path": relative, "content": data.decode("utf-8", errors="replace"),
                "truncated": target.stat().st_size > len(data),
            })
    return {"files": files}


def search_repository(root: Path, query: str) -> dict:
    query = query.strip()
    if not root.is_absolute() or not root.is_dir() or not query or len(query) > 200:
        raise ValueError("Repository search requires a path and a query of at most 200 characters.")
    ripgrep = shutil.which("rg")
    if ripgrep:
        command = [ripgrep, "--fixed-strings", "--line-number", "--no-heading", "--color", "never", "--glob", "!.git/**", "--", query, "."]
    else:
        command = [git_executable(), "grep", "--line-number", "--fixed-strings", "--", query]
    result = subprocess.run(
        command, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False, env=agent_environment(),
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "Repository search failed.")
    lines = result.stdout.splitlines()
    return {"query": query, "matches": lines[:500], "truncated": len(lines) > 500}


def selected_patch(repository: Path, selection: dict | None) -> str:
    patch = combined_repository_patch(repository)
    if not selection:
        return patch
    if not isinstance(selection, dict):
        raise ValueError("Diff selection must be an object.")
    expected_digest = str(selection.get("digest", ""))
    actual_digest = hashlib.sha256(patch.encode()).hexdigest()
    if expected_digest and expected_digest != actual_digest:
        raise ValueError(f"The diff in {repository} changed after review. Reopen review before publishing.")
    accepted = set(str(value) for value in selection.get("accepted", []))
    pieces = []
    for item in split_diff_files(patch):
        if item["id"] in accepted:
            pieces.append(item["patch"])
            continue
        chosen_hunks = [hunk["patch"] for hunk in item["hunks"] if hunk["id"] in accepted]
        if chosen_hunks:
            pieces.append(item["header"] + "".join(chosen_hunks))
    return "".join(pieces)


def stage_selected_changes(repository: Path, selection: dict | None, git: str | None = None) -> None:
    git = git or git_executable()
    patch = selected_patch(repository, selection)
    if not patch.strip():
        raise ValueError(f"Select at least one changed hunk to publish in {repository}.")
    checked_command([git, "reset", "--quiet"], repository)
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as patch_file:
        patch_file.write(patch)
        patch_path = patch_file.name
    try:
        checked_command([git, "apply", "--cached", "--binary", "--whitespace=nowarn", patch_path], repository)
    finally:
        try:
            os.unlink(patch_path)
        except OSError:
            pass


def valid_branch_name(branch: str) -> bool:
    return bool(
        branch
        and len(branch) <= 180
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
        and ".." not in branch
        and not branch.endswith("/")
    )


def validate_publish_details(data: dict) -> tuple[str, str, str, str, str, str]:
    branch = str(data.get("branch", "")).strip()
    destination_branch = str(data.get("destinationBranch", "")).strip()
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    if not valid_branch_name(branch):
        raise ValueError("Enter a valid source branch name.")
    if not valid_branch_name(destination_branch):
        raise ValueError("Enter a valid destination branch name.")
    if not title or len(title) > 240 or len(body) > 50_000:
        raise ValueError("Invalid pull-request details.")
    git = shutil.which("git")
    gh = find_cli("gh")
    if not git:
        raise RuntimeError("git is not installed or is not on PATH.")
    if not gh:
        raise RuntimeError("GitHub CLI (gh) is required to create the pull request.")
    return branch, destination_branch, title, body, git, gh


def preflight_publish(repository: Path, branch: str, git: str) -> None:
    status = checked_command([git, "status", "--porcelain"], repository)
    if not status:
        raise ValueError(f"There are no uncommitted changes to publish in {repository}.")
    current = checked_command([git, "branch", "--show-current"], repository)
    if current != branch:
        exists = subprocess.run(
            [git, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repository
        ).returncode == 0
        if exists:
            raise ValueError(f"Local branch {branch} already exists in {repository}; choose another name.")


def publish_repository(
    repository: Path, branch: str, destination_branch: str,
    title: str, body: str, git: str, gh: str, selection: dict | None = None,
) -> dict:
    current = checked_command([git, "branch", "--show-current"], repository)
    if current != branch:
        checked_command([git, "switch", "-c", branch], repository)
    # Build the index exclusively from the reviewed patch. Rejected hunks stay
    # untouched in the working tree and remain visible after the commit.
    stage_selected_changes(repository, selection, git)
    checked_command([git, "commit", "-m", title], repository)
    checked_command([git, "push", "-u", "origin", branch], repository, timeout=900)
    pr_output = checked_command(
        [gh, "pr", "create", "--base", destination_branch, "--title", title, "--body", body],
        repository,
        timeout=300,
    )
    return {
        "repository": str(repository), "name": repository.name, "branch": branch,
        "destinationBranch": destination_branch,
        "pullRequest": pr_output.splitlines()[-1] if pr_output else "created",
    }


def publish_changes(data: dict) -> dict:
    branch, destination_branch, title, body, git, gh = validate_publish_details(data)
    repository = existing_repo_path(data.get("repository") or {})
    preflight_publish(repository, branch, git)
    return publish_repository(repository, branch, destination_branch, title, body, git, gh, data.get("selection"))


def publish_cupboard(data: dict) -> dict:
    branch, destination_branch, title, body, git, gh = validate_publish_details(data)
    root, discovered = cupboard_repositories(data.get("cupboard") or {})
    requested = {str(value) for value in (data.get("repositories") or []) if value}
    dirty_repositories = [
        repository for repository in discovered
        if checked_command([git, "status", "--porcelain"], repository)
    ]
    reviewed_repositories = [
        repository for repository in dirty_repositories
        if not requested or str(repository.relative_to(root)) in requested or str(repository) in requested
    ]
    unreviewed = [repository for repository in dirty_repositories if repository not in reviewed_repositories]
    if unreviewed:
        names = ", ".join(str(repository.relative_to(root)) for repository in unreviewed)
        raise ValueError(f"Changed cupboards were not included in the manager review: {names}.")
    selections = data.get("selections") or {}
    if not isinstance(selections, dict):
        raise ValueError("Cupboard diff selections must be an object.")
    repositories = [
        repository for repository in reviewed_repositories
        if ("." if repository == root else str(repository.relative_to(root))) not in selections
        or bool(selections["." if repository == root else str(repository.relative_to(root))].get("accepted"))
    ]
    if not repositories:
        raise ValueError("There are no uncommitted changes in the selected cupboards.")
    for repository in repositories:
        preflight_publish(repository, branch, git)
    results = [
        publish_repository(
            repository, branch, destination_branch, title, body, git, gh,
            (data.get("selections") or {}).get("." if repository == root else str(repository.relative_to(root))),
        )
        for repository in repositories
    ]
    return {
        "branch": branch,
        "destinationBranch": destination_branch,
        "repositories": results,
        "root": str(root),
    }


def repository_state(data: dict) -> dict:
    """Report whether the current branch's work is already present on its upstream."""
    repository = existing_repo_path(data.get("repository") or {})
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is not installed or is not on PATH.")

    dirty = bool(checked_command([git, "status", "--porcelain"], repository))
    branch = checked_command([git, "branch", "--show-current"], repository)
    upstream_result = subprocess.run(
        [git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        env=agent_environment(),
    )
    upstream = upstream_result.stdout.strip() if upstream_result.returncode == 0 else ""
    remote_branches = checked_command(
        [git, "for-each-ref", "--contains", "HEAD", "--format=%(refname:short)", "refs/remotes"],
        repository,
    ).splitlines()
    ahead = behind = None
    if upstream:
        counts = checked_command([git, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"], repository)
        behind_text, ahead_text = counts.split()
        behind, ahead = int(behind_text), int(ahead_text)

    locally_pushed = bool(remote_branches) or (ahead == 0 if ahead is not None else False)
    remote_head_matches = False
    if not dirty and not locally_pushed and branch:
        remote_env = {**agent_environment(), "GIT_TERMINAL_PROMPT": "0"}
        remote_result = subprocess.run(
            [git, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            env=remote_env,
        )
        remote_head = remote_result.stdout.split()[0] if remote_result.returncode == 0 and remote_result.stdout.strip() else ""
        local_head = checked_command([git, "rev-parse", "HEAD"], repository)
        remote_head_matches = bool(remote_head) and remote_head == local_head

    return {
        "head": checked_command([git, "rev-parse", "HEAD"], repository),
        "branch": branch, "upstream": upstream or None, "dirty": dirty,
        "ahead": ahead, "behind": behind, "remoteBranches": remote_branches,
        "remoteHeadMatches": remote_head_matches,
        "pushed": not dirty and (locally_pushed or remote_head_matches),
    }


def cupboard_state(data: dict) -> dict:
    root, discovered = cupboard_repositories(data.get("cupboard") or {})
    requested = {str(value) for value in (data.get("repositories") or []) if value}
    repositories = [
        repository for repository in discovered
        if not requested or str(repository.relative_to(root)) in requested or str(repository) in requested
    ]
    states = []
    for repository in repositories:
        state = repository_state({"mode": "local", "path": str(repository)})
        states.append({
            **state,
            "name": repository.name,
            "path": str(repository),
            "relativePath": "." if repository == root else str(repository.relative_to(root)),
        })
    return {
        "repositories": states,
        "dirty": any(item["dirty"] for item in states),
        "pushed": bool(states) and all(item["pushed"] for item in states),
        "branch": states[0]["branch"] if states and len({item["branch"] for item in states}) == 1 else None,
    }


def select_directory(data: dict) -> dict:
    title = str(data.get("title", "Select a folder")).strip()[:120] or "Select a folder"
    initial_raw = str(data.get("initial", "")).strip()
    initial = Path(initial_raw).expanduser() if initial_raw else Path.home()
    if not initial.is_dir():
        initial = initial.parent if initial.parent.is_dir() else Path.home()

    selected = ""
    zenity = shutil.which("zenity")
    kdialog = shutil.which("kdialog")
    if zenity:
        result = subprocess.run(
            [zenity, "--file-selection", "--directory", f"--title={title}", f"--filename={initial}{os.sep}"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, check=False,
        )
        if result.returncode != 0 and result.stderr.strip():
            raise RuntimeError("The native folder picker could not connect to the desktop session.")
        selected = result.stdout.strip() if result.returncode == 0 else ""
    elif kdialog:
        result = subprocess.run(
            [kdialog, "--getexistingdirectory", str(initial), "--title", title],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, check=False,
        )
        if result.returncode != 0 and result.stderr.strip():
            raise RuntimeError("The native folder picker could not connect to the desktop session.")
        selected = result.stdout.strip() if result.returncode == 0 else ""
    else:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title=title, initialdir=str(initial), mustexist=True)
            root.destroy()
        except Exception as exc:
            raise RuntimeError(
                "No native folder picker is available. Install zenity, kdialog, or Python tkinter."
            ) from exc

    if not selected:
        return {"cancelled": True}
    path = Path(selected).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("The selected folder is not an accessible absolute directory.")
    if data.get("requireGit") and not (path / ".git").exists():
        raise ValueError("Select a Git repository folder containing .git.")
    return {"cancelled": False, "path": str(path.resolve())}


def browse_directory(data: dict) -> dict:
    raw = str(data.get("path", "")).strip()
    path = Path(raw).expanduser() if raw else Path.home()
    if not path.is_absolute():
        raise ValueError("Folder browsing requires an absolute path.")
    path = path.resolve()
    if not path.is_dir():
        raise ValueError("That folder does not exist or is not accessible.")
    try:
        directories = sorted(
            (child for child in path.iterdir() if child.is_dir()),
            key=lambda child: (child.name.startswith("."), child.name.lower()),
        )[:500]
    except PermissionError as exc:
        raise ValueError("That folder cannot be read with the current user's permissions.") from exc
    parent = path.parent if path.parent != path else None
    is_git = (path / ".git").exists()
    return {
        "path": str(path),
        "parent": str(parent) if parent else None,
        "directories": [{"name": child.name, "path": str(child)} for child in directories],
        "isGit": is_git,
        "selectable": is_git if data.get("requireGit") else True,
    }


def plugin_signal_rules() -> list[dict]:
    path = ROOT / "plugin_suggestions.json"
    try:
        rules = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load plugin suggestions from {path}.") from exc
    if not isinstance(rules, list):
        raise RuntimeError("plugin_suggestions.json must contain an array.")
    return rules


def plugin_suggestions(spec: dict) -> dict:
    roots = git_repositories_for_spec(spec)
    evidence = []
    corpus_parts = []
    manifest_names = {
        "package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml",
        "gemfile", "pom.xml", "build.gradle", "dockerfile", "compose.yaml", "docker-compose.yml",
    }
    for root in roots:
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = [name for name in directory_names if name not in IGNORED_CUPBOARD_DIRS and name != ".git"]
            if len(evidence) >= 200:
                break
            for file_name in file_names:
                path = Path(current) / file_name
                relative = str(path.relative_to(root))
                lowered = path.name.lower()
                if lowered in manifest_names or path.suffix == ".tf" or relative.startswith(".github/workflows/"):
                    evidence.append(relative)
                    try:
                        corpus_parts.append(relative + "\n" + path.read_text(errors="replace")[:100_000])
                    except OSError:
                        pass
    corpus = "\n".join(corpus_parts).lower()
    suggestions = []
    for rule in plugin_signal_rules():
        identifier, signals = str(rule.get("id", "")), rule.get("signals") or []
        label, access = str(rule.get("name", identifier)), str(rule.get("access", "Review the tool's permissions before enabling it."))
        matched = [signal for signal in signals if signal in corpus]
        if matched:
            suggestions.append({
                "id": identifier, "name": label, "matchedSignals": matched,
                "access": access, "requiresApproval": True,
                "source": "local manifest heuristic",
            })
    return {"suggestions": suggestions, "filesInspected": len(evidence), "evidence": evidence[:40]}


SUGGESTION_PLUGIN_NAMES = {
    "terraform": "terraform-helpers", "postgresql": "postgres-helpers", "kafka": "kafka-helpers",
    "aws": "aws-helpers", "docker": "container-helpers", "stripe": "stripe-helpers",
    "kubernetes": "kubernetes-helpers", "github": "github-helpers",
}


def approved_plugin_path(identifier: str) -> dict:
    raw_name = identifier.split("@", 1)[0]
    name = SUGGESTION_PLUGIN_NAMES.get(raw_name, raw_name)
    if not re.fullmatch(r"[a-z0-9-]{1,80}", name):
        raise ValueError("Invalid plugin suggestion.")
    path = (ROOT / "the-office-plugins" / "plugins" / name).resolve()
    if not (path / ".claude-plugin" / "plugin.json").is_file():
        raise ValueError("That suggested plugin is not in the internal marketplace.")
    return {"name": name, "path": str(path), "marketplace": "the-office-plugins"}


def agent_command(
    agent: str,
    repository: Path,
    prompt: str,
    run_type: str,
    schema_path: str | None = None,
    output_path: str | None = None,
    output_schema: dict | None = None,
    session_id: str | None = None,
    allow_non_git: bool = False,
    repository_spec: dict | None = None,
) -> list[str]:
    executable = find_cli(agent)
    if not executable:
        raise RuntimeError(f"{agent} CLI is not installed or is not on PATH.")
    spec = repository_spec or {}
    return adapter_for(agent).command(
        executable, repository, prompt, run_type,
        schema_path=schema_path, output_path=output_path, output_schema=output_schema,
        session_id=session_id, allow_non_git=allow_non_git,
        mcp_servers=normalized_mcp_servers(spec), plugin_paths=normalized_plugin_paths(spec),
        add_codex_mcp_overrides=add_codex_mcp_overrides, claude_mcp_config=claude_mcp_config,
        plugin_settings=claude_plugin_suggestion_settings(), lightweight_run_types=LIGHTWEIGHT_RUN_TYPES,
    )


def run_agent(run: dict, repository_spec: dict, prompt: str) -> None:
    temporary_paths: list[str] = []
    try:
        agent_adapter = adapter_for(run["agent"])
        repository = repo_path(repository_spec, run)
        if run["runType"] in ("work", "orchestrate") and not run.get("checkpointRef"):
            run["checkpointRef"] = create_run_checkpoint(repository_spec, int(run.get("databaseRunId") or now_ms()))
            persist_run_status(run)
            append_log(run, "system", "Created a recoverable pre-run repository checkpoint")
        schema_path = output_path = None
        output_schema = (
            ONBOARD_SCHEMA if run["runType"] == "onboard"
            else PLAN_SCHEMA if run["runType"] == "plan"
            else REVIEW_SCHEMA if run["runType"] in ("review", "orchestrate")
            else REPORT_SCHEMA if run["runType"] == "report"
            else RECEPTION_SCHEMA if run["runType"] == "reception"
            else FLOOR_CALL_SCHEMA if run["runType"] == "floor_call"
            else FLOOR_INTENT_SCHEMA if run["runType"] == "floor_intent"
            else None
        )
        if output_schema and agent_adapter.uses_structured_output_files():
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as schema_file:
                json.dump(output_schema, schema_file)
                schema_path = schema_file.name
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as output_file:
                output_path = output_file.name
            temporary_paths += [schema_path, output_path]
        command = agent_command(
            run["agent"], repository, prompt, run["runType"], schema_path, output_path,
            output_schema, run.get("sessionId"), bool(repository_spec.get("allowNonGit")), repository_spec
        )
        resume_note = f" · resuming {run['sessionId']}" if run.get("sessionId") else " · new session"
        append_log(run, "system", f"Starting {run['agent']} in {repository}{resume_note}")
        initial_activity = {
            "onboard": ("inspecting", "Learning the codebase"),
            "plan": ("thinking", "Analyzing the task"),
            "orchestrate": ("delegating", "Coordinating employees"),
            "review": ("reviewing", "Reviewing changes"),
            "question": ("thinking", "Answering a question"),
            "report": ("researching", "Investigating for a report"),
            "reception": ("thinking", "Routing work across floors"),
            "floor_call": ("coordinating", "Consulting another floor"),
            "floor_intent": ("thinking", "Reading the floor request"),
            "chat": ("thinking", "Writing a response"),
        }.get(run["runType"], ("thinking", "Starting the task"))
        set_activity(run, *initial_activity)
        process_env = agent_environment()
        retried_claude_login = False
        while True:
            claude_result = None
            final_message = None
            claude_auth_failed = False
            process = subprocess.Popen(
                command,
                cwd=repository,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=process_env,
            )
            with lock:
                run["process"] = process
                run["pid"] = process.pid
            assert process.stdout is not None
            for line in process.stdout:
                append_log(run, "agent", line)
                suggestion_match = re.search(r"plugin suggestion:\s*([A-Za-z0-9_-]+@[A-Za-z0-9_-]+)", line, re.IGNORECASE)
                if not suggestion_match:
                    suggestion_match = re.search(r"/plugin install\s+([A-Za-z0-9_-]+@[A-Za-z0-9_-]+)", line)
                if suggestion_match:
                    suggestion = suggestion_match.group(1)
                    if suggestion not in run.setdefault("pluginSuggestions", []):
                        run["pluginSuggestions"].append(suggestion)
                if agent_adapter.authentication_failed(line):
                    claude_auth_failed = True
                try:
                    event = json.loads(line)
                    update_activity_from_event(run, event)
                    discovered_session = agent_adapter.extract_session_id(event)
                    if discovered_session:
                        with lock:
                            run["sessionId"] = discovered_session
                    usage = event.get("usage")
                    if not usage and isinstance(event.get("message"), dict):
                        usage = event["message"].get("usage")
                    if not usage and isinstance(event.get("result"), dict):
                        usage = event["result"].get("usage")
                    if isinstance(usage, dict):
                        input_tokens = int(usage.get("input_tokens", usage.get("inputTokens", 0)) or 0)
                        output_tokens = int(usage.get("output_tokens", usage.get("outputTokens", 0)) or 0)
                        with lock:
                            run["inputTokens"] = max(run.get("inputTokens", 0), input_tokens)
                            run["outputTokens"] = max(run.get("outputTokens", 0), output_tokens)
                    final_message = agent_adapter.final_message(event, final_message)
                    if output_schema:
                        claude_result = agent_adapter.structured_result(event, claude_result)
                except json.JSONDecodeError:
                    pass
            returncode = process.wait()
            if (
                agent_adapter.name == "claude" and returncode != 0 and claude_auth_failed
                and not retried_claude_login and "ANTHROPIC_API_KEY" in process_env
            ):
                append_log(run, "system", "The configured Claude API key was rejected; retrying with the local Claude login")
                process_env = dict(process_env)
                process_env.pop("ANTHROPIC_API_KEY", None)
                retried_claude_login = True
                continue
            break
        plan_result = None
        if returncode == 0 and output_schema:
            candidate = Path(output_path).read_text().strip() if output_path else claude_result
            if isinstance(candidate, str):
                candidate = json.loads(candidate)
            if not isinstance(candidate, dict):
                raise RuntimeError("Tech lead returned invalid structured output.")
            if run["runType"] == "plan" and candidate.get("decision") not in ("single", "multi"):
                raise RuntimeError("Tech lead returned an invalid work plan.")
            if run["runType"] == "onboard" and not isinstance(candidate.get("summary"), str):
                raise RuntimeError("Tech lead returned invalid repository context.")
            if run["runType"] in ("review", "orchestrate") and not isinstance(candidate.get("ready"), bool):
                raise RuntimeError("Tech lead returned an invalid change review.")
            if run["runType"] == "onboard" and not isinstance(candidate.get("repositories"), list):
                raise RuntimeError("Tech lead returned an invalid cupboard index.")
            if run["runType"] in ("review", "orchestrate") and not isinstance(candidate.get("repositories"), list):
                raise RuntimeError("Tech lead returned an invalid repository review index.")
            if run["runType"] == "report" and not isinstance(candidate.get("executive_summary"), str):
                raise RuntimeError("The employee returned an invalid report.")
            if run["runType"] == "reception" and not isinstance(candidate.get("routes"), list):
                raise RuntimeError("Reception returned an invalid routing plan.")
            if run["runType"] == "floor_call" and not isinstance(candidate.get("answer"), str):
                raise RuntimeError("The consulted floor returned an invalid answer.")
            if run["runType"] == "floor_intent" and not isinstance(candidate.get("floors"), list):
                raise RuntimeError("Floor intent parsing returned an invalid floor list.")
            plan_result = candidate
        with lock:
            run["returncode"] = returncode
            run["status"] = "completed" if returncode == 0 else "failed"
            run["result"] = plan_result
            run["finalMessage"] = final_message
            run["errorMessage"] = None if returncode == 0 else (final_message or f"{run['agent']} exited with code {returncode}")
            run["endedAt"] = now_ms()
            run["process"] = None
        set_activity(run, "completed" if returncode == 0 else "failed", "Completed" if returncode == 0 else "Agent stopped with an error")
        append_log(run, "system", f"{run['agent']} exited with code {returncode}")
    except Exception as exc:  # surfaced in the profile log
        append_log(run, "error", str(exc))
        with lock:
            run["status"] = "failed"
            run["errorMessage"] = str(exc)
            run["endedAt"] = now_ms()
            run["process"] = None
        set_activity(run, "failed", "Agent stopped with an error")
    finally:
        if run.get("checkpointRef") and not run.get("completionRef") and run.get("status") in ("completed", "failed"):
            try:
                run["completionRef"] = create_run_completion(
                    repository_spec, int(run.get("databaseRunId") or now_ms())
                )
                append_log(run, "system", "Captured the immutable repository state at run completion")
            except Exception as exc:
                append_log(run, "error", f"Could not capture the completion snapshot: {exc}")
        persist_run_status(run)
        if run.get("status") in ("completed", "failed") and not run.get("hookEmitted"):
            run["hookEmitted"] = True
            emit_lifecycle_hook(
                "on_run_finished" if run["status"] == "completed" else "on_run_blocked",
                {"runId": run.get("databaseRunId"), "profileId": run["profileId"], "task": run["task"], "status": run["status"], "runType": run["runType"]},
                run,
            )
        for temporary_path in temporary_paths:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def run_agent_with_path_locks(run: dict) -> None:
    owner = f"run:{run.get('databaseRunId') or run['profileId']}:{run['startedAt']}"
    claims = run.get("pathClaims") or []
    try:
        if not acquire_path_claims(owner, claims, run):
            return
        with lock:
            if run.get("status") == "waiting_for_lock":
                run["status"] = "running"
        persist_run_status(run)
        if claims:
            append_log(run, "system", "Acquired file ownership: " + ", ".join(f"{repository}:{path}" for repository, path in claims))
        run_agent(run, run["repository"], run["prompt"])
    finally:
        release_path_claims(owner)


def public_run(run: dict, since: int = 0) -> dict:
    with lock:
        payload = {
            "profileId": run["profileId"],
            "agent": run["agent"],
            "task": run["task"],
            "status": run["status"],
            "pid": run.get("pid"),
            "startedAt": run["startedAt"],
            "endedAt": run.get("endedAt"),
            "returncode": run.get("returncode"),
            "runType": run.get("runType", "work"),
            "result": run.get("result"),
            "sessionId": run.get("sessionId"),
            "finalMessage": run.get("finalMessage"),
            "errorMessage": run.get("errorMessage"),
            "activityPhase": run.get("activityPhase", "idle"),
            "activityLabel": run.get("activityLabel", "Available"),
            "activityUpdatedAt": run.get("activityUpdatedAt"),
            "databaseRunId": run.get("databaseRunId"),
            "checkpointRef": run.get("checkpointRef"),
            "completionRef": run.get("completionRef"),
            "inputTokens": run.get("inputTokens", 0),
            "outputTokens": run.get("outputTokens", 0),
            "approvalCategories": run.get("approvalCategories", []),
            "verification": list(run.get("verification", []))[-20:],
            "pathClaims": [{"repository": repository, "path": path} for repository, path in run.get("requestedPathClaims", [])],
            "pluginSuggestions": list(run.get("pluginSuggestions", [])),
            "logs": [item for item in run["logs"] if item["sequence"] > since],
            "sequence": run["sequence"],
        }
    previous_input, previous_output = session_token_usage(payload.get("sessionId"), payload.get("databaseRunId"))
    payload["sessionInputTokens"] = previous_input + int(payload["inputTokens"] or 0)
    payload["sessionOutputTokens"] = previous_output + int(payload["outputTokens"] or 0)
    payload["sessionTurns"] = session_turn_count(payload.get("sessionId"), payload.get("databaseRunId")) + (1 if payload.get("sessionId") else 0)
    return payload


class OfficeHandler(BaseHTTPRequestHandler):
    server_version = "TheOffice/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def request_json(self, max_body: int = MAX_BODY) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_body:
            raise ValueError("Invalid request size.")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.json_response({
                "ok": True,
                "version": 7,
                "runTypes": list(SUPPORTED_RUN_TYPES),
                "agents": {"codex": bool(find_cli("codex")), "claude": bool(find_cli("claude"))},
                "plugins": bundled_plugins(),
                "storage": {"mode": "sqlite", "path": str(DATABASE_PATH)},
            })
            return
        if parsed.path in ("/api/state", "/api/state/export"):
            self.json_response(read_office_state())
            return
        if parsed.path == "/api/runs":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self.json_response(persisted_run_history(limit))
            return
        if parsed.path == "/api/repo-diff":
            try:
                query = parse_qs(parsed.query)
                repository = existing_repo_path({"mode": "local", "path": query.get("path", [""])[0]})
                self.json_response(repository_diff(repository))
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        live_match = re.fullmatch(r"/api/runs/(\d+)/live-diff", parsed.path)
        if live_match:
            with database_lock:
                row = require_database().execute(
                    "SELECT repository_json FROM agent_runs WHERE id=?", (int(live_match.group(1)),)
                ).fetchone()
            if not row:
                self.json_response({"error": "Unknown run."}, HTTPStatus.NOT_FOUND)
                return
            try:
                spec = json.loads(row["repository_json"] or "{}")
                states = [repository_live_diff(repository) for repository in git_repositories_for_spec(spec)]
                self.json_response({"repositories": states, "fileCount": sum(item["fileCount"] for item in states)})
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        timeline_match = re.fullmatch(r"/api/runs/(\d+)/diff", parsed.path)
        if timeline_match:
            with database_lock:
                row = require_database().execute(
                    "SELECT checkpoint_ref, completion_ref FROM agent_runs WHERE id=?", (int(timeline_match.group(1)),)
                ).fetchone()
            if not row:
                self.json_response({"error": "Unknown run."}, HTTPStatus.NOT_FOUND)
                return
            try:
                self.json_response(checkpoint_diff(row["checkpoint_ref"], row["completion_ref"]))
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/files":
            try:
                query = parse_qs(parsed.query)
                root = Path(query.get("path", [""])[0]).expanduser().resolve()
                self.json_response({"files": repository_files(root, query.get("q", [""])[0])})
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/repo-tree":
            try:
                query = parse_qs(parsed.query)
                root = workspace_root(query.get("path", [""])[0])
                self.json_response({"files": repository_tree_files(root)})
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/repo-file":
            try:
                query = parse_qs(parsed.query)
                root = workspace_root(query.get("root", [""])[0])
                self.json_response(read_workspace_file(root, query.get("path", [""])[0]))
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/repo-commands":
            try:
                query = parse_qs(parsed.query)
                root = workspace_root(query.get("path", [""])[0])
                self.json_response({"commands": repository_commands(root)})
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/git-panel":
            try:
                query = parse_qs(parsed.query)
                repository = existing_repo_path({"mode": "local", "path": query.get("path", [""])[0]})
                self.json_response(git_panel_state(repository))
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        shell_match = re.fullmatch(r"/api/shell-jobs/([a-f0-9]+)", parsed.path)
        if shell_match:
            try:
                query = parse_qs(parsed.query)
                self.json_response(public_shell_job(shell_match.group(1), max(0, int(query.get("since", ["0"])[0]))))
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/repo-search":
            try:
                query = parse_qs(parsed.query)
                root = Path(query.get("path", [""])[0]).expanduser().resolve()
                self.json_response(search_repository(root, query.get("q", [""])[0]))
            except ValueError as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            profile_id = query.get("profileId", [""])[0]
            try:
                since = max(0, int(query.get("since", ["0"])[0]))
            except ValueError:
                since = 0
            with lock:
                run = runs.get(profile_id)
            historical = None if run else persisted_run(profile_id, since)
            self.json_response(public_run(run, since) if run else historical or {
                "profileId": profile_id, "status": "idle", "logs": [], "sequence": 0
            })
            return
        if parsed.path.startswith("/ui/"):
            path = resolve_ui_asset(ROOT, parsed.path)
            if not path:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in ("/", "/office.html"):
            path = ROOT / "office.html"
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.request_json()
            if parsed.path == "/api/run":
                self.start_run(data)
                return
            if parsed.path == "/api/stop":
                self.stop_run(data)
                return
            if parsed.path == "/api/publish":
                self.json_response(publish_changes(data))
                return
            if parsed.path == "/api/publish-cupboard":
                self.json_response(publish_cupboard(data))
                return
            if parsed.path == "/api/repository-state":
                self.json_response(repository_state(data))
                return
            if parsed.path == "/api/cupboard-state":
                self.json_response(cupboard_state(data))
                return
            if parsed.path == "/api/discover-repositories":
                self.json_response(cupboard_manifest(data))
                return
            if parsed.path == "/api/floor-intent-resolve":
                self.json_response(floor_intent_resolution(data))
                return
            if parsed.path == "/api/select-directory":
                self.json_response(select_directory(data))
                return
            if parsed.path == "/api/browse-directory":
                self.json_response(browse_directory(data))
                return
            if parsed.path == "/api/file-context":
                root = Path(str(data.get("path", ""))).expanduser().resolve()
                self.json_response(file_context(root, data.get("references") or []))
                return
            if parsed.path == "/api/repo-file":
                root = workspace_root(data.get("root"))
                self.json_response(write_workspace_file(root, data.get("path"), data.get("content")))
                return
            if parsed.path == "/api/git-panel":
                self.json_response(git_panel_action(data))
                return
            if parsed.path == "/api/shell-jobs":
                self.json_response(start_shell_job(data), HTTPStatus.ACCEPTED)
                return
            shell_stop_match = re.fullmatch(r"/api/shell-jobs/([a-f0-9]+)/stop", parsed.path)
            if shell_stop_match:
                with lock:
                    job = shell_jobs.get(shell_stop_match.group(1))
                    process = job and job.get("process")
                if not job:
                    raise ValueError("Unknown shell job.")
                if process and process.poll() is None:
                    process.terminate()
                self.json_response({"ok": True, "id": shell_stop_match.group(1)})
                return
            if parsed.path == "/api/update-repositories":
                self.json_response(update_repositories(data.get("repository") or {}))
                return
            if parsed.path == "/api/plugin-suggestions":
                self.json_response(plugin_suggestions(data.get("repository") or {}))
                return
            if parsed.path == "/api/approve-plugin":
                self.json_response(approved_plugin_path(str(data.get("identifier", ""))))
                return
            if parsed.path == "/api/hooks/event":
                event = str(data.get("event", ""))
                emit_lifecycle_hook(event, data.get("payload") if isinstance(data.get("payload"), dict) else {})
                self.json_response({"ok": True, "event": event})
                return
            revert_match = re.fullmatch(r"/api/runs/(\d+)/revert", parsed.path)
            if revert_match:
                self.revert_run(int(revert_match.group(1)))
                return
            retry_match = re.fullmatch(r"/api/runs/(\d+)/retry", parsed.path)
            if retry_match:
                self.retry_run(int(retry_match.group(1)))
                return
            approve_match = re.fullmatch(r"/api/runs/(\d+)/(approve|deny)", parsed.path)
            if approve_match:
                self.decide_run(int(approve_match.group(1)), approve_match.group(2) == "approve")
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except StateConflictError as exc:
            self.json_response({"error": str(exc), **read_office_state()}, HTTPStatus.CONFLICT)
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/state":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = self.request_json(MAX_STATE_BODY)
            self.json_response(write_office_state(data))
        except StateConflictError as exc:
            self.json_response({"error": str(exc), **read_office_state()}, HTTPStatus.CONFLICT)
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def start_run(self, data: dict) -> None:
        profile_id = str(data.get("profileId", "")).strip()
        agent = str(data.get("agent", "")).strip().lower()
        task = str(data.get("task", "")).strip()
        prompt = str(data.get("prompt", "")).strip()
        repository = data.get("repository") or {}
        run_type = str(data.get("runType", "work")).strip().lower()
        session_id = str(data.get("sessionId", "")).strip() or None
        if not profile_id or agent not in ("codex", "claude") or not task or not prompt:
            raise ValueError("profileId, agent, task, and prompt are required.")
        if run_type not in SUPPORTED_RUN_TYPES:
            raise ValueError("Unsupported runType.")
        if len(profile_id) > 160 or len(task) > 500 or len(prompt) > 100_000:
            raise ValueError("Run request is too large.")
        if session_id and (len(session_id) > 160 or not re.fullmatch(r"[A-Za-z0-9._:-]+", session_id)):
            raise ValueError("Invalid agent session ID.")
        if not session_id and run_type in PERSISTENT_RUN_TYPES and not data.get("freshSession"):
            session_id = latest_session_id(profile_id, agent)
        requested_path_claims = normalized_path_claims(data.get("pathClaims"))
        with lock:
            previous = runs.get(profile_id)
            if previous and previous["status"] in ACTIVE_RUN_STATUSES:
                self.json_response({"error": "This agent session is already handling another turn."}, HTTPStatus.CONFLICT)
                return
            run = {
                "profileId": profile_id,
                "agent": agent,
                "task": task,
                "runType": run_type,
                "status": "starting",
                "startedAt": now_ms(),
                "endedAt": None,
                "returncode": None,
                "pid": None,
                "process": None,
                "result": None,
                "sessionId": session_id,
                "repository": repository,
                "prompt": prompt,
                "retryOf": data.get("retryOf"),
                "checkpointRef": None,
                "completionRef": None,
                "inputTokens": 0,
                "outputTokens": 0,
                "finalMessage": None,
                "errorMessage": None,
                "activityPhase": "queued",
                "activityLabel": "Queued",
                "activityUpdatedAt": now_ms(),
                "logs": deque(maxlen=MAX_LOG_LINES),
                "sequence": 0,
                "approvalCategories": permission_categories(repository, run_type),
                "verification": [],
                "requestedPathClaims": requested_path_claims,
                "pathClaims": scope_path_claims(repository, requested_path_claims),
                "pluginSuggestions": [],
            }
            runs[profile_id] = run
        persist_run_started(run)
        append_log(run, "system", f"Queued task: {task}")
        if run["approvalCategories"]:
            with lock:
                run["status"] = "awaiting_approval"
            set_activity(run, "approval", "Waiting for permission approval")
            append_log(run, "system", "Waiting for approval before granting: " + ", ".join(run["approvalCategories"]))
            persist_run_status(run)
            self.json_response(public_run(run), HTTPStatus.ACCEPTED)
            return
        self.launch_run(run)
        self.json_response(public_run(run), HTTPStatus.ACCEPTED)

    def launch_run(self, run: dict) -> None:
        with lock:
            run["status"] = "waiting_for_lock" if run.get("pathClaims") else "running"
        persist_run_status(run)
        threading.Thread(target=run_agent_with_path_locks, args=(run,), daemon=True).start()

    def decide_run(self, run_id: int, approved: bool) -> None:
        with lock:
            run = next((item for item in runs.values() if item.get("databaseRunId") == run_id), None)
            if not run:
                self.json_response({"error": "This approval is no longer active; retry the run."}, HTTPStatus.NOT_FOUND)
                return
            if run.get("status") != "awaiting_approval":
                raise ValueError("This run is not waiting for approval.")
        if approved:
            append_log(run, "system", "Permission request approved by the user")
            self.launch_run(run)
        else:
            with lock:
                run["status"] = "denied"
                run["endedAt"] = now_ms()
                run["errorMessage"] = "Permission request denied by the user."
            set_activity(run, "denied", "Permission denied")
            append_log(run, "system", "Permission request denied by the user")
            persist_run_status(run)
        self.json_response(public_run(run))

    def revert_run(self, run_id: int) -> None:
        with database_lock:
            row = require_database().execute(
                "SELECT checkpoint_ref, status FROM agent_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            self.json_response({"error": "Unknown run."}, HTTPStatus.NOT_FOUND)
            return
        if not row["checkpoint_ref"]:
            raise ValueError("This run has no repository checkpoint to restore.")
        checkpoint_repositories = {
            str(item.get("repository")) for item in json.loads(row["checkpoint_ref"] or "[]")
        }
        with lock:
            active_specs = [
                item.get("repository") or {} for item in runs.values()
                if item.get("status") in ACTIVE_RUN_STATUSES
            ]
        for spec in active_specs:
            try:
                active_paths = {str(path) for path in git_repositories_for_spec(spec)}
            except (ValueError, RuntimeError):
                continue
            if checkpoint_repositories & active_paths:
                raise ValueError("Wait for active runs in this repository to finish before reverting.")
        restored = restore_run_checkpoint(row["checkpoint_ref"])
        self.json_response({"ok": True, "runId": run_id, "repositories": restored})

    def retry_run(self, run_id: int) -> None:
        with database_lock:
            row = require_database().execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            self.json_response({"error": "Unknown run."}, HTTPStatus.NOT_FOUND)
            return
        if row["status"] not in ("failed", "interrupted"):
            raise ValueError("Only failed or interrupted runs can be retried.")
        with lock:
            active = runs.get(row["profile_id"])
            if active and active.get("status") in ACTIVE_RUN_STATUSES:
                self.json_response({"error": "This agent session is already handling another turn."}, HTTPStatus.CONFLICT)
                return
        if not row["repository_json"] or not row["prompt"]:
            raise ValueError("This older run does not retain enough information to retry.")
        if row["checkpoint_ref"]:
            restore_run_checkpoint(row["checkpoint_ref"])
        self.start_run({
            "profileId": row["profile_id"], "agent": row["agent"], "task": row["task"],
            "runType": row["run_type"], "prompt": row["prompt"],
            "repository": json.loads(row["repository_json"]), "sessionId": row["session_id"],
            "retryOf": run_id,
            "pathClaims": [
                {"repository": repository, "path": path}
                for repository, path in json.loads(row["path_claims_json"] or "[]")
            ],
        })

    def stop_run(self, data: dict) -> None:
        profile_id = str(data.get("profileId", "")).strip()
        with lock:
            run = runs.get(profile_id)
            process = run and run.get("process")
        if not run:
            self.json_response({"error": "Unknown profile."}, HTTPStatus.NOT_FOUND)
            return
        if run.get("status") in ("awaiting_approval", "waiting_for_lock"):
            with lock:
                run["status"] = "denied"
                run["endedAt"] = now_ms()
                run["errorMessage"] = "Permission request cancelled by the user."
            set_activity(run, "denied", "Permission request cancelled")
            append_log(run, "system", "Permission request cancelled by the user")
            persist_run_status(run)
            with path_lock_condition:
                path_lock_condition.notify_all()
        if process and process.poll() is None:
            set_activity(run, "stopping", "Stopping agent")
            process.terminate()
            append_log(run, "system", "Stop requested by user")
        self.json_response(public_run(run))


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve The Office and run local Claude/Codex agents.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--print-claude-plugin-policy", action="store_true",
        help="print the managed-settings fragment required for Claude native relevance suggestions",
    )
    args = parser.parse_args()
    if args.print_claude_plugin_policy:
        print(json.dumps(claude_plugin_suggestion_settings(), indent=2))
        return
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), OfficeHandler)
    print(f"the office: http://127.0.0.1:{args.port}")
    print(f"Storage: {DATABASE_PATH}")
    print(f"Codex: {find_cli('codex') or 'not found'}")
    print(f"Claude: {find_cli('claude') or 'not found'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
