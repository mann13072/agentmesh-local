import tempfile
import json
import sqlite3
import subprocess
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest
import uuid
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentmesh.cli import initialize
from agentmesh.coordination import execute, MeshError
from test_upgrade import legacy


class CoordinationTests(unittest.TestCase):
    def test_online_backup_and_idempotent_retry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            self.assertEqual(initialize(root, {"root": str(root)})[1], 0)
            legacy(root)
            execute(root, "project-create", {"op_id": str(uuid.uuid4()), "project_id": "backup", "title": "Backup"})
            request = {"op_id": str(uuid.uuid4())}
            result = execute(root, "backup", request)
            self.assertEqual(result, execute(root, "backup", request))
            conn = sqlite3.connect(result["data"]["path"])
            try:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT title FROM projects WHERE id='backup'").fetchone()[0], "Backup")
            finally:
                conn.close()
            restored_path = root / "runtime" / "restored-test.db"
            shutil.copyfile(result["data"]["path"], restored_path)
            restored = sqlite3.connect(restored_path)
            try:
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(restored.execute("SELECT COUNT(*) FROM projects WHERE id='backup'").fetchone()[0], 1)
            finally:
                restored.close()

    def test_expired_attempt_and_leader_are_fenced(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            self.assertEqual(initialize(root, {"root": str(root)})[1], 0)
            legacy(root)

            def call(command, **kwargs):
                return execute(root, command, {"op_id": str(uuid.uuid4()), **kwargs})["data"]

            call("project-create", project_id="fence", title="Fencing")
            lead = call("session-register", tool="codex", label="leader", capabilities=[])["session_id"]
            worker = call("session-register", tool="codex", label="worker", capabilities=[])["session_id"]
            call("membership-set", session_id=lead, project_id="fence", role="leader-candidate", enabled=True)
            call("membership-set", session_id=worker, project_id="fence", role="worker", enabled=True)
            gen = call("leader-acquire", session_id=lead, project_id="fence")["generation"]
            leader = {"session_id": lead, "project_id": "fence", "leader_generation": gen}
            task = call("task-create", **leader, title="Fence", description="Fence", category="test", priority=1, required_capabilities=[], outputs=[], acceptance=["Done"])
            call("task-publish", **leader, task_id=task["task_id"], expected_version=task["version"])
            claim = call("task-claim", session_id=worker, project_id="fence", task_id=task["task_id"])
            beat = {"session_id": worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
            with patch("agentmesh.coordination.now", return_value="9999-12-31T00:00:00.000000Z"):
                # 0.2: an expired holder keeps its attempt until recover, so its heartbeat still works...
                call("heartbeat", **beat)
                # Leadership has no deadline: far in the future the leader still leads (fencing is by generation).
                self.assertEqual(call("leader-renew", **leader)["generation"], gen)
            db = sqlite3.connect(root / "runtime" / "coordination.db")
            try:
                db.execute("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", (claim["attempt_id"],))
                db.commit()
            finally:
                db.close()
            call("recover", **leader, task_id=task["task_id"], reason="expired")
            # ...and after recover the old holder is fenced.
            with self.assertRaises(MeshError):
                call("heartbeat", **beat)

    def test_simultaneous_claims_and_idempotency(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            self.assertEqual(initialize(root, {"root": str(root)})[1], 0)
            legacy(root)

            def call(command, **kwargs):
                return execute(root, command, {"op_id": str(uuid.uuid4()), **kwargs})["data"]

            call("project-create", project_id="race", title="Race")
            leader = call("session-register", tool="codex", label="leader", capabilities=[])["session_id"]
            workers = [call("session-register", tool="codex", label="worker", capabilities=[])["session_id"] for _ in range(2)]
            call("membership-set", session_id=leader, project_id="race", role="leader-candidate", enabled=True)
            for worker in workers:
                call("membership-set", session_id=worker, project_id="race", role="worker", enabled=True)
            generation = call("leader-acquire", session_id=leader, project_id="race")["generation"]
            lead = {"session_id": leader, "project_id": "race", "leader_generation": generation}
            task = call("task-create", **lead, title="Race", description="Race", category="test", priority=1, required_capabilities=[], outputs=[], acceptance=["Done"])
            call("task-publish", **lead, task_id=task["task_id"], expected_version=task["version"])

            def claim(worker):
                data = {"op_id": str(uuid.uuid4()), "session_id": worker, "project_id": "race", "task_id": task["task_id"]}
                env = dict(os.environ)
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
                result = subprocess.run([sys.executable, "-m", "agentmesh.cli", "--root", str(root), "task-claim", "--input", "-"], input=json.dumps(data), text=True, encoding="utf-8", capture_output=True, env=env, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return result.returncode

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim, workers))
            self.assertCountEqual(results, [0, 3])
            op = {"op_id": str(uuid.uuid4()), "tool": "codex", "label": "same", "capabilities": []}
            self.assertEqual(execute(root, "session-register", dict(op)), execute(root, "session-register", dict(op)))
            with self.assertRaises(MeshError):
                execute(root, "session-register", {**op, "label": "changed"})

    def test_worker_flow_and_independent_review(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            self.assertEqual(initialize(root, {"root": str(root)})[1], 0)
            legacy(root)

            def call(command, **kwargs):
                return execute(root, command, {"op_id": str(uuid.uuid4()), **kwargs})["data"]

            project = "example"
            call("project-create", project_id=project, title="Example")
            lead = call("session-register", tool="codex", label="Leader", capabilities=[])["session_id"]
            worker = call("session-register", tool="codex", label="Worker", capabilities=["python"])["session_id"]
            call("membership-set", session_id=lead, project_id=project, role="leader-candidate", enabled=True)
            call("membership-set", session_id=worker, project_id=project, role="worker", enabled=True)
            gen = call("leader-acquire", session_id=lead, project_id=project)["generation"]
            leader = {"session_id": lead, "project_id": project, "leader_generation": gen}
            inbox = root / "vault" / "03-Inbox" / "New-Tasks" / "request.md"
            request_id = str(uuid.uuid4())
            inbox.write_text(f"---\nrequest_id: {request_id}\nproject_id: example\ntitle: Research\ncategory: research\npriority: 5\n---\nExplore options.\n", encoding="utf-8")
            imported = call("inbox-import", **leader, path=str(inbox))
            self.assertEqual(imported, call("inbox-import", **leader, path=str(inbox)))
            inbox.write_text(inbox.read_text(encoding="utf-8") + "Changed.\n", encoding="utf-8")
            with self.assertRaises(MeshError):
                call("inbox-import", **leader, path=str(inbox))
            task = call("task-create", **leader, title="Test", description="Task", category="testing", priority=2, required_capabilities=["python"], outputs=[], acceptance=["Pass"])
            call("task-publish", **leader, task_id=task["task_id"], expected_version=task["version"])
            claim = call("task-claim", session_id=worker, project_id=project, task_id=task["task_id"])
            ownership = {"session_id": worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
            with self.assertRaises(MeshError):
                call("task-claim", session_id=worker, project_id=project, task_id=task["task_id"])
            call("task-start", **ownership)
            call("task-submit", **ownership, manifest={"paths": [], "summary": "Done", "evidence": "Checked"})
            with self.assertRaises(MeshError):
                call("review", session_id=worker, attempt_id=claim["attempt_id"], outcome="accepted", evidence="Checked")
            reviewed = call("review", session_id=lead, attempt_id=claim["attempt_id"], outcome="accepted", evidence="Reviewed", leader_generation=gen)
            self.assertEqual(reviewed["task_state"], "completed")
            request = {"op_id": str(uuid.uuid4())}
            first = execute(root, "sync", request)
            second = execute(root, "sync", request)
            self.assertEqual(first, second)
            lines = (root / "runtime" / "shared-activity.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), len({json.loads(line)["seq"] for line in lines}))
            task_note = root / "vault" / "01-Projects" / project / "Tasks" / (task["task_id"] + ".md")
            self.assertTrue(task_note.read_text(encoding="utf-8").startswith("---\nschema: 1"))


if __name__ == "__main__":
    unittest.main()
