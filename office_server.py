#!/usr/bin/env python3
"""Local The Office server: serves the UI and runs Claude/Codex workers."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
MAX_BODY = 1_000_000
SUPPORTED_RUN_TYPES = (
    "work", "chat", "question", "onboard", "plan", "review", "orchestrate",
    "report", "reception", "floor_call",
)
MAX_LOG_LINES = 5000
lock = threading.RLock()
clone_lock = threading.Lock()
runs: dict[str, dict] = {}

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
                "required": ["title", "note"],
                "properties": {
                    "title": {"type": "string"},
                    "note": {"type": "string"},
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
                "required": ["floor_id", "title", "note", "role"],
                "properties": {
                    "floor_id": {"type": "string"},
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "role": {"type": "string"},
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

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ready", "summary", "issues", "suggested_branch", "pr_title", "pr_body"],
    "properties": {
        "ready": {"type": "boolean"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "suggested_branch": {"type": "string"},
        "pr_title": {"type": "string"},
        "pr_body": {"type": "string"},
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
    "required": ["summary", "architecture", "conventions", "test_commands", "risk_areas", "key_files"],
    "properties": {
        "summary": {"type": "string"},
        "architecture": {"type": "string"},
        "conventions": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "test_commands": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "risk_areas": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "key_files": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
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


def append_log(run: dict, stream: str, text: str) -> None:
    with lock:
        run["sequence"] += 1
        run["logs"].append({
            "sequence": run["sequence"],
            "time": now_ms(),
            "stream": stream,
            "text": text.rstrip("\n"),
        })


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
        "jest", "vitest", "phpunit", "dotnet test",
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


def update_activity_from_event(run: dict, event: dict) -> None:
    if run["agent"] == "codex":
        event_type = str(event.get("type", ""))
        item = event.get("item") or {}
        item_type = str(item.get("type", ""))
        if item_type == "command_execution":
            set_activity(run, *command_activity(item.get("command")))
        elif item_type in ("file_change", "file_changes"):
            set_activity(run, "editing", "Editing code")
        elif item_type == "reasoning":
            set_activity(run, "thinking", "Thinking through the task")
        elif item_type == "web_search":
            set_activity(run, "researching", "Researching")
        elif item_type == "mcp_tool_call":
            set_activity(run, "tool", "Using a development tool")
        elif event_type == "turn.started":
            set_activity(run, "thinking", "Starting the task")
        elif event_type == "turn.completed":
            set_activity(run, "wrapping", "Wrapping up")
        return

    if event.get("type") != "assistant":
        return
    message = event.get("message") or {}
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name", "")).lower()
        tool_input = block.get("input") or {}
        if name in ("edit", "write", "multiedit", "notebookedit"):
            set_activity(run, "editing", "Editing code")
        elif name in ("bash", "shell", "exec", "execute"):
            set_activity(run, *command_activity(tool_input.get("command")))
        elif name in ("read", "glob", "grep", "ls", "search"):
            set_activity(run, "inspecting", "Reading the codebase")
        elif name in ("task", "agent", "dispatch_agent", "send_message"):
            set_activity(run, "delegating", "Coordinating employees")
        else:
            set_activity(run, "tool", "Using a development tool")


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


def publish_changes(data: dict) -> dict:
    repository = existing_repo_path(data.get("repository") or {})
    branch = str(data.get("branch", "")).strip()
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    if not branch or len(branch) > 180 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch):
        raise ValueError("Enter a valid branch name.")
    if ".." in branch or branch.endswith("/") or not title or len(title) > 240 or len(body) > 50_000:
        raise ValueError("Invalid branch or pull-request details.")
    git = shutil.which("git")
    gh = find_cli("gh")
    if not git:
        raise RuntimeError("git is not installed or is not on PATH.")
    if not gh:
        raise RuntimeError("GitHub CLI (gh) is required to create the pull request.")
    status = checked_command([git, "status", "--porcelain"], repository)
    if not status:
        raise ValueError("There are no uncommitted changes to publish.")
    current = checked_command([git, "branch", "--show-current"], repository)
    if current != branch:
        exists = subprocess.run(
            [git, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repository
        ).returncode == 0
        if exists:
            raise ValueError(f"Local branch {branch} already exists; choose another name.")
        checked_command([git, "switch", "-c", branch], repository)
    checked_command([git, "add", "-A"], repository)
    checked_command([git, "commit", "-m", title], repository)
    checked_command([git, "push", "-u", "origin", branch], repository, timeout=900)
    pr_output = checked_command([gh, "pr", "create", "--title", title, "--body", body], repository, timeout=300)
    return {"branch": branch, "pullRequest": pr_output.splitlines()[-1] if pr_output else "created"}


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
        "branch": branch, "upstream": upstream or None, "dirty": dirty,
        "ahead": ahead, "behind": behind, "remoteBranches": remote_branches,
        "remoteHeadMatches": remote_head_matches,
        "pushed": not dirty and (locally_pushed or remote_head_matches),
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


def agent_command(
    agent: str,
    repository: Path,
    prompt: str,
    run_type: str,
    schema_path: str | None = None,
    output_path: str | None = None,
    output_schema: dict | None = None,
    session_id: str | None = None,
) -> list[str]:
    executable = find_cli(agent)
    if not executable:
        raise RuntimeError(f"{agent} CLI is not installed or is not on PATH.")
    if agent == "codex":
        if session_id:
            command = [executable, "exec", "resume", "--json"]
            if run_type == "chat":
                command += ["--skip-git-repo-check"]
            if run_type in ("question", "review", "report", "floor_call"):
                command += ["-c", 'sandbox_mode="read-only"']
            if run_type in ("onboard", "plan", "review", "orchestrate", "report", "reception", "floor_call"):
                command += ["--output-schema", schema_path, "-o", output_path]
            return command + [session_id, prompt]
        command = [
            executable,
            "exec",
            "--json",
        ]
        if run_type in ("review", "chat", "question", "report", "reception", "floor_call"):
            command += ["--sandbox", "read-only"]
            if run_type == "chat":
                command += ["--skip-git-repo-check"]
            if run_type in ("review", "report", "reception", "floor_call"):
                command += ["--output-schema", schema_path, "-o", output_path]
        else:
            # --approve-for-me already selects the workspace-write policy and cannot be
            # combined with an explicit --sandbox value in current Codex builds.
            command += ["--approve-for-me"]
            if run_type in ("onboard", "plan", "orchestrate"):
                command += ["--output-schema", schema_path, "-o", output_path]
        return command + ["-C", str(repository), prompt]
    if agent == "claude":
        command = [
            executable,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
        ]
        command += ["plan" if run_type in ("review", "chat", "question", "report", "reception", "floor_call") else "acceptEdits"]
        if run_type == "chat":
            command += ["--tools", ""]
        if run_type in ("onboard", "plan", "review", "orchestrate", "report", "reception", "floor_call"):
            command += ["--json-schema", json.dumps(output_schema)]
        if session_id:
            command += ["--resume", session_id]
        return command
    raise ValueError("Agent must be claude or codex.")


def run_agent(run: dict, repository_spec: dict, prompt: str) -> None:
    temporary_paths: list[str] = []
    try:
        repository = repo_path(repository_spec, run)
        schema_path = output_path = None
        output_schema = (
            ONBOARD_SCHEMA if run["runType"] == "onboard"
            else PLAN_SCHEMA if run["runType"] == "plan"
            else REVIEW_SCHEMA if run["runType"] in ("review", "orchestrate")
            else REPORT_SCHEMA if run["runType"] == "report"
            else RECEPTION_SCHEMA if run["runType"] == "reception"
            else FLOOR_CALL_SCHEMA if run["runType"] == "floor_call"
            else None
        )
        if output_schema and run["agent"] == "codex":
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as schema_file:
                json.dump(output_schema, schema_file)
                schema_path = schema_file.name
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as output_file:
                output_path = output_file.name
            temporary_paths += [schema_path, output_path]
        command = agent_command(
            run["agent"], repository, prompt, run["runType"], schema_path, output_path,
            output_schema, run.get("sessionId")
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
                if run["agent"] == "claude" and ("authentication_failed" in line or "Invalid API key" in line):
                    claude_auth_failed = True
                try:
                    event = json.loads(line)
                    update_activity_from_event(run, event)
                    discovered_session = (
                        event.get("thread_id") or event.get("session_id")
                        or event.get("conversation_id")
                    )
                    if discovered_session:
                        with lock:
                            run["sessionId"] = str(discovered_session)
                    if run["agent"] == "codex":
                        item = event.get("item") or {}
                        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                            final_message = item.get("text") or final_message
                    elif event.get("type") == "result":
                        if isinstance(event.get("result"), str):
                            final_message = event["result"]
                        if output_schema:
                            claude_result = event.get("structured_output") or event.get("result")
                except json.JSONDecodeError:
                    pass
            returncode = process.wait()
            if (
                run["agent"] == "claude" and returncode != 0 and claude_auth_failed
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
            if run["runType"] == "report" and not isinstance(candidate.get("executive_summary"), str):
                raise RuntimeError("The employee returned an invalid report.")
            if run["runType"] == "reception" and not isinstance(candidate.get("routes"), list):
                raise RuntimeError("Reception returned an invalid routing plan.")
            if run["runType"] == "floor_call" and not isinstance(candidate.get("answer"), str):
                raise RuntimeError("The consulted floor returned an invalid answer.")
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
        for temporary_path in temporary_paths:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def public_run(run: dict, since: int = 0) -> dict:
    with lock:
        return {
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
            "logs": [item for item in run["logs"] if item["sequence"] > since],
            "sequence": run["sequence"],
        }


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

    def request_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Invalid request size.")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.json_response({
                "ok": True,
                "version": 2,
                "runTypes": list(SUPPORTED_RUN_TYPES),
                "agents": {"codex": bool(find_cli("codex")), "claude": bool(find_cli("claude"))},
            })
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
            self.json_response(public_run(run, since) if run else {
                "profileId": profile_id, "status": "idle", "logs": [], "sequence": 0
            })
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
            if parsed.path == "/api/repository-state":
                self.json_response(repository_state(data))
                return
            if parsed.path == "/api/select-directory":
                self.json_response(select_directory(data))
                return
            if parsed.path == "/api/browse-directory":
                self.json_response(browse_directory(data))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
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
        with lock:
            previous = runs.get(profile_id)
            if previous and previous["status"] in ("starting", "running"):
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
                "finalMessage": None,
                "errorMessage": None,
                "activityPhase": "queued",
                "activityLabel": "Queued",
                "activityUpdatedAt": now_ms(),
                "logs": deque(maxlen=MAX_LOG_LINES),
                "sequence": 0,
            }
            runs[profile_id] = run
        append_log(run, "system", f"Queued task: {task}")
        with lock:
            run["status"] = "running"
        threading.Thread(target=run_agent, args=(run, repository, prompt), daemon=True).start()
        self.json_response(public_run(run), HTTPStatus.ACCEPTED)

    def stop_run(self, data: dict) -> None:
        profile_id = str(data.get("profileId", "")).strip()
        with lock:
            run = runs.get(profile_id)
            process = run and run.get("process")
        if not run:
            self.json_response({"error": "Unknown profile."}, HTTPStatus.NOT_FOUND)
            return
        if process and process.poll() is None:
            set_activity(run, "stopping", "Stopping agent")
            process.terminate()
            append_log(run, "system", "Stop requested by user")
        self.json_response(public_run(run))


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve The Office and run local Claude/Codex agents.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), OfficeHandler)
    print(f"the office: http://127.0.0.1:{args.port}")
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
