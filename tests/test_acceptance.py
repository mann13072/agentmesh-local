import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

os.environ["AGENTMESH_NO_POPUP"] = "1"  # never show a real approval popup during tests
os.environ["AGENTMESH_NO_WAKE"] = "1"  # never run a real codex queue during tests
os.environ["AGENTMESH_CHAT_HOME"] = str(Path(__file__).resolve().parent / "no-chat-home")  # never read real Codex/Antigravity chats
for _name in ("CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"):
    os.environ.pop(_name, None)

from agentmesh.cli import initialize
from agentmesh.coordination import execute, MeshError
from test_upgrade import legacy


def worktree(claim):
    """The CLI only names the worktree; the worker creates it (here: a plain folder stands in for git worktree add)."""
    path = Path(claim["worktree"])
    path.mkdir(parents=True, exist_ok=True)
    return path


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name) / "mesh"
        self.assertEqual(initialize(self.root, {"root": str(self.root)})[1], 0)
        legacy(self.root)
        self.call("project-create", project_id="p", title="Project")
        self.leader = self.call("session-register", tool="codex", label="Leader", capabilities=[])["session_id"]
        self.first = self.call("session-register", tool="codex", label="First", capabilities=["python"])["session_id"]
        self.second = self.call("session-register", tool="codex", label="Second", capabilities=["python"])["session_id"]
        for sid, role in ((self.leader, "leader-candidate"), (self.first, "worker"), (self.second, "worker")):
            self.call("membership-set", session_id=sid, project_id="p", role=role, enabled=True)
        gen = self.call("leader-acquire", session_id=self.leader, project_id="p")["generation"]
        self.lead = {"session_id": self.leader, "project_id": "p", "leader_generation": gen}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, command, **values):
        return execute(self.root, command, {"op_id": str(uuid.uuid4()), **values})["data"]

    def task(self, name, capabilities=None):
        return self.call("task-create", **self.lead, title=name, description=name, category="test", priority=1, required_capabilities=capabilities or [], outputs=[], acceptance=["Done"])

    def publish(self, task):
        self.call("task-publish", **self.lead, task_id=task["task_id"], expected_version=task["version"])

    def test_dependencies_reservations_and_project_pause(self):
        first, second = self.task("First"), self.task("Second", ["python"])
        self.call("task-depend", **self.lead, task_id=second["task_id"], depends_on_id=first["task_id"])
        second["version"] += 1
        with self.assertRaises(MeshError):
            self.call("task-depend", **self.lead, task_id=first["task_id"], depends_on_id=second["task_id"])
        self.publish(first)
        self.publish(second)
        with self.assertRaises(MeshError):
            self.call("task-claim", session_id=self.second, project_id="p", task_id=second["task_id"])
        self.call("task-reserve", **self.lead, task_id=first["task_id"], worker_session_id=self.first)
        with self.assertRaises(MeshError):
            self.call("task-claim", session_id=self.second, project_id="p", task_id=first["task_id"])
        self.call("project-status", **self.lead, status="paused", reason="Testing")
        with self.assertRaises(MeshError):
            self.call("task-claim", session_id=self.first, project_id="p", task_id=first["task_id"])
        self.call("project-status", **self.lead, status="active", reason="Resume")
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=first["task_id"])
        attempt = {"session_id": self.first, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        self.call("task-submit", **attempt, manifest={"paths": [], "summary": "Done", "evidence": "Test"})
        self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="accepted", evidence="Reviewed", leader_generation=self.lead["leader_generation"])
        # 0.3: the integration gate is enforced: accepted is not enough, the prerequisite must be integrated.
        with self.assertRaises(MeshError) as gate:
            self.call("task-claim", session_id=self.second, project_id="p", task_id=second["task_id"])
        self.assertEqual(gate.exception.details["blocker"], "dependency_not_integrated")
        self.call("task-integrate", **self.lead, task_id=first["task_id"], destination=str(self.root), evidence="merged and tested")
        self.call("task-claim", session_id=self.second, project_id="p", task_id=second["task_id"])

    def test_recover_preserves_checkpoint_and_fences_old_worker(self):
        task = self.task("Recover")
        self.publish(task)
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"])
        attempt = {"session_id": self.first, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        output = worktree(claim) / "partial.md"
        output.write_text("Keep me", encoding="utf-8")
        self.call("checkpoint", **attempt, summary="Partial", paths=[str(output)])
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute("UPDATE attempts SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00.000000Z", claim["attempt_id"]))
            db.commit()
        finally:
            db.close()
        recovered = self.call("recover", **self.lead, task_id=task["task_id"], reason="Lease expired")
        self.assertEqual(recovered["abandoned_attempt_id"], claim["attempt_id"])
        replacement = self.call("task-claim", session_id=self.second, project_id="p", task_id=task["task_id"])
        self.assertGreater(replacement["generation"], claim["generation"])
        with self.assertRaises(MeshError):
            self.call("task-submit", **attempt, manifest={"paths": [str(output)], "summary": "Old", "evidence": "Old"})
        self.assertEqual(output.read_text(encoding="utf-8"), "Keep me")
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            payload = json.loads(db.execute("SELECT payload_json FROM events WHERE action='recover'").fetchone()[0])
            self.assertEqual(payload["preserved_checkpoint"]["paths"], [str(output)])
        finally:
            db.close()

    def test_projection_crash_retry_and_input_rejections(self):
        task = self.task("Projection")
        self.publish(task)
        request = {"op_id": str(uuid.uuid4())}
        with patch("agentmesh.projections.replace", side_effect=OSError("simulated interrupted write")):
            with self.assertRaises(OSError):
                execute(self.root, "sync", request)
        execute(self.root, "sync", request)
        log = self.root / "runtime" / "shared-activity.jsonl"
        rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), len({row["seq"] for row in rows}))
        note = self.root / "vault" / "01-Projects" / "p" / "Tasks" / (task["task_id"] + ".md")
        note.write_text("manual change", encoding="utf-8")
        execute(self.root, "sync", {"op_id": str(uuid.uuid4())})
        self.assertIn("status: ready", note.read_text(encoding="utf-8"))
        with self.assertRaises(MeshError):
            self.call("task-claim", session_id=self.leader, project_id="p", task_id=task["task_id"])
        with self.assertRaises(MeshError):
            self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"], unexpected=True)
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"])
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaises(MeshError):
            self.call("checkpoint", session_id=self.first, attempt_id=claim["attempt_id"], generation=claim["generation"], summary="Invalid", paths=[str(outside)])

    def test_database_lock_fails_closed(self):
        settings_file = self.root / "config" / "settings.json"
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        settings["busy_timeout_ms"] = 30
        settings_file.write_text(json.dumps(settings), encoding="utf-8")
        db = sqlite3.connect(self.root / "runtime" / "coordination.db", isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                self.call("project-status", **self.lead, status="paused", reason="Lock test")
            db.execute("ROLLBACK")
            self.assertEqual(db.execute("SELECT status FROM projects WHERE id='p'").fetchone()[0], "active")
        finally:
            db.close()

    def test_reservation_expiry_capabilities_and_leader_failover(self):
        task = self.task("Reserved", ["python"])
        self.publish(task)
        self.call("task-reserve", **self.lead, task_id=task["task_id"], worker_session_id=self.first)
        unqualified = self.call("session-register", tool="opencode", label="Unqualified", capabilities=[])["session_id"]
        self.call("membership-set", session_id=unqualified, project_id="p", role="worker", enabled=True)
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute("UPDATE tasks SET reservation_until=? WHERE id=?", ("2000-01-01T00:00:00.000000Z", task["task_id"]))
            db.execute("UPDATE leader_leases SET lease_until=? WHERE project_id='p'", ("2000-01-01T00:00:00.000000Z",))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(MeshError):
            self.call("task-claim", session_id=unqualified, project_id="p", task_id=task["task_id"])
        claim = self.call("task-claim", session_id=self.second, project_id="p", task_id=task["task_id"])
        self.call("membership-set", session_id=self.first, project_id="p", role="leader-candidate", enabled=True)
        generation = self.call("leader-acquire", session_id=self.first, project_id="p")["generation"]
        self.assertGreater(generation, self.lead["leader_generation"])
        with self.assertRaises(MeshError):
            self.call("project-status", **self.lead, status="paused", reason="Stale leader")
        attempt = {"session_id": self.second, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        self.call("task-submit", **attempt, manifest={"paths": [], "summary": "Done", "evidence": "Done"})
        reviewed = self.call("review", session_id=self.first, attempt_id=claim["attempt_id"], outcome="accepted", evidence="Reviewed", leader_generation=generation)
        self.assertEqual(reviewed["task_state"], "completed")

    def test_symlink_escape_is_rejected_when_supported(self):
        task = self.task("Paths")
        self.publish(task)
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"])
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        link = worktree(claim) / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Windows symlink creation unavailable: {exc}")
        with self.assertRaises(MeshError):
            self.call("checkpoint", session_id=self.first, attempt_id=claim["attempt_id"], generation=claim["generation"], summary="Escape", paths=[str(link / "secret.txt")])

    def test_legacy_workspace_path_is_rejected(self):
        task = self.task("No workspace")
        self.publish(task)
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"])
        self.assertNotIn("workspace", claim)
        self.assertEqual(claim["branch"], "agentmesh/" + task["task_id"] + "-" + claim["attempt_id"])
        self.assertFalse(Path(claim["worktree"]).exists())
        legacy = self.root / "workspaces" / "p" / "attempts" / claim["attempt_id"]
        legacy.mkdir(parents=True)
        (legacy / "out.md").write_text("old place", encoding="utf-8")
        with self.assertRaises(MeshError):
            self.call("checkpoint", session_id=self.first, attempt_id=claim["attempt_id"], generation=claim["generation"], summary="Old place", paths=[str(legacy / "out.md")])

    def test_review_revisions_recommendations_and_pagination(self):
        task = self.task("Review")
        task = self.call("task-spec", **self.lead, task_id=task["task_id"], expected_version=task["version"], title="Review updated", description="Scope", category="test", priority=1, required_capabilities=[], outputs=[], acceptance=["Done"])
        self.publish(task)
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"])
        attempt = {"session_id": self.first, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        self.call("task-submit", **attempt, manifest={"paths": [], "summary": "Done", "evidence": "Evidence"})
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute("UPDATE attempts SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00.000000Z", claim["attempt_id"]))
            db.commit()
        finally:
            db.close()
        result = self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="blocked", evidence="Need detail", leader_generation=self.lead["leader_generation"])
        self.assertEqual(result["task_state"], "blocked")
        self.call("task-unblock", **self.lead, task_id=task["task_id"], reason="Details available")
        result = self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="revision", evidence="Revise", leader_generation=self.lead["leader_generation"])
        self.assertEqual(result["task_state"], "ready")
        new_claim = self.call("task-claim", session_id=self.second, project_id="p", task_id=task["task_id"])
        self.assertGreater(new_claim["generation"], claim["generation"])
        rec = self.call("recommend", session_id=self.first, project_id="p", task_id=task["task_id"], dedupe_key="Suggestion", body="Improve the design")
        duplicate = self.call("recommend", session_id=self.second, project_id="p", task_id=task["task_id"], dedupe_key="suggestion", body="Same idea")
        self.assertTrue(duplicate["existing"])
        self.assertEqual(rec["recommendation_id"], duplicate["recommendation_id"])
        self.call("recommend-decide", **self.lead, recommendation_id=rec["recommendation_id"], status="accepted", reason="Useful")
        first_page = execute(self.root, "list", {"project_id": "p", "kind": "reviews", "cursor": None, "limit": 1})["data"]
        self.assertIsNotNone(first_page["next_cursor"])
        second_page = execute(self.root, "list", {"project_id": "p", "kind": "reviews", "cursor": first_page["next_cursor"], "limit": 1})["data"]
        self.assertNotEqual(first_page["items"][0]["id"], second_page["items"][0]["id"])
        self.call("sync")
        self.assertTrue((self.root / "vault" / "01-Projects" / "p" / "Recommendations" / (rec["recommendation_id"] + ".md")).is_file())

    # ---- Stage 1 interface: help, blockers, recovery references, integration records ----

    def expire(self, sql, *args):
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            db.execute(sql, ("2000-01-01T00:00:00.000000Z", *args))
            db.commit()
        finally:
            db.close()

    def run_main(self, argv):
        import io
        from contextlib import redirect_stdout
        from agentmesh.cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            status = main(argv)
        return status, out.getvalue()

    def test_help_needs_no_root_and_examples_match_fields(self):
        from agentmesh.cli import COMMANDS, OPTIONAL
        for argv in ([], ["--help"], ["-h"], ["help"], ["help", "task-claim"], ["task-claim", "--help"]):
            status, text = self.run_main(argv)
            self.assertEqual(status, 0, argv)
            self.assertIn("agentmesh", text)
        self.assertEqual(self.run_main(["help", "no-such-command"])[0], 2)
        self.assertIn("Required: op_id, session_id, project_id, task_id", self.run_main(["help", "task-claim"])[1])
        from agentmesh.cli import FLOWS
        for flow, names in FLOWS.items():
            status, text = self.run_main(["help", flow])
            self.assertEqual(status, 0, flow)
            for name in names:
                self.assertIn(f"agentmesh {name}\n", text, f"{flow} flow is missing {name}")
        status, text = self.run_main(["--root", str(self.root), "status"])
        self.assertEqual(status, 2)
        self.assertIn("next_action", json.loads(text)["error"])

        def concrete(value):
            if value == "<uuid4>":
                return str(uuid.uuid4())
            if value == "<project-id>":
                return "p"
            if isinstance(value, str) and "|" in value:
                return value.split("|")[0]
            if isinstance(value, dict):
                return {k: concrete(v) for k, v in value.items()}
            return value

        for name, (_, example, _) in COMMANDS.items():
            if name in ("init", "project-init"):
                continue
            variants = [example]
            if name != "heartbeat" and OPTIONAL.get(name):
                variants.append({k: v for k, v in example.items() if k not in OPTIONAL[name]})
            for variant in variants:
                try:
                    execute(self.root, name, {k: concrete(v) for k, v in variant.items()})
                except MeshError as exc:
                    self.assertNotEqual(str(exc), "incorrect or unknown input fields", f"{name}: {exc.details}")
                except sqlite3.IntegrityError:
                    pass

    def test_utf8_bom_input_is_accepted(self):
        request = self.root / "status.json"
        request.write_bytes(b"\xef\xbb\xbf" + json.dumps({"session_id": self.first, "project_id": "p", "view": "compact"}).encode("utf-8"))
        status, text = self.run_main(["--root", str(self.root), "status", "--input", str(request)])
        self.assertEqual(status, 0, text)
        self.assertTrue(json.loads(text)["ok"])

    def eligibility(self, sid, task_id):
        return execute(self.root, "task-detail", {"project_id": "p", "task_id": task_id, "session_id": sid})["data"]["eligibility"]

    def assert_blocked(self, sid, task_id, code):
        """The advisory view and the authoritative claim must agree."""
        view = self.eligibility(sid, task_id)
        self.assertFalse(view["claimable"])
        codes = [b["code"] for b in view["blockers"]]
        self.assertIn(code, codes)
        with self.assertRaises(MeshError) as caught:
            self.call("task-claim", session_id=sid, project_id="p", task_id=task_id)
        self.assertEqual(caught.exception.code, "CONFLICT")
        self.assertEqual(caught.exception.details["blocker"], codes[0])
        self.assertTrue(caught.exception.details["next_action"])
        compact = execute(self.root, "status", {"session_id": sid, "project_id": "p", "view": "compact"})["data"]
        row = next(x for x in compact["tasks"] if x["id"] == task_id)
        self.assertFalse(row["claimable"])
        self.assertEqual(sorted(row["blockers"] + [b["code"] for b in compact["session_blockers"]]), sorted(codes))

    def test_eligibility_blockers_match_claim(self):
        base, needs_python, dependent, hidden = self.task("Base"), self.task("Python", ["python"]), self.task("Dependent"), self.task("Proposed")
        self.call("task-depend", **self.lead, task_id=dependent["task_id"], depends_on_id=base["task_id"])
        dependent["version"] += 1
        for item in (base, needs_python, dependent):
            self.publish(item)
        plain = self.call("worker-join", project_id="p", tool="other", label="Plain", capabilities=[], role="worker")["session_id"]
        self.assert_blocked(self.first, dependent["task_id"], "dependency_incomplete")
        self.assert_blocked(self.first, hidden["task_id"], "not_ready")
        self.assert_blocked(plain, needs_python["task_id"], "capability_mismatch")
        self.assert_blocked(self.leader, base["task_id"], "not_worker")
        self.call("task-reserve", **self.lead, task_id=base["task_id"], worker_session_id=self.first)
        self.assert_blocked(self.second, base["task_id"], "reserved_elsewhere")
        self.assertTrue(self.eligibility(self.first, base["task_id"])["claimable"])
        self.call("project-status", **self.lead, status="paused", reason="Testing")
        self.assert_blocked(self.first, base["task_id"], "project_paused")
        self.call("project-status", **self.lead, status="active", reason="Resume")
        status = execute(self.root, "status", {"session_id": self.first, "project_id": "p", "view": "compact"})["data"]
        self.assertTrue(next(x for x in status["tasks"] if x["id"] == base["task_id"])["claimable"])
        first_claimable = next(x for x in status["tasks"] if x["claimable"])
        self.assertIn(first_claimable["id"], status["next_action"])
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=base["task_id"])
        self.assert_blocked(self.first, needs_python["task_id"], "has_active_attempt")
        status = execute(self.root, "status", {"session_id": self.first, "project_id": "p", "view": "compact"})["data"]
        self.assertEqual(status["my_active_attempt"]["attempt_id"], claim["attempt_id"])
        self.assertEqual(status["my_active_attempt"]["worktree"], claim["worktree"])
        self.expire("UPDATE sessions SET lease_until=? WHERE id=?", plain)
        self.assert_blocked(plain, needs_python["task_id"], "session_expired")
        status = execute(self.root, "status", {"session_id": plain, "project_id": "p", "view": "compact"})["data"]
        self.assertIn("fresh session", status["next_action"])

    def test_task_detail_returns_revision_and_recovery_references(self):
        task = self.task("Revise")
        self.publish(task)
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"])
        attempt = {"session_id": self.first, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        output = worktree(claim) / "draft.md"
        output.write_text("Draft", encoding="utf-8")
        self.call("checkpoint", **attempt, summary="Draft written", paths=[str(output)])
        self.call("task-submit", **attempt, manifest={"paths": [str(output)], "summary": "Draft", "evidence": "Read it"})
        self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="revision", evidence="Add a conclusion", leader_generation=self.lead["leader_generation"])
        detail = execute(self.root, "task-detail", {"project_id": "p", "task_id": task["task_id"], "session_id": self.first})["data"]
        self.assertEqual(detail["latest_review"]["outcome"], "revision")
        self.assertEqual(detail["latest_review"]["evidence"], "Add a conclusion")
        self.assertEqual(detail["attempts"][0]["state"], "revision")
        self.assertEqual(detail["attempts"][0]["manifest"]["paths"], [str(output)])
        self.assertEqual(detail["attempts"][0]["worktree"], claim["worktree"])
        self.assertTrue(detail["eligibility"]["claimable"])
        compact = execute(self.root, "status", {"session_id": self.first, "project_id": "p", "view": "compact"})["data"]
        self.assertTrue(next(x for x in compact["tasks"] if x["id"] == task["task_id"])["revision_requested"])
        second = self.call("task-claim", session_id=self.first, project_id="p", task_id=task["task_id"])
        retry = {"session_id": self.first, "attempt_id": second["attempt_id"], "generation": second["generation"]}
        self.call("task-start", **retry)
        kept = worktree(second) / "draft.md"
        kept.write_text("Draft plus conclusion", encoding="utf-8")
        self.call("checkpoint", **retry, summary="Conclusion added", paths=[str(kept)])
        self.expire("UPDATE attempts SET lease_until=? WHERE id=?", second["attempt_id"])
        self.call("recover", **self.lead, task_id=task["task_id"], reason="Lease expired")
        detail = execute(self.root, "task-detail", {"project_id": "p", "task_id": task["task_id"]})["data"]
        self.assertNotIn("eligibility", detail)
        self.assertEqual(detail["attempts_total"], 2)
        self.assertEqual([a["state"] for a in detail["attempts"]], ["abandoned", "revision"])
        self.assertEqual(detail["attempts"][0]["checkpoint"]["paths"], [str(kept)])
        self.assertNotIn("checkpoint", detail["attempts"][1])

    def test_task_integrate_records_verified_destination(self):
        first, second = self.task("Stage 1"), self.task("Stage 2")
        self.call("task-depend", **self.lead, task_id=second["task_id"], depends_on_id=first["task_id"])
        self.publish(first)
        claim = self.call("task-claim", session_id=self.first, project_id="p", task_id=first["task_id"])
        attempt = {"session_id": self.first, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        self.call("task-submit", **attempt, manifest={"paths": [], "summary": "Done", "evidence": "Tests"})
        destination = self.root / "app"
        destination.mkdir()
        record = {"task_id": first["task_id"], "destination": str(destination), "evidence": "npm test passed in app"}
        with self.assertRaises(MeshError):
            self.call("task-integrate", **self.lead, **record)
        self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="accepted", evidence="Verified", leader_generation=self.lead["leader_generation"])
        with self.assertRaises(MeshError):
            self.call("task-integrate", session_id=self.first, project_id="p", **record)
        with self.assertRaises(MeshError):
            self.call("task-integrate", **self.lead, **{**record, "destination": str(self.root / "missing")})
        self.call("activity-log", session_id=self.first, project_id="p", task_id=first["task_id"], action="task-integrate", result="success")
        detail = execute(self.root, "task-detail", {"project_id": "p", "task_id": second["task_id"]})["data"]
        self.assertFalse(detail["dependencies"][0]["integrated"])
        done = self.call("task-integrate", **self.lead, **record)
        self.assertEqual(done["attempt_id"], claim["attempt_id"])
        detail = execute(self.root, "task-detail", {"project_id": "p", "task_id": second["task_id"]})["data"]
        self.assertTrue(detail["dependencies"][0]["integrated"])
        own = execute(self.root, "task-detail", {"project_id": "p", "task_id": first["task_id"]})["data"]
        self.assertEqual(own["integration"]["destination"], str(destination))
        self.assertEqual(own["integration"]["attempt_id"], claim["attempt_id"])
        compact = execute(self.root, "status", {"session_id": self.leader, "project_id": "p", "view": "compact", "include_done": True})["data"]
        self.assertTrue(next(x for x in compact["tasks"] if x["id"] == first["task_id"])["integrated"])
        default = execute(self.root, "status", {"session_id": self.leader, "project_id": "p", "view": "compact"})["data"]
        self.assertNotIn(first["task_id"], [x["id"] for x in default["tasks"]])
        self.assertIn("re-arm", compact["next_action"])
        integrator = self.call("session-register", tool="other", label="Integrator", capabilities=[])["session_id"]
        self.call("membership-set", session_id=integrator, project_id="p", role="integrator", enabled=True)
        self.call("task-integrate", session_id=integrator, project_id="p", **record)

    # ---- Stage 2 (feedback anoma-model, 2026-11-07) ----

    def compact(self, sid):
        return execute(self.root, "status", {"session_id": sid, "project_id": "p", "view": "compact"})["data"]

    def started(self, task, sid=None):
        sid = sid or self.first
        claim = self.call("task-claim", session_id=sid, project_id="p", task_id=task["task_id"])
        attempt = {"session_id": sid, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        return attempt

    def test_leader_holds_role_for_whole_session_without_renewal(self):
        from agentmesh.coordination import HELD
        session = self.compact(self.leader)["session"]
        self.assertEqual(session["lease_until"], HELD)
        self.assertIsNone(session["lease_seconds_remaining"])
        # A session-only heartbeat must not shorten the leader's held session.
        self.assertEqual(self.call("heartbeat", session_id=self.leader, attempt_id=None)["session_lease_until"], HELD)
        renewed = self.call("leader-renew", **self.lead)
        self.assertEqual(renewed["lease_until"], HELD)
        self.assertIn("not needed", renewed["note"])
        # Acquiring again in the same session keeps the generation.
        again = self.call("leader-acquire", session_id=self.leader, project_id="p")
        self.assertEqual(again["generation"], self.lead["leader_generation"])
        self.assertTrue(again["already_leader"])
        released = self.call("leader-release", **self.lead)
        self.assertNotEqual(released["session_lease_until"], HELD)
        status = self.compact(self.leader)
        self.assertEqual(status["leader"]["effective_status"], "released")
        self.assertGreater(status["session"]["lease_seconds_remaining"], 800)

    def test_takeover_needs_flag_and_reason_while_leader_host_is_alive(self):
        candidate = self.call("session-register", tool="claude-code", label="Next", capabilities=[])["session_id"]
        self.call("membership-set", session_id=candidate, project_id="p", role="leader-candidate", enabled=True)
        with patch("agentmesh.coordination.host_state", return_value="alive"):
            with self.assertRaises(MeshError) as held:
                self.call("leader-acquire", session_id=candidate, project_id="p")
            self.assertEqual(held.exception.details["owner_host_state"], "alive")
            self.assertIn("takeover", held.exception.details["next_action"])
            with self.assertRaises(MeshError):
                self.call("leader-acquire", session_id=candidate, project_id="p", takeover=True)
            with self.assertRaises(MeshError):
                self.call("leader-acquire", session_id=candidate, project_id="p", takeover="yes", reason="x")
            taken = self.call("leader-acquire", session_id=candidate, project_id="p", takeover=True, reason="User said: you are the leader now")
        self.assertEqual(taken["generation"], self.lead["leader_generation"] + 1)
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            payload = json.loads(db.execute("SELECT payload_json FROM events WHERE action='leader-acquire' ORDER BY seq DESC LIMIT 1").fetchone()[0])
            old_lease = db.execute("SELECT lease_until FROM sessions WHERE id=?", (self.leader,)).fetchone()[0]
        finally:
            db.close()
        self.assertIn("user-directed takeover", payload["reason"])
        self.assertTrue(payload["takeover"])
        from agentmesh.coordination import HELD
        self.assertNotEqual(old_lease, HELD)
        # The replaced leader is fenced and told to stop, not to re-acquire.
        with self.assertRaises(MeshError) as fenced:
            self.call("task-create", **self.lead, title="Old", description="Old", category="t", priority=1, required_capabilities=[], outputs=[], acceptance=["x"])
        self.assertIn("Stop all leader work", fenced.exception.details["next_action"])

    def test_leader_slot_is_free_when_leader_host_process_is_gone(self):
        candidate = self.call("session-register", tool="claude-code", label="Next", capabilities=[])["session_id"]
        self.call("membership-set", session_id=candidate, project_id="p", role="leader-candidate", enabled=True)
        with patch("agentmesh.coordination.host_state", return_value="gone"):
            status = self.compact(candidate)
            self.assertEqual(status["leader"]["effective_status"], "gone")
            self.assertTrue(status["leader"]["slot_free"])
            self.assertIn("leader slot is free", status["next_action"])
            taken = self.call("leader-acquire", session_id=candidate, project_id="p")
        self.assertEqual(taken["generation"], self.lead["leader_generation"] + 1)

    def test_resumed_leader_keeps_its_generation(self):
        # The old host process ended (status shows gone), but the same conversation comes back with its IDs.
        with patch("agentmesh.coordination.host_state", return_value="gone"):
            self.assertEqual(self.compact(self.leader)["leader"]["effective_status"], "gone")
            again = self.call("leader-acquire", session_id=self.leader, project_id="p")
        self.assertEqual(again["generation"], self.lead["leader_generation"])
        self.assertTrue(again["already_leader"])
        self.assertEqual(self.call("leader-renew", **self.lead)["generation"], self.lead["leader_generation"])
        self.call("project-status", **self.lead, status="active", reason="back after resume")

    def test_leader_handoff_reaches_the_next_leader_only(self):
        log = {"project_id": "p", "task_id": None, "action": "leader-handoff"}
        self.call("activity-log", session_id=self.leader, **log, result="Plan: stage 1 then 2. Next: review T1.")
        self.call("activity-log", session_id=self.leader, **log, result="Plan: stage 2 left. Next: publish T3. Open: ask user about API.")
        # Only the current leader may write it.
        with self.assertRaises(MeshError):
            self.call("activity-log", session_id=self.first, **log, result="not the leader")
        self.assertNotIn("last_handoff", self.compact(self.first)["leader"])
        self.assertIn("publish T3", self.compact(self.leader)["leader"]["last_handoff"]["text"])
        candidate = self.call("session-register", tool="claude-code", label="Next", capabilities=[])["session_id"]
        self.call("membership-set", session_id=candidate, project_id="p", role="leader-candidate", enabled=True)
        self.call("leader-release", **self.lead)
        taken = self.call("leader-acquire", session_id=candidate, project_id="p")
        self.assertEqual(taken["last_handoff"]["session_id"], self.leader)
        self.assertIn("ask user about API", taken["last_handoff"]["text"])

    def test_workers_get_leader_alert_but_keep_their_next_action(self):
        self.assertIsNone(self.compact(self.first)["leader_alert"])
        task = self.task("Work")
        self.publish(task)
        attempt = self.started(task)
        before = self.compact(self.first)["next_action"]
        with patch("agentmesh.coordination.host_state", return_value="gone"):
            status = self.compact(self.first)
            self.assertEqual(status["leader_alert"]["state"], "gone")
            self.assertIn("Tell the user once", status["leader_alert"]["message"])
            self.assertEqual(status["next_action"], before)
            # The leader itself gets no alert about itself.
            self.assertIsNone(self.compact(self.leader)["leader_alert"])
            # Work continues: the worker can still submit.
            self.call("task-submit", **attempt, manifest={"paths": [], "summary": "done", "evidence": "ok"})
        self.call("leader-release", **self.lead)
        self.assertEqual(self.compact(self.second)["leader_alert"]["state"], "released")

    def test_leader_host_is_recorded_and_refreshed_after_resume(self):
        from agentmesh import coordination
        with patch("agentmesh.coordination.host_identity", return_value={"name": "claude.exe", "pid": 111, "started": "1"}):
            self.call("project-status", **self.lead, status="active", reason="first host")
        with patch("agentmesh.coordination.host_identity", return_value={"name": "claude.exe", "pid": 222, "started": "2"}):
            self.call("project-status", **self.lead, status="active", reason="resumed in a new process")
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        db.row_factory = sqlite3.Row
        try:
            self.assertEqual(coordination.recorded_host(db, "p", self.leader)["pid"], 222)
        finally:
            db.close()

    def test_leader_takeover_when_owner_session_is_gone(self):
        candidate = self.call("session-register", tool="claude-code", label="Next", capabilities=[])["session_id"]
        self.call("membership-set", session_id=candidate, project_id="p", role="leader-candidate", enabled=True)
        with patch("agentmesh.coordination.host_state", return_value="unknown"):
            with self.assertRaises(MeshError) as held:
                self.call("leader-acquire", session_id=candidate, project_id="p")
        self.assertEqual(held.exception.details["owner_session_state"], "valid")
        self.expire("UPDATE sessions SET lease_until=? WHERE id=?", self.leader)
        taken = self.call("leader-acquire", session_id=candidate, project_id="p")
        self.assertEqual(taken["generation"], self.lead["leader_generation"] + 1)
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        try:
            payload = db.execute("SELECT payload_json FROM events WHERE action='leader-acquire' ORDER BY seq DESC LIMIT 1").fetchone()[0]
        finally:
            db.close()
        self.assertIn("took over generation", json.loads(payload)["reason"])
        with self.assertRaises(MeshError):
            self.call("task-create", **self.lead, title="Old", description="Old", category="t", priority=1, required_capabilities=[], outputs=[], acceptance=["x"])
        # A closing leader releases its lease, so the next candidate acquires at once.
        third = self.call("session-register", tool="codex", label="Third", capabilities=[])["session_id"]
        self.call("membership-set", session_id=third, project_id="p", role="leader-candidate", enabled=True)
        self.call("session-close", session_id=candidate)
        self.assertEqual(self.call("leader-acquire", session_id=third, project_id="p")["generation"], taken["generation"] + 1)

    def test_worker_join_suffixes_duplicate_live_labels(self):
        join = {"project_id": "p", "tool": "codex", "capabilities": [], "role": "worker"}
        first = self.call("worker-join", **join, label="codex-A")
        second = self.call("worker-join", **join, label="codex-A")
        third = self.call("worker-join", **join, label="CODEX-A")
        self.assertEqual((first["label"], second["label"], third["label"]), ("codex-A", "codex-A-2", "CODEX-A-3"))
        self.assertNotIn("label_note", first)
        self.assertIn("label_note", second)
        self.call("session-close", session_id=first["session_id"])
        self.assertEqual(self.call("worker-join", **join, label="codex-A")["label"], "codex-A")

    def test_publish_warns_without_capable_worker_and_task_cancel(self):
        rust = self.task("Rust", ["rust"])
        published = self.call("task-publish", **self.lead, task_id=rust["task_id"], expected_version=rust["version"])
        self.assertEqual(published["warnings"][0]["code"], "no_capable_worker")
        python = self.task("Python", ["python"])
        self.assertEqual(self.call("task-publish", **self.lead, task_id=python["task_id"], expected_version=python["version"])["warnings"], [])
        status = self.compact(self.leader)
        counts = {x["id"]: x["capable_workers"] for x in status["tasks"]}
        self.assertEqual((counts[rust["task_id"]], counts[python["task_id"]]), (0, 2))
        self.assertIn("capable_workers 0", status["next_action"])
        self.assertEqual(self.call("task-cancel", **self.lead, task_id=rust["task_id"], reason="wrong capability")["state"], "cancelled")
        with self.assertRaises(MeshError):
            self.call("task-claim", session_id=self.first, project_id="p", task_id=rust["task_id"])
        base, dependent = self.task("Base"), self.task("Dependent")
        self.call("task-depend", **self.lead, task_id=dependent["task_id"], depends_on_id=base["task_id"])
        with self.assertRaises(MeshError) as refused:
            self.call("task-cancel", **self.lead, task_id=base["task_id"], reason="no longer needed")
        self.assertEqual(refused.exception.details["dependents"][0]["id"], dependent["task_id"])
        self.call("task-cancel", **self.lead, task_id=dependent["task_id"], reason="no longer needed")
        self.call("task-cancel", **self.lead, task_id=base["task_id"], reason="no longer needed")
        self.started(python)
        with self.assertRaises(MeshError):
            self.call("task-cancel", **self.lead, task_id=python["task_id"], reason="busy")

    def test_no_live_worker_is_a_wait_not_a_cancel_hint(self):
        for sid in (self.first, self.second):
            self.call("session-close", session_id=sid)
        task = self.task("Before workers join", ["python"])
        published = self.call("task-publish", **self.lead, task_id=task["task_id"], expected_version=task["version"])
        self.assertEqual([w["code"] for w in published["warnings"]], ["no_live_worker"])
        status = self.compact(self.leader)
        self.assertEqual(status["tasks"][0]["capable_workers"], 0)
        self.assertIn("re-arm", status["next_action"])
        self.assertNotIn("task-cancel", status["next_action"])

    def test_attempt_release_blocked_then_spec_unblock_and_reclaim(self):
        task = self.task("Unclear brief")
        self.publish(task)
        attempt = self.started(task)
        released = self.call("attempt-release", **attempt, reason="register layout has 8 slots, brief says 9", blocker=True)
        self.assertEqual(released["task_state"], "blocked")
        self.assertEqual(released["released_attempt_id"], attempt["attempt_id"])
        with self.assertRaises(MeshError):
            self.call("heartbeat", **attempt)
        status = self.compact(self.leader)
        item = next(x for x in status["tasks"] if x["id"] == task["task_id"])
        self.assertEqual(item["released"]["reason"], "register layout has 8 slots, brief says 9")
        self.assertIn("task-spec", status["next_action"])
        self.assertIsNone(self.compact(self.first)["my_active_attempt"])
        version = execute(self.root, "task-detail", {"project_id": "p", "task_id": task["task_id"]})["data"]["task"]["version"]
        self.call("task-spec", **self.lead, task_id=task["task_id"], expected_version=version, title="Clear brief", description="8 slots", category="test", priority=1, required_capabilities=[], outputs=[], acceptance=["Done"])
        self.assertEqual(self.call("task-unblock", **self.lead, task_id=task["task_id"], reason="brief fixed")["state"], "ready")
        again = self.started(task, self.second)
        self.assertEqual(self.call("attempt-release", **again, reason="out of time", blocker=False)["task_state"], "ready")

    def test_checkpoint_notes_reply_receipt_and_recover_hint(self):
        task = self.task("Talk")
        self.publish(task)
        attempt = self.started(task)
        self.call("checkpoint", **attempt, summary="Plain checkpoint", paths=[])
        self.call("checkpoint", **attempt, summary="Halfway", paths=[], note="schema parsed", question="label_id2 is missing; add it?", blocker=True)
        status = self.compact(self.leader)
        info = next(x for x in status["tasks"] if x["id"] == task["task_id"])["attempt"]
        self.assertEqual((info["worker_note"], info["question"], info["blocker_flag"]), ("schema parsed", "label_id2 is missing; add it?", True))
        self.assertEqual(info["lease_state"], "valid")
        self.assertIn("attempt-reply", status["next_action"])
        self.call("attempt-reply", **self.lead, task_id=task["task_id"], body="Yes, add label_id2 as INTEGER.")
        self.assertNotIn("attempt-reply", self.compact(self.leader)["next_action"])
        mine = self.compact(self.first)
        self.assertEqual(mine["my_active_attempt"]["leader_reply"]["text"], "Yes, add label_id2 as INTEGER.")
        self.assertIn("reply_seen", mine["next_action"])
        self.call("checkpoint", **attempt, summary="Added column", paths=[], reply_seen=True)
        info = next(x for x in self.compact(self.leader)["tasks"] if x["id"] == task["task_id"])["attempt"]
        self.assertTrue(info["leader_reply_seen"])
        self.assertNotIn("question", info)
        self.assertNotIn("leader_reply", self.compact(self.first)["next_action"])
        with self.assertRaises(MeshError):
            self.call("checkpoint", **attempt, summary="Bad", paths=[], blocker="yes")
        self.expire("UPDATE attempts SET lease_until=? WHERE id=?", attempt["attempt_id"])
        status = self.compact(self.leader)
        self.assertIn("recover", status["next_action"])
        self.assertEqual(next(x for x in status["tasks"] if x["id"] == task["task_id"])["attempt"]["lease_state"], "expired")

    def test_error_hints_for_project_id_json_heartbeat_and_accept(self):
        projects = self.root.parent / "projects"
        (projects / "anoma-model" / "config").mkdir(parents=True)
        (projects / "anoma-model" / "config" / "settings.json").write_text("{}", encoding="utf-8")
        request = self.root.parent / "request.json"
        request.write_text(json.dumps({"op_id": str(uuid.uuid4()), "project_id": "anoma-model", "tool": "codex", "label": "W", "capabilities": [], "role": "worker"}), encoding="utf-8")
        status, text = self.run_main(["--root", str(projects / "Anoma Model"), "worker-join", "--input", str(request)])
        error = json.loads(text)["error"]
        self.assertEqual((status, error["code"], error["did_you_mean"], error["known_projects"]), (2, "PROJECT_NOT_FOUND", "anoma-model", ["anoma-model"]))
        request.write_text('{"destination": "C:\\Users\\x"}', encoding="utf-8")
        status, text = self.run_main(["--root", str(self.root), "task-integrate", "--input", str(request)])
        error = json.loads(text)["error"]
        self.assertEqual((status, error["code"]), (2, "INVALID_INPUT"))
        self.assertIn("backslash", error["next_action"])
        with self.assertRaises(MeshError) as wrong:
            self.call("worker-join", project_id="Project", tool="codex", label="W", capabilities=[], role="worker")
        self.assertEqual((wrong.exception.code, wrong.exception.details["known_projects"]), ("PROJECT_NOT_FOUND", ["p"]))
        with self.assertRaises(MeshError) as beat:
            self.call("heartbeat", session_id=self.first)
        self.assertIn('"attempt_id":null', beat.exception.details["next_action"])
        task = self.task("Accept")
        self.publish(task)
        attempt = self.started(task)
        self.call("task-submit", **attempt, manifest={"paths": [], "summary": "Done", "evidence": "Tests"})
        accepted = self.call("review", session_id=self.leader, attempt_id=attempt["attempt_id"], outcome="accepted", evidence="OK", leader_generation=self.lead["leader_generation"])
        self.assertEqual(accepted["branch"], "agentmesh/" + task["task_id"] + "-" + attempt["attempt_id"])
        self.assertIn("task-integrate", accepted["next_action"])


if __name__ == "__main__":
    unittest.main()
