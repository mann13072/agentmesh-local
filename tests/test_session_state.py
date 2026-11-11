import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from agentmesh.cli import initialize
from agentmesh.coordination import execute, MeshError
from test_upgrade import legacy


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name) / "mesh"
        self.assertEqual(initialize(self.root, {"root": str(self.root)})[1], 0)
        legacy(self.root)
        self.call("project-create", project_id="p", title="Test")
        self.sid = self.call("session-register", tool="codex", label="worker", capabilities=[])["session_id"]
        self.call("membership-set", session_id=self.sid, project_id="p", role="worker", enabled=True)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, command, **data):
        return execute(self.root, command, {"op_id": str(uuid.uuid4()), **data})["data"]

    def test_worker_join_is_atomic_idempotent_and_only_grants_worker(self):
        request = {"op_id": str(uuid.uuid4()), "project_id": "p", "tool": "antigravity", "label": "worker", "capabilities": [], "role": "worker"}
        first = execute(self.root, "worker-join", dict(request))
        self.assertEqual(first, execute(self.root, "worker-join", dict(request)))
        sid = first["data"]["session_id"]
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            self.assertEqual(db.execute("SELECT role FROM memberships WHERE session_id=?", (sid,)).fetchall(), [("worker",)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND action='worker-join'", (sid,)).fetchone()[0], 1)
        finally:
            db.close()
        with self.assertRaises(MeshError):
            execute(self.root, "worker-join", {**request, "label": "changed"})
        with self.assertRaises(MeshError):
            execute(self.root, "worker-join", {**request, "op_id": str(uuid.uuid4()), "role": "leader-candidate"})
        with self.assertRaises(MeshError):
            execute(self.root, "worker-join", {**request, "op_id": str(uuid.uuid4()), "project_id": "missing"})
        with self.assertRaises(MeshError):
            self.call("leader-acquire", session_id=sid, project_id="p")

    def test_compact_reads_and_scoped_task_details(self):
        self.call("membership-set", session_id=self.sid, project_id="p", role="leader-candidate", enabled=True)
        generation = self.call("leader-acquire", session_id=self.sid, project_id="p")["generation"]
        task = self.call("task-create", session_id=self.sid, project_id="p", leader_generation=generation, title="Example", description="long detail " * 100, category="testing", priority=1, required_capabilities=[], outputs=[], acceptance=["Done"])
        compact = execute(self.root, "status", {"session_id": self.sid, "project_id": "p", "view": "compact"})["data"]
        self.assertNotIn("recent_events", compact)
        self.assertNotIn("description", compact["tasks"][0])
        self.assertEqual(compact["tasks"][0]["id"], task["task_id"])
        self.assertEqual(compact["session"]["lease_state"], "valid")
        listed = execute(self.root, "list", {"project_id": "p", "kind": "tasks", "cursor": None, "limit": 1, "view": "compact"})["data"]
        self.assertNotIn("description", listed["items"][0])
        detail = execute(self.root, "task-detail", {"project_id": "p", "task_id": task["task_id"]})["data"]
        self.assertTrue(detail["task"]["description"].startswith("long detail"))
        self.assertEqual(detail["dependencies"], [])
        with self.assertRaises(MeshError):
            execute(self.root, "task-detail", {"project_id": "wrong", "task_id": task["task_id"]})
        with self.assertRaises(MeshError):
            execute(self.root, "status", {"session_id": self.sid, "project_id": "p", "view": "other"})
        full = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]
        self.assertIn("description", full["tasks"][0])
        self.assertIn("recent_events", full)

    def test_lease_state_is_derived_without_changing_ownership(self):
        active = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]["session"]
        self.assertEqual(active["effective_status"], "available")
        self.assertEqual(active["lease_state"], "valid")
        self.assertGreater(active["lease_seconds_remaining"], 0)
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute("UPDATE sessions SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", (self.sid,))
            db.commit()
        finally:
            db.close()
        expired = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]["session"]
        self.assertEqual(expired["status"], "available")
        self.assertEqual(expired["effective_status"], "lease_expired")
        self.assertEqual(expired["lease_state"], "expired")
        self.assertEqual(expired["lease_seconds_remaining"], 0)
        with self.assertRaises(MeshError):
            self.call("heartbeat", session_id=self.sid, attempt_id=None)
        rows = execute(self.root, "list", {"project_id": "p", "kind": "sessions", "cursor": None, "limit": 50})["data"]["items"]
        self.assertEqual(rows[0]["effective_status"], "lease_expired")
        self.call("sync")
        dashboard = (self.root / "vault" / "00-Dashboard" / "Active-Agents.md").read_text(encoding="utf-8")
        self.assertIn("lease_expired", dashboard)
        self.assertIn("as of", dashboard.lower())

    def test_closed_session_does_not_appear_available(self):
        self.call("session-close", session_id=self.sid)
        closed = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]["session"]
        self.assertEqual(closed["effective_status"], "closed")
        self.assertEqual(closed["lease_state"], "closed")
        with self.assertRaises(MeshError):
            self.call("heartbeat", session_id=self.sid, attempt_id=None)

    def test_leader_with_expired_session_is_unavailable_without_reviving(self):
        self.call("membership-set", session_id=self.sid, project_id="p", role="leader-candidate", enabled=True)
        lease = self.call("leader-acquire", session_id=self.sid, project_id="p")
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute("UPDATE sessions SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", (self.sid,))
            db.commit()
        finally:
            db.close()
        status = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]
        self.assertEqual(status["leader"]["owner_session_lease_state"], "expired")
        self.assertEqual(status["leader"]["effective_status"], "owner_unavailable")
        self.assertTrue(status["leader"]["slot_free"])
        with self.assertRaises(MeshError):
            self.call("leader-renew", session_id=self.sid, project_id="p", leader_generation=lease["generation"])

    def test_invalid_lease_is_unknown_and_cannot_be_renewed(self):
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute("UPDATE sessions SET lease_until='not-a-timestamp' WHERE id=?", (self.sid,))
            db.commit()
        finally:
            db.close()
        status = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]["session"]
        self.assertEqual(status["effective_status"], "unknown")
        self.assertIsNone(status["lease_seconds_remaining"])
        with self.assertRaises(MeshError):
            self.call("heartbeat", session_id=self.sid, attempt_id=None)

    def test_busy_session_with_expired_attempt_needs_recovery(self):
        self.call("membership-set", session_id=self.sid, project_id="p", role="leader-candidate", enabled=True)
        leader = self.call("leader-acquire", session_id=self.sid, project_id="p")
        task = self.call("task-create", session_id=self.sid, project_id="p", leader_generation=leader["generation"], title="Work", description="Work", category="testing", priority=1, required_capabilities=[], outputs=[], acceptance=["Done"])
        self.call("task-publish", session_id=self.sid, project_id="p", leader_generation=leader["generation"], task_id=task["task_id"], expected_version=task["version"])
        attempt = self.call("task-claim", session_id=self.sid, project_id="p", task_id=task["task_id"])
        current = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]["session"]
        self.assertEqual(current["effective_status"], "busy")
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", (attempt["attempt_id"],))
            db.commit()
        finally:
            db.close()
        data = execute(self.root, "status", {"session_id": self.sid, "project_id": "p"})["data"]
        self.assertEqual(data["session"]["lease_state"], "valid")
        self.assertEqual(data["session"]["effective_status"], "attempt_lease_expired")
        self.assertEqual(data["tasks"][0]["state"], "claimed")
        with self.assertRaises(MeshError):
            self.call("task-start", session_id=self.sid, attempt_id=attempt["attempt_id"], generation=attempt["generation"])


if __name__ == "__main__":
    unittest.main()
