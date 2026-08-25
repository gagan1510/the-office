import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path

import office_server as app


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_directory = app.DATA_DIRECTORY
        self.original_database_path = app.DATABASE_PATH
        app.DATA_DIRECTORY = Path(self.temporary_directory.name)
        app.DATABASE_PATH = app.DATA_DIRECTORY / "office.db"
        app.initialize_database()

    def tearDown(self):
        if app.database is not None:
            app.database.close()
            app.database = None
        app.DATA_DIRECTORY = self.original_data_directory
        app.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

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
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
