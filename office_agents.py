"""Agent-specific command construction and event interpretation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable


class AgentAdapter:
    name = ""

    def command(self, executable: str, repository: Path, prompt: str, run_type: str, **options) -> list[str]:
        raise NotImplementedError

    def parse_activity(self, event: dict, command_activity: Callable[[object], tuple[str, str]]) -> tuple[str, str] | None:
        return None

    def extract_session_id(self, event: dict) -> str | None:
        value = event.get("thread_id") or event.get("session_id") or event.get("conversation_id")
        return str(value) if value else None

    def final_message(self, event: dict, current: str | None) -> str | None:
        return current

    def structured_result(self, event: dict, current: object) -> object:
        return current

    def authentication_failed(self, line: str) -> bool:
        return False

    def uses_structured_output_files(self) -> bool:
        return False


class CodexAdapter(AgentAdapter):
    name = "codex"

    def uses_structured_output_files(self) -> bool:
        return True

    def command(self, executable: str, repository: Path, prompt: str, run_type: str, **options) -> list[str]:
        session_id = options.get("session_id")
        schema_path, output_path = options.get("schema_path"), options.get("output_path")
        structured = run_type in ("onboard", "plan", "review", "orchestrate", "report", "reception", "floor_call", "floor_intent")
        if session_id:
            command = [executable, "exec", "resume", "--json"]
            if structured:
                command += ["--output-schema", schema_path, "-o", output_path]
            return command + [session_id, prompt]
        command = [executable, "exec", "--json"]
        options["add_codex_mcp_overrides"](command, options.get("mcp_servers") or [])
        if run_type in ("onboard", "plan", "review", "chat", "question", "report", "reception", "floor_call", "floor_intent"):
            command += ["--sandbox", "read-only"]
            if run_type == "chat" or options.get("allow_non_git"):
                command += ["--skip-git-repo-check"]
            if structured:
                command += ["--output-schema", schema_path, "-o", output_path]
        else:
            command += ["--approve-for-me"]
            if options.get("allow_non_git"):
                command += ["--skip-git-repo-check"]
            if run_type == "orchestrate":
                command += ["--output-schema", schema_path, "-o", output_path]
        if run_type in ("reception", "plan", "review", "floor_intent"):
            model = os.environ.get("TASK_OFFICE_CODEX_CLASSIFIER_MODEL", "gpt-5.1-codex-mini").strip()
            if model:
                command += ["--model", model]
        return command + ["-C", str(repository), prompt]

    def parse_activity(self, event: dict, command_activity: Callable[[object], tuple[str, str]]) -> tuple[str, str] | None:
        event_type, item = str(event.get("type", "")), event.get("item") or {}
        item_type = str(item.get("type", ""))
        if item_type == "command_execution": return command_activity(item.get("command"))
        if item_type in ("file_change", "file_changes"): return "editing", "Editing code"
        if item_type == "reasoning": return "thinking", "Thinking through the task"
        if item_type == "web_search": return "researching", "Researching"
        if item_type == "mcp_tool_call": return "tool", "Using a development tool"
        if event_type == "turn.started": return "thinking", "Starting the task"
        if event_type == "turn.completed": return "wrapping", "Wrapping up"
        return None

    def final_message(self, event: dict, current: str | None) -> str | None:
        item = event.get("item") or {}
        return item.get("text") if event.get("type") == "item.completed" and item.get("type") == "agent_message" else current


class ClaudeAdapter(AgentAdapter):
    name = "claude"

    def command(self, executable: str, repository: Path, prompt: str, run_type: str, **options) -> list[str]:
        command = [executable, "-p", prompt, "--output-format", "stream-json", "--verbose", "--permission-mode"]
        command += ["plan" if run_type in ("onboard", "plan", "review", "chat", "question", "report", "reception", "floor_call", "floor_intent") else "acceptEdits"]
        if run_type == "chat": command += ["--tools", ""]
        if run_type in options["lightweight_run_types"]:
            settings = json.dumps({"disableAllHooks": True, "disableBundledSkills": True, "autoMemoryEnabled": False}, separators=(",", ":"))
            # Claude validates --mcp-config as an MCP config document, even when
            # strict mode intentionally disables every server for classifier runs.
            # An empty object has no mcpServers record and newer CLIs reject it.
            empty_mcp_config = json.dumps({"mcpServers": {}}, separators=(",", ":"))
            command += ["--settings", settings, "--strict-mcp-config", "--mcp-config", empty_mcp_config]
        else:
            command += ["--settings", json.dumps(options["plugin_settings"], separators=(",", ":"))]
            servers = options.get("mcp_servers") or []
            if servers: command += ["--strict-mcp-config", "--mcp-config", json.dumps(options["claude_mcp_config"](servers), separators=(",", ":"))]
            for path in options.get("plugin_paths") or []: command += ["--plugin-dir", path]
        if run_type in ("reception", "plan", "review", "floor_intent"):
            model = os.environ.get("TASK_OFFICE_CLAUDE_CLASSIFIER_MODEL", "haiku").strip()
            if model: command += ["--model", model]
        if run_type in ("onboard", "plan", "review", "orchestrate", "report", "reception", "floor_call", "floor_intent"):
            command += ["--json-schema", json.dumps(options.get("output_schema"))]
        if options.get("session_id"): command += ["--resume", options["session_id"]]
        return command

    def parse_activity(self, event: dict, command_activity: Callable[[object], tuple[str, str]]) -> tuple[str, str] | None:
        if event.get("type") != "assistant": return None
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use": continue
            name, tool_input = str(block.get("name", "")).lower(), block.get("input") or {}
            if name in ("edit", "write", "multiedit", "notebookedit"): return "editing", "Editing code"
            if name in ("bash", "shell", "exec", "execute"): return command_activity(tool_input.get("command"))
            if name in ("read", "glob", "grep", "ls", "search"): return "inspecting", "Reading the codebase"
            if name in ("task", "agent", "dispatch_agent", "send_message"): return "delegating", "Coordinating employees"
            return "tool", "Using a development tool"
        return None

    def final_message(self, event: dict, current: str | None) -> str | None:
        return event.get("result") if event.get("type") == "result" and isinstance(event.get("result"), str) else current

    def structured_result(self, event: dict, current: object) -> object:
        return (event.get("structured_output") or event.get("result")) if event.get("type") == "result" else current

    def authentication_failed(self, line: str) -> bool:
        return "authentication_failed" in line or "Invalid API key" in line


ADAPTERS = {adapter.name: adapter for adapter in (CodexAdapter(), ClaudeAdapter())}


def adapter_for(name: str) -> AgentAdapter:
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise ValueError("Agent must be claude or codex.") from exc
