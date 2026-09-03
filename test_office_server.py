import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import office_server as app


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_directory = app.DATA_DIRECTORY
        self.original_database_path = app.DATABASE_PATH
        app.DATA_DIRECTORY = Path(self.temporary_directory.name)
        app.DATABASE_PATH = app.DATA_DIRECTORY / "office.db"
        app.initialize_database()
        app.runs.clear()

    def tearDown(self):
        if app.database is not None:
            app.database.close()
            app.database = None
        app.DATA_DIRECTORY = self.original_data_directory
        app.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()
        app.runs.clear()

    def test_state_is_revisioned_and_snapshotted(self):
        first = {"schemaVersion": 6, "value": 1}
        second = {"schemaVersion": 6, "value": 2}
        saved = app.write_office_state({"expectedRevision": 0, "state": first})
        self.assertEqual(saved["revision"], 1)
        app.write_office_state({"expectedRevision": 1, "state": second})
        self.assertEqual(app.read_office_state()["state"], second)
        with self.assertRaises(app.StateConflictError):
            app.write_office_state({"expectedRevision": 1, "state": first})
        with app.database_lock:
            snapshots = app.database.execute("SELECT revision FROM state_snapshots").fetchall()
        self.assertEqual([row["revision"] for row in snapshots], [1])

    def test_custom_plugin_directories_are_shared_across_floors(self):
        plugin = Path(self.temporary_directory.name) / "custom-plugin"
        plugin.mkdir()
        app.write_office_state({"expectedRevision": 0, "state": {
            "schemaVersion": 8,
            "floors": [{"id": "one", "pluginPaths": [str(plugin)]}, {"id": "two", "pluginPaths": []}],
            "workers": [],
        }})
        self.assertIn(str(plugin.resolve()), app.normalized_plugin_paths({"pluginPaths": []}))

    def test_run_and_logs_survive_restart(self):
        run = {
            "profileId": "lead:floor-1", "agent": "codex", "task": "Persist this run",
            "runType": "plan", "status": "running", "startedAt": app.now_ms(),
            "endedAt": None, "returncode": None, "result": None, "sessionId": "session-1",
            "finalMessage": None, "errorMessage": None, "activityPhase": "thinking",
            "activityLabel": "Working", "activityUpdatedAt": app.now_ms(),
            "logs": deque(maxlen=app.MAX_LOG_LINES), "sequence": 0,
        }
        app.persist_run_started(run)
        app.append_log(run, "agent", "work in progress")
        app.database.close()
        app.database = None

        app.initialize_database()
        restored = app.persisted_run("lead:floor-1")
        self.assertEqual(restored["status"], "failed")
        self.assertEqual(restored["persistedStatus"], "interrupted")
        self.assertEqual(restored["logs"][0]["text"], "work in progress")
        self.assertEqual(len(list((app.DATA_DIRECTORY / "backups").glob("office-*.db"))), 1)
        self.assertEqual(app.latest_session_id("lead:floor-1", "codex"), "session-1")

    def test_session_usage_counts_persisted_turns(self):
        for index in range(2):
            run = {
                "profileId": "lead:floor-1", "agent": "codex", "task": f"turn {index}",
                "runType": "work", "status": "completed", "startedAt": app.now_ms() + index,
                "endedAt": app.now_ms(), "returncode": 0, "result": None,
                "sessionId": "session-shared", "inputTokens": 10, "outputTokens": 5,
                "activityPhase": "completed", "activityLabel": "Completed",
                "activityUpdatedAt": app.now_ms(), "logs": deque(maxlen=app.MAX_LOG_LINES),
                "sequence": 0,
            }
            app.persist_run_started(run)
            app.persist_run_status(run)
        restored = app.persisted_run("lead:floor-1")
        self.assertEqual(restored["sessionTurns"], 2)
        self.assertEqual(restored["sessionInputTokens"], 20)

    def test_state_http_api(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.OfficeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def request(path, method="GET", payload=None):
            body = None if payload is None else json.dumps(payload).encode()
            call = urllib.request.Request(
                base + path, data=body, method=method, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(call, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        try:
            state = {"schemaVersion": 6, "floors": [{"id": "floor-1"}], "workers": []}
            status, empty = request("/api/state")
            self.assertEqual((status, empty["exists"]), (200, False))
            status, saved = request("/api/state", "PUT", {"expectedRevision": 0, "state": state})
            self.assertEqual((status, saved["revision"]), (200, 1))
            status, exported = request("/api/state/export")
            self.assertEqual((status, exported["state"]), (200, state))
            status, conflict = request("/api/state", "PUT", {"expectedRevision": 0, "state": state})
            self.assertEqual((status, conflict["revision"]), (409, 1))
            with urllib.request.urlopen(base + "/ui/settings.js", timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(), "text/javascript")
                self.assertIn("pluginSuggestionMarkup", response.read().decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @mock.patch.object(app, "run_agent")
    def test_permission_gate_waits_for_explicit_approval(self, run_agent):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.OfficeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        payload = {
            "profileId": "worker:permission", "agent": "codex", "task": "edit",
            "runType": "work", "prompt": "edit safely",
            "repository": {"mode": "local", "path": self.temporary_directory.name,
                           "permissions": {"requireConfirmationFor": ["file_edits"]}},
        }
        try:
            request = urllib.request.Request(
                base + "/api/run", method="POST", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                waiting = json.load(response)
            self.assertEqual(waiting["status"], "awaiting_approval")
            run_agent.assert_not_called()
            approve = urllib.request.Request(
                base + f"/api/runs/{waiting['databaseRunId']}/approve", method="POST", data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(approve, timeout=5) as response:
                approved = json.load(response)
            self.assertEqual(approved["status"], "running")
            for _ in range(20):
                if run_agent.called:
                    break
                threading.Event().wait(.01)
            run_agent.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class GitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name) / "repo"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repository, check=True)
        (self.repository / "app.py").write_text("one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\n")
        subprocess.run(["git", "add", "app.py"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.repository, check=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_diff_includes_untracked_and_selects_individual_hunks(self):
        original = "".join(f"line {index}\n" for index in range(1, 41))
        (self.repository / "app.py").write_text(original)
        subprocess.run(["git", "add", "app.py"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-qm", "expand fixture"], cwd=self.repository, check=True)
        changed = original.replace("line 1\n", "FIRST\n").replace("line 40\n", "LAST\n")
        (self.repository / "app.py").write_text(changed)
        (self.repository / "new.txt").write_text("new file\n")
        diff = app.repository_diff(self.repository)
        self.assertEqual({item["path"] for item in diff["files"]}, {"app.py", "new.txt"})
        app_file = next(item for item in diff["files"] if item["path"] == "app.py")
        self.assertTrue(next(item for item in diff["files"] if item["path"] == "new.txt")["atomic"])
        self.assertEqual(len(app_file["hunks"]), 2)
        chosen = app_file["hunks"][0]["id"]
        patch = app.selected_patch(self.repository, {"digest": diff["digest"], "accepted": [chosen]})
        self.assertIn("app.py", patch)
        self.assertNotIn("new file", patch)
        app.stage_selected_changes(self.repository, {"digest": diff["digest"], "accepted": [chosen]})
        staged = subprocess.run(
            ["git", "diff", "--cached"], cwd=self.repository, text=True, stdout=subprocess.PIPE, check=True
        ).stdout
        remaining = subprocess.run(
            ["git", "diff"], cwd=self.repository, text=True, stdout=subprocess.PIPE, check=True
        ).stdout
        self.assertIn("FIRST", staged)
        self.assertNotIn("LAST", staged)
        self.assertIn("LAST", remaining)
        self.assertTrue((self.repository / "new.txt").exists())

    def test_checkpoint_restores_tracked_and_untracked_content(self):
        (self.repository / "before.txt").write_text("keep me\n")
        checkpoint = app.create_run_checkpoint({"mode": "local", "path": str(self.repository)}, 42)
        (self.repository / "app.py").write_text("worker edit\n")
        (self.repository / "before.txt").write_text("changed\n")
        (self.repository / "created.txt").write_text("remove me\n")
        restored = app.restore_run_checkpoint(checkpoint)
        self.assertEqual(restored, [str(self.repository)])
        self.assertTrue((self.repository / "app.py").read_text().startswith("one\n"))
        self.assertEqual((self.repository / "before.txt").read_text(), "keep me\n")
        self.assertFalse((self.repository / "created.txt").exists())

    def test_checkpoint_diff_tracks_changes_since_run_start(self):
        checkpoint = app.create_run_checkpoint({"mode": "local", "path": str(self.repository)}, 43)
        (self.repository / "app.py").write_text("after checkpoint\n")
        timeline = app.checkpoint_diff(checkpoint)
        self.assertEqual(timeline["fileCount"], 1)
        self.assertIn("after checkpoint", timeline["repositories"][0]["patch"])

    def test_run_timeline_uses_immutable_completion_snapshot(self):
        checkpoint = app.create_run_checkpoint({"mode": "local", "path": str(self.repository)}, 201)
        (self.repository / "app.py").write_text("completed state\n")
        completion = app.create_run_completion({"mode": "local", "path": str(self.repository)}, 201)
        (self.repository / "app.py").write_text("later unrelated state\n")
        timeline = app.checkpoint_diff(checkpoint, completion)
        patch = timeline["repositories"][0]["patch"]
        self.assertIn("completed state", patch)
        self.assertNotIn("later unrelated state", patch)

    def test_plugin_suggestions_are_loaded_from_manifest_rules(self):
        (self.repository / "requirements.txt").write_text("psycopg==3.2\n")
        suggestions = app.plugin_suggestions({"mode": "local", "path": str(self.repository)})
        self.assertIn("postgresql", {item["id"] for item in suggestions["suggestions"]})
        self.assertTrue(all(item["requiresApproval"] for item in suggestions["suggestions"]))

    def test_stale_diff_selection_is_rejected(self):
        (self.repository / "app.py").write_text("changed\n")
        diff = app.repository_diff(self.repository)
        (self.repository / "app.py").write_text("changed again\n")
        with self.assertRaisesRegex(ValueError, "changed after review"):
            app.selected_patch(self.repository, {"digest": diff["digest"], "accepted": []})

    def test_context_update_skips_missing_upstream_and_blocks_dirty_worktree(self):
        clean = app.update_repositories({"mode": "local", "path": str(self.repository)})
        self.assertEqual(clean["repositories"][0]["status"], "skipped")
        (self.repository / "app.py").write_text("local edit\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            app.update_repositories({"mode": "local", "path": str(self.repository)})

    def test_workspace_editor_rejects_escape_and_saves_text(self):
        loaded = app.read_workspace_file(self.repository, "app.py")
        self.assertIn("one", loaded["content"])
        saved = app.write_workspace_file(self.repository, "app.py", "edited\n")
        self.assertTrue(saved["ok"])
        self.assertEqual((self.repository / "app.py").read_text(), "edited\n")
        with self.assertRaisesRegex(ValueError, "safe workspace-relative"):
            app.read_workspace_file(self.repository, "../outside.txt")

    def test_repo_commands_and_git_panel_cover_daily_work(self):
        (self.repository / "package.json").write_text(json.dumps({"scripts": {"test": "echo ok", "build": "echo build"}}))
        commands = app.repository_commands(self.repository)
        self.assertEqual({item["command"] for item in commands}, {"npm run test", "npm run build"})
        state = app.git_panel_state(self.repository)
        self.assertTrue(state["branch"])
        self.assertIn("package.json", {item["path"] for item in state["changes"]})
        (self.repository / "scratch.txt").write_text("discard\n")
        with self.assertRaisesRegex(ValueError, "explicit confirmation"):
            app.git_panel_action({"path": str(self.repository), "action": "discard", "file": "scratch.txt"})
        app.git_panel_action({"path": str(self.repository), "action": "discard", "file": "scratch.txt", "confirmed": True})
        self.assertFalse((self.repository / "scratch.txt").exists())

    def test_shell_job_streams_scoped_output(self):
        job = app.start_shell_job({"path": str(self.repository), "command": "printf 'hello\\n'"})
        for _ in range(100):
            current = app.public_shell_job(job["id"])
            if current["status"] != "running":
                break
            threading.Event().wait(.01)
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["lines"], ["hello"])

    def test_diff_and_fresh_file_context_http_apis(self):
        (self.repository / "app.py").write_text("current content\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.OfficeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            encoded_path = urllib.parse.quote(str(self.repository), safe="")
            with urllib.request.urlopen(f"{base}/api/repo-diff?path={encoded_path}", timeout=5) as response:
                diff = json.load(response)
            self.assertEqual(diff["files"][0]["path"], "app.py")
            request = urllib.request.Request(
                base + "/api/file-context", method="POST",
                data=json.dumps({"path": str(self.repository), "references": ["app.py", "../secret"]}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                context = json.load(response)
            self.assertEqual(context["files"], [{"path": "app.py", "content": "current content\n", "truncated": False}])
            with urllib.request.urlopen(f"{base}/api/repo-search?path={encoded_path}&q=current", timeout=5) as response:
                search = json.load(response)
            self.assertTrue(any("app.py" in match for match in search["matches"]))
            with urllib.request.urlopen(f"{base}/api/repo-tree?path={encoded_path}", timeout=5) as response:
                tree = json.load(response)
            self.assertIn("app.py", tree["files"])
            with urllib.request.urlopen(f"{base}/api/repo-file?root={encoded_path}&path=app.py", timeout=5) as response:
                opened = json.load(response)
            self.assertEqual(opened["content"], "current content\n")
            save = urllib.request.Request(
                base + "/api/repo-file", method="POST",
                data=json.dumps({"root": str(self.repository), "path": "app.py", "content": "saved in browser\n"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(save, timeout=5) as response:
                self.assertTrue(json.load(response)["ok"])
            with urllib.request.urlopen(f"{base}/api/git-panel?path={encoded_path}", timeout=5) as response:
                self.assertTrue(json.load(response)["branch"])
            command = urllib.request.Request(
                base + "/api/shell-jobs", method="POST",
                data=json.dumps({"path": str(self.repository), "command": "printf routed"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(command, timeout=5) as response:
                shell = json.load(response)
            for _ in range(100):
                with urllib.request.urlopen(f"{base}/api/shell-jobs/{shell['id']}", timeout=5) as response:
                    shell_state = json.load(response)
                if shell_state["status"] != "running":
                    break
                threading.Event().wait(.01)
            self.assertEqual((shell_state["status"], shell_state["lines"]), ("completed", ["routed"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class AgentCommandTests(unittest.TestCase):
    @mock.patch.object(app, "find_cli", return_value="/usr/bin/agent")
    def test_claude_classification_is_lightweight_and_work_is_not(self, _find_cli):
        plan = app.agent_command("claude", Path("/tmp"), "prompt", "plan", output_schema={})
        work = app.agent_command("claude", Path("/tmp"), "prompt", "work")
        self.assertIn("--strict-mcp-config", plan)
        self.assertEqual(
            json.loads(plan[plan.index("--mcp-config") + 1]),
            {"mcpServers": {}},
        )
        self.assertIn("--settings", plan)
        self.assertIn("--model", plan)
        self.assertNotIn("--strict-mcp-config", work)
        self.assertNotIn("--model", work)
        self.assertNotIn("pluginSuggestionMarketplaces", plan[plan.index("--settings") + 1])
        self.assertNotIn("pluginSuggestionMarketplaces", work[work.index("--settings") + 1])
        for plugin in app.bundled_plugin_paths():
            self.assertIn(plugin, work)
        self.assertNotIn("--plugin-dir", plan)

    @mock.patch.object(app, "find_cli", return_value="/usr/bin/agent")
    def test_claude_floor_intent_classification_is_lightweight(self, _find_cli):
        floor_intent = app.agent_command("claude", Path("/tmp"), "prompt", "floor_intent", output_schema={})
        work = app.agent_command("claude", Path("/tmp"), "prompt", "work")
        self.assertIn("--strict-mcp-config", floor_intent)
        self.assertEqual(
            json.loads(floor_intent[floor_intent.index("--mcp-config") + 1]),
            {"mcpServers": {}},
        )
        self.assertIn("--settings", floor_intent)
        self.assertIn("--model", floor_intent)
        self.assertIn("--json-schema", floor_intent)
        self.assertEqual(
            floor_intent[floor_intent.index("--permission-mode") + 1], "plan"
        )
        self.assertNotIn("--strict-mcp-config", work)
        self.assertNotIn("--model", work)

    @mock.patch.object(app, "find_cli", return_value="/usr/bin/agent")
    def test_codex_floor_intent_fresh_run_uses_sandbox_and_classifier_model(self, _find_cli):
        command = app.agent_command(
            "codex", Path("/tmp"), "prompt", "floor_intent", "/tmp/schema", "/tmp/out", {}
        )
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--model", command)
        self.assertIn("--output-schema", command)
        self.assertNotIn("--approve-for-me", command)

    @mock.patch.object(app, "find_cli", return_value="/usr/bin/agent")
    def test_codex_resume_omits_fresh_run_shaping_flags(self, _find_cli):
        command = app.agent_command(
            "codex", Path("/tmp"), "prompt", "review", "/tmp/schema", "/tmp/out", {}, "session-1"
        )
        self.assertEqual(command[:4], ["/usr/bin/agent", "exec", "resume", "--json"])
        self.assertNotIn("--model", command)
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("--approve-for-me", command)

    @mock.patch.object(app, "find_cli", return_value="/usr/bin/agent")
    def test_floor_mcp_configuration_is_added_to_fresh_agent_commands(self, _find_cli):
        spec = {"mcpServers": [{
            "name": "docs", "type": "http", "url": "https://example.com/mcp",
            "enabledTools": ["search"], "approvalMode": "prompt",
        }]}
        codex = app.agent_command("codex", Path("/tmp"), "prompt", "work", repository_spec=spec)
        claude = app.agent_command("claude", Path("/tmp"), "prompt", "work", repository_spec=spec)
        self.assertTrue(any("mcp_servers.docs.url" in value for value in codex))
        self.assertIn("--mcp-config", claude)
        self.assertIn("https://example.com/mcp", claude[claude.index("--mcp-config") + 1])

    def test_permission_categories_only_gate_implementation(self):
        spec = {"permissions": {"requireConfirmationFor": ["file_edits", "network_access", "unknown"]}}
        self.assertEqual(app.permission_categories(spec, "work"), ["file_edits", "network_access"])
        self.assertEqual(app.permission_categories(spec, "plan"), [])
        self.assertEqual(
            app.permission_categories({"permissions": {"autoApproveEdits": False}}, "work"),
            ["file_edits"],
        )

    def test_invalid_mcp_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "needs a command"):
            app.normalized_mcp_servers({"mcpServers": [{"name": "broken", "type": "stdio"}]})

    def test_verification_counts_parses_common_test_output(self):
        self.assertEqual(app.verification_counts("12 passed, 2 skipped in 1.2s"), {"passed": 12, "skipped": 2})

    def test_overlapping_path_claims_wait_and_independent_claims_do_not(self):
        app.active_path_claims.clear()
        first = [("repo", "src")]
        overlap = [("repo", "src/app.py")]
        independent = [("repo", "tests")]
        self.assertTrue(app.acquire_path_claims("first", first, {"status": "running"}))
        acquired_overlap = threading.Event()
        waiter_run = {"status": "waiting_for_lock"}
        waiter = threading.Thread(
            target=lambda: acquired_overlap.set() if app.acquire_path_claims("second", overlap, waiter_run) else None
        )
        waiter.start()
        self.assertFalse(acquired_overlap.wait(.05))
        self.assertTrue(app.acquire_path_claims("third", independent, {"status": "running"}))
        app.release_path_claims("first")
        self.assertTrue(acquired_overlap.wait(1))
        app.release_path_claims("second")
        app.release_path_claims("third")
        waiter.join(timeout=1)
        scoped_a = app.scope_path_claims({"mode": "local", "path": "/tmp/a"}, [(".", "src")])
        scoped_b = app.scope_path_claims({"mode": "local", "path": "/tmp/b"}, [(".", "src")])
        self.assertFalse(app.path_claims_overlap(scoped_a[0], scoped_b[0]))

    def test_lifecycle_hooks_receive_json_and_reject_unknown_events(self):
        with tempfile.TemporaryDirectory() as directory:
            hooks = Path(directory)
            capture = hooks / "capture.json"
            script = hooks / "on_run_finished"
            script.write_text("#!/bin/sh\ntee \"$HOOK_CAPTURE\" >/dev/null\n")
            script.chmod(0o700)
            with mock.patch.dict(os.environ, {
                "TASK_OFFICE_HOOKS_DIR": str(hooks), "HOOK_CAPTURE": str(capture),
            }, clear=False):
                app.emit_lifecycle_hook("on_run_finished", {"task": "done"})
            payload = json.loads(capture.read_text())
            self.assertEqual((payload["event"], payload["task"]), ("on_run_finished", "done"))
            with self.assertRaisesRegex(ValueError, "Unsupported lifecycle hook"):
                app.emit_lifecycle_hook("on_unknown_event", {})

    def test_all_internal_claude_plugins_are_enabled_for_every_floor(self):
        root = Path(__file__).parent
        marketplace = json.loads((root / "the-office-plugins/.claude-plugin/marketplace.json").read_text())
        self.assertEqual(marketplace["name"], "the-office-plugins")
        self.assertTrue(all(plugin.get("relevance") for plugin in marketplace["plugins"]))
        bundled = app.bundled_plugin_paths()
        self.assertEqual(len(bundled), len(marketplace["plugins"]))
        self.assertEqual(app.normalized_plugin_paths({}), bundled)
        approved = app.approved_plugin_path("postgresql@the-office-plugins")
        self.assertEqual(approved["name"], "postgres-helpers")
        self.assertTrue(Path(approved["path"]).is_dir())


class FloorIntentTests(unittest.TestCase):
    def test_url_shaped_input_resolves_to_clone(self):
        resolution = app.resolve_floor_intent("https://github.com/example/repo.git")
        self.assertEqual(resolution["mode"], "clone")
        self.assertEqual(resolution["url"], "https://github.com/example/repo.git")

    def test_scp_style_url_resolves_to_clone(self):
        resolution = app.resolve_floor_intent("git@github.com:example/repo.git")
        self.assertEqual(resolution["mode"], "clone")

    def test_existing_git_directory_resolves_to_local(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            resolution = app.resolve_floor_intent(str(repository))
            self.assertEqual(resolution["mode"], "local")
            self.assertEqual(resolution["resolved_path"], str(repository.resolve()))

    def test_directory_with_nested_repos_resolves_to_cupboard(self):
        with tempfile.TemporaryDirectory() as directory:
            cupboard = Path(directory) / "cupboard"
            nested = cupboard / "nested-repo"
            nested.mkdir(parents=True)
            (nested / ".git").mkdir()
            resolution = app.resolve_floor_intent(str(cupboard))
            self.assertEqual(resolution["mode"], "cupboard")
            self.assertEqual(resolution["repositories"], [str(nested.resolve())])

    def test_existing_empty_directory_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()
            resolution = app.resolve_floor_intent(str(empty))
            self.assertEqual(resolution["mode"], "ambiguous")
            self.assertEqual(resolution["repositories"], [])

    def test_nonexistent_path_is_unresolved(self):
        resolution = app.resolve_floor_intent("/definitely/not/a/real/path/xyz")
        self.assertEqual(resolution["mode"], "unresolved")

    def test_empty_string_is_unresolved(self):
        resolution = app.resolve_floor_intent("")
        self.assertEqual(resolution["mode"], "unresolved")

    def test_resolution_maps_floors_and_preserves_passthrough_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            payload = {
                "unresolved": None,
                "floors": [{
                    "raw_path_or_url": str(repository),
                    "agent": "codex",
                    "lead_name": "Priya",
                    "floor_name": "Payments",
                }],
            }
            result = app.floor_intent_resolution(payload)
            self.assertIsNone(result["unresolved"])
            self.assertEqual(len(result["floors"]), 1)
            entry = result["floors"][0]
            self.assertEqual(entry["agent"], "codex")
            self.assertEqual(entry["lead_name"], "Priya")
            self.assertEqual(entry["floor_name"], "Payments")
            self.assertEqual(entry["resolution"]["mode"], "local")

    def test_resolution_rejects_missing_floors(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            app.floor_intent_resolution({"unresolved": None})

    def test_resolution_rejects_empty_floors(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            app.floor_intent_resolution({"floors": [], "unresolved": None})

    def test_resolution_rejects_too_many_floors(self):
        floors = [{"raw_path_or_url": "x", "agent": None, "lead_name": None, "floor_name": None}
                   for _ in range(app.MAX_FLOOR_INTENT_ITEMS + 1)]
        with self.assertRaisesRegex(ValueError, "At most"):
            app.floor_intent_resolution({"floors": floors, "unresolved": None})

    def test_floor_intent_resolve_http_api(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.OfficeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def request(path, payload):
            body = json.dumps(payload).encode()
            call = urllib.request.Request(
                base + path, data=body, method="POST", headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(call, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        try:
            payload = {
                "unresolved": None,
                "floors": [{
                    "raw_path_or_url": "https://github.com/example/repo.git",
                    "agent": "claude", "lead_name": "Sam", "floor_name": "Growth",
                }],
            }
            status, resolved = request("/api/floor-intent-resolve", payload)
            self.assertEqual(status, 200)
            self.assertEqual(resolved["floors"][0]["resolution"]["mode"], "clone")
            self.assertEqual(resolved["floors"][0]["lead_name"], "Sam")

            status, error = request("/api/floor-intent-resolve", {"unresolved": None})
            self.assertEqual(status, 400)
            self.assertIn("error", error)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class VisualPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parent / "office.html").read_text()

    def test_ambient_and_personality_features_are_wired_to_rendered_state(self):
        for marker in (
            'document.body.dataset.daypart', 'floorEl.dataset.busy', 'long-idle',
            'workerShippedCount', 'workerFlavor', 'editAvatar', 'just-hired',
        ):
            self.assertIn(marker, self.html)

    def test_building_easter_egg_and_streak_moments_are_available(self):
        for marker in ('openBuildingView()', 'pokeDecor(this)', 'streak-banner', 'celebratePublish()'):
            self.assertIn(marker, self.html)

    def test_visual_doc_ambient_personality_and_wall_details_are_available(self):
        for marker in (
            'seasonalOfficeDecor', 'data-neglected', 'plant-sway', 'officePet',
            'startOfficeRadio', 'workerQuirkMarkup', 'ambientWorkerBubble',
            'employeeOfTheMonth', 'officeWallMarkup', 'pokeWaterCooler',
        ):
            self.assertIn(marker, self.html)

    def test_visual_doc_movement_replay_and_motion_details_are_available(self):
        for marker in (
            'animatePaperTrail', 'animateFloorMessenger', 'animateReceptionParcel',
            'watchTodayHappen', 'openOrgChart', 'officeCrt', 'avatar-progress',
            'animateCountChips', 'manager-thinking', 'camera-enter-right',
        ):
            self.assertIn(marker, self.html)

    def test_phase_nine_workspace_and_editable_review_are_available(self):
        for marker in (
            'openWorkspace()', '/api/repo-tree', '/api/repo-file', 'saveWorkspaceFile',
            'loadWorkspaceGit', 'runWorkspaceCommand', 'loadWorkspaceReadme',
            'saveEditedReviewFile', 'originalEditable:false',
        ):
            self.assertIn(marker, self.html)

    def test_new_long_term_visual_extras_are_available(self):
        for marker in (
            'officeGardenMarkup', 'exportPictureDay', 'refreshLocalWeather',
            'screensaver', 'KONAMI_KEYS', "prefers-reduced-motion:reduce",
        ):
            self.assertIn(marker, self.html)

    def test_floor_decor_uses_a_reserved_non_overlapping_grid_row(self):
        self.assertIn('class="floor-utility-strip"', self.html)
        for marker in (
            '.floor-utility-strip .plant{position:relative',
            '.floor-utility-strip .water-cooler{position:relative',
            '.floor-utility-strip .paper-outbox{position:relative',
            '.floor-utility-strip .office-garden{position:relative',
        ):
            self.assertIn(marker, self.html)

    def test_major_frontend_areas_are_loaded_as_es_modules(self):
        for module in ('floor-grid.js', 'review-panel.js', 'reception.js', 'settings.js'):
            self.assertIn(f'type="module" src="/ui/{module}"', self.html)
            self.assertTrue((Path(__file__).parent / 'ui' / module).is_file())


if __name__ == "__main__":
    unittest.main()
