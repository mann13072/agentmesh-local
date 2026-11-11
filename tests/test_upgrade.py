"""Tests for the 2026-11-09 upgrade plan (UPGRADE-PLAN-2026-11-09.md). Each test names its plan step."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

os.environ["AGENTMESH_NO_POPUP"] = "1"  # never show a real approval popup during tests
os.environ["AGENTMESH_NO_WAKE"] = "1"  # never run a real codex queue during tests
os.environ["AGENTMESH_CHAT_HOME"] = str(Path(__file__).resolve().parent / "no-chat-home")  # never read real Codex/Antigravity chats
for _name in ("CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"):
    os.environ.pop(_name, None)

from agentmesh.cli import initialize
from agentmesh.coordination import execute, MeshError

SRC = str(Path(__file__).resolve().parents[1] / "src")


def legacy(root):
    """Tests that use membership-set for leader roles run with appointment codes switched off."""
    path = root / "config" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["require_appointment_codes"] = False
    path.write_text(json.dumps(settings), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name) / "mesh"
        self.assertEqual(initialize(self.root, {"root": str(self.root)})[1], 0)
        legacy(self.root)
        self.call("project-create", project_id="p", title="Project")
        self.leader = self.call("session-register", tool="codex", label="Leader", capabilities=[])["session_id"]
        self.call("membership-set", session_id=self.leader, project_id="p", role="leader-candidate", enabled=True)
        gen = self.call("leader-acquire", session_id=self.leader, project_id="p")["generation"]
        self.lead = {"session_id": self.leader, "project_id": "p", "leader_generation": gen}
        self.worker = self.call("worker-join", project_id="p", tool="claude-code", label="W", capabilities=["python"], role="worker")["session_id"]

    def tearDown(self):
        self.temp.cleanup()

    def call(self, command, **values):
        return execute(self.root, command, {"op_id": str(uuid.uuid4()), **values})["data"]

    def db(self):
        db = sqlite3.connect(self.root / "runtime" / "coordination.db")
        db.row_factory = sqlite3.Row
        return db

    def sql(self, statement, *args):
        db = self.db()
        try:
            rows = db.execute(statement, args).fetchall()
            db.commit()
            return rows
        finally:
            db.close()

    def task(self, title="T", priority=50, caps=(), publish=True):
        t = self.call("task-create", **self.lead, title=title, description="d", category="c", priority=priority,
                      required_capabilities=list(caps), outputs=[], acceptance=["a"])
        if publish:
            self.call("task-publish", **self.lead, task_id=t["task_id"], expected_version=t["version"])
            t["version"] += 1
        return t

    def started(self, task, sid=None):
        sid = sid or self.worker
        claim = self.call("task-claim", session_id=sid, project_id="p", task_id=task["task_id"])
        attempt = {"session_id": sid, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-start", **attempt)
        return attempt

    def compact(self, sid, **extra):
        return execute(self.root, "status", {"session_id": sid, "project_id": "p", "view": "compact", **extra})["data"]

    def cli(self, command, data, env=None):
        env = dict(env if env is not None else os.environ)
        env["PYTHONPATH"] = SRC
        process = subprocess.run([sys.executable, "-m", "agentmesh.cli", "--root", str(self.root), command, "--input", "-"],
                                 input=json.dumps(data, ensure_ascii=False).encode("utf-8"), capture_output=True, env=env, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return process


class Release02(Base):
    def test_0_2_3_non_cp1252_output_without_pythonioencoding(self):
        self.task(title="Stage 1 → Stage 2 ✓")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        process = self.cli("status", {"session_id": self.worker, "project_id": "p", "view": "compact"}, env)
        self.assertEqual(process.returncode, 0, process.stderr)
        data = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(data["data"]["tasks"][0]["title"], "Stage 1 → Stage 2 ✓")
        self.assertIn("cli_version", data)

    def test_0_2_4_wrong_types_give_json(self):
        for command, data in (("status", {"session_id": self.worker, "project_id": ["p"]}),
                              ("task-detail", {"project_id": {"a": 1}, "task_id": str(uuid.uuid4())})):
            process = self.cli(command, data)
            self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
            self.assertEqual(json.loads(process.stdout)["error"]["code"], "INVALID_INPUT")

    def test_0_2_5_claimable_task_is_not_hidden_by_100_others(self):
        db = self.db()
        try:
            for i in range(100):
                db.execute("INSERT INTO tasks(id,project_id,title,description,category,priority,required_capabilities_json,outputs_json,acceptance_json,state,version,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,'[]','[]','[\"a\"]','proposed',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", (str(uuid.uuid4()), "p", f"hi-{i}", "d", "c", 90))
            db.commit()
        finally:
            db.close()
        low = self.task(title="the only ready task", priority=10)
        status = self.compact(self.worker)
        self.assertEqual(status["tasks"][0]["id"], low["task_id"])
        self.assertTrue(status["tasks"][0]["claimable"])
        self.assertIn(low["task_id"], status["next_action"])

    def test_0_2_6_capabilities_are_normalised_and_titles_single_line(self):
        t = self.task(caps=["Python "])
        self.call("task-claim", session_id=self.worker, project_id="p", task_id=t["task_id"])
        upper = self.call("worker-join", project_id="p", tool="codex", label="U", capabilities=["PYTHON"], role="worker")
        caps = self.sql("SELECT capabilities_json FROM sessions WHERE id=?", upper["session_id"])[0][0]
        self.assertEqual(json.loads(caps), ["python"])
        with self.assertRaises(MeshError):
            self.call("task-create", **self.lead, title="a\nb", description="d", category="c", priority=1, required_capabilities=[], outputs=[], acceptance=["a"])

    def test_0_2_7_rejected_task_can_be_reopened_or_cancelled(self):
        t = self.task()
        a = self.started(t)
        self.call("task-submit", **a, manifest={"paths": [], "summary": "s", "evidence": "e"})
        self.call("review", session_id=self.leader, attempt_id=a["attempt_id"], outcome="rejected", evidence="no", leader_generation=self.lead["leader_generation"])
        reopened = self.call("task-reopen", **self.lead, task_id=t["task_id"], reason="try again")
        self.assertEqual(reopened["state"], "proposed")
        spec = self.call("task-spec", **self.lead, task_id=t["task_id"], expected_version=reopened["version"], title="T2", description="d", category="c",
                         priority=1, required_capabilities=[], outputs=[], acceptance=["a"])
        self.call("task-publish", **self.lead, task_id=t["task_id"], expected_version=spec["version"])
        second = self.call("task-claim", session_id=self.worker, project_id="p", task_id=t["task_id"])
        self.assertEqual(second["generation"], 2)
        other = self.task(title="X")
        b = self.started(other, self.call("worker-join", project_id="p", tool="codex", label="W2", capabilities=[], role="worker")["session_id"])
        self.call("task-submit", **b, manifest={"paths": [], "summary": "s", "evidence": "e"})
        self.call("review", session_id=self.leader, attempt_id=b["attempt_id"], outcome="rejected", evidence="no", leader_generation=self.lead["leader_generation"])
        self.assertEqual(self.call("task-cancel", **self.lead, task_id=other["task_id"], reason="drop")["state"], "cancelled")

    def test_0_2_8_expired_holder_keeps_attempt_until_recover(self):
        t, other = self.task(), self.task(title="Other")
        a = self.started(t)
        self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", a["attempt_id"])
        self.sql("UPDATE sessions SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", self.worker)
        self.assertIn("heartbeat", self.compact(self.worker)["next_action"])
        with self.assertRaises(MeshError) as second:
            self.call("task-claim", session_id=self.worker, project_id="p", task_id=other["task_id"])
        self.assertIn(second.exception.details["blocker"], ("session_expired", "has_active_attempt"))
        self.call("heartbeat", **a)
        with self.assertRaises(MeshError) as busy:
            self.call("task-claim", session_id=self.worker, project_id="p", task_id=other["task_id"])
        self.assertEqual(busy.exception.details["blocker"], "has_active_attempt")
        self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", a["attempt_id"])
        self.call("task-submit", **a, manifest={"paths": [], "summary": "s", "evidence": "e"})
        t2 = self.task(title="Third")
        b = self.started(t2)
        self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", b["attempt_id"])
        self.call("recover", **self.lead, task_id=t2["task_id"], reason="expired")
        with self.assertRaises(MeshError):
            self.call("task-submit", **b, manifest={"paths": [], "summary": "s", "evidence": "e"})

    def test_0_2_9_heartbeats_leave_no_log_and_mutations_renew(self):
        t = self.task()
        a = self.started(t)
        before = [self.sql("SELECT COUNT(*) FROM events")[0][0], self.sql("SELECT COUNT(*) FROM operations")[0][0]]
        for _ in range(5):
            execute(self.root, "heartbeat", {"session_id": self.worker, **{k: a[k] for k in ("attempt_id", "generation")}})
        self.assertEqual([self.sql("SELECT COUNT(*) FROM events")[0][0], self.sql("SELECT COUNT(*) FROM operations")[0][0]], before)
        self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", a["attempt_id"])
        self.call("checkpoint", **a, summary="s", paths=[])
        lease = self.sql("SELECT lease_until FROM attempts WHERE id=?", a["attempt_id"])[0][0]
        self.assertGreater(lease, "2026")

    def test_0_2_10_sync_waits_for_lock_and_skips_unchanged_files(self):
        import msvcrt
        self.call("sync")
        lock = open(self.root / "runtime" / "projection.lock", "r+b")
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

        def release():
            time.sleep(1)
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            lock.close()
        threading.Thread(target=release).start()
        self.call("sync")
        note = self.root / "vault" / "02-Agents" / "Sessions" / (self.worker + ".md")
        stamp = note.stat().st_mtime_ns
        time.sleep(0.05)
        self.call("sync")
        self.assertEqual(note.stat().st_mtime_ns, stamp)

    def test_0_2_11_version_flag(self):
        env = dict(os.environ, PYTHONPATH=SRC)
        process = subprocess.run([sys.executable, "-m", "agentmesh.cli", "--version"], capture_output=True, text=True, env=env, timeout=30)
        from agentmesh import __version__
        self.assertEqual(process.stdout.strip(), __version__)


def git(repo, *args):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout.strip()


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    git(path, "config", "core.autocrlf", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-m", "base")
    return path


class Release03(Base):
    def code(self, role):
        from agentmesh.coordination import create_appointment
        db = self.db()
        try:
            code, _ = create_appointment(db, "p", role)
            db.commit()
            return code
        finally:
            db.close()

    def coded(self):
        path = self.root / "config" / "settings.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["require_appointment_codes"] = True
        path.write_text(json.dumps(settings), encoding="utf-8")

    def join(self, command, code, **extra):
        return self.call(command, project_id="p", tool="claude-code", label=command, capabilities=[], appointment_code=code, rulebook_version="3.0", **extra)

    def test_0_3_1_version_1_database_migrates_with_backup(self):
        from agentmesh.coordination import v1_schema_text, schema_text
        from agentmesh.cli import schema_matches
        old = Path(self.temp.name) / "old"
        self.assertEqual(initialize(old, {"root": str(old)})[1], 0)
        legacy(old)
        db_path = old / "runtime" / "coordination.db"
        db_path.unlink()
        db = sqlite3.connect(db_path)
        db.executescript(v1_schema_text())
        db.execute("INSERT INTO projects VALUES('p','P','active','2026-01-01T00:00:00Z')")
        db.commit()
        db.close()
        execute(old, "list", {"project_id": "p", "kind": "tasks", "cursor": None, "limit": 5})
        db = sqlite3.connect(db_path)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertTrue(schema_matches(db, schema_text()))
        finally:
            db.close()
        self.assertEqual(len(list((old / "backups").glob("pre-v2-*.db"))), 1)
        execute(old, "list", {"project_id": "p", "kind": "tasks", "cursor": None, "limit": 5})
        self.assertEqual(len(list((old / "backups").glob("pre-v2-*.db"))), 1)

    def test_0_3_2_trusted_roles_need_codes(self):
        self.coded()
        with self.assertRaises(MeshError):
            self.call("membership-set", session_id=self.worker, project_id="p", role="reviewer", enabled=True)
        with self.assertRaises(MeshError):
            self.call("membership-set", session_id=self.worker, project_id="p", role="leader-candidate", enabled=True)
        with self.assertRaises(MeshError):
            self.call("membership-set", session_id=self.leader, project_id="p", role="leader-candidate", enabled=False)
        candidate = self.call("session-register", tool="codex", label="x", capabilities=[])["session_id"]
        with self.assertRaises(MeshError):
            self.call("leader-acquire", session_id=candidate, project_id="p")
        # (c) a code works once
        code = self.code("leader")
        joined = self.join("leader-join", code)
        self.assertEqual(joined["generation"], self.lead["leader_generation"] + 1)
        with self.assertRaises(MeshError) as reused:
            self.join("leader-join", code)
        self.assertIn("already used", str(reused.exception))
        # (f) the old leader is fenced
        with self.assertRaises(MeshError) as fenced:
            self.call("project-status", **self.lead, status="paused", reason="old")
        self.assertIn("Stop all leader work", fenced.exception.details["next_action"])
        # (d) wrong role / (e) expired
        with self.assertRaises(MeshError):
            self.join("reviewer-join", self.code("leader"))
        expired = self.code("reviewer")
        self.sql("UPDATE appointments SET expires_at='2000-01-01T00:00:00Z' WHERE redeemed_by IS NULL AND role='reviewer'")
        with self.assertRaises(MeshError) as late:
            self.join("reviewer-join", expired)
        self.assertIn("expired", str(late.exception))
        with self.assertRaises(MeshError):
            self.call("leader-join", project_id="p", tool="codex", label="x", capabilities=[], appointment_code=self.code("leader"), rulebook_version="2.8")
        reviewer = self.join("reviewer-join", self.code("reviewer"))
        self.assertEqual(reviewer["role"], "reviewer")

    def popup(self, answer):
        from agentmesh import coordination
        asked = []
        original = coordination.ask_user_approval
        coordination.ask_user_approval = lambda caption, text: asked.append((caption, text)) or answer
        self.addCleanup(setattr, coordination, "ask_user_approval", original)
        return asked

    def test_0_5_2_popup_click_replaces_the_code(self):
        self.coded()
        asked = self.popup("yes")
        data = {"op_id": str(uuid.uuid4()), "project_id": "p", "tool": "claude-code", "label": "Lead", "capabilities": [], "rulebook_version": "3.0"}
        joined = execute(self.root, "leader-join", dict(data))["data"]
        self.assertEqual(joined["generation"], self.lead["leader_generation"] + 1)
        self.assertEqual(len(asked), 1)
        self.assertIn("leader of AgentMesh project p", asked[0][1])
        # a retry with the same op_id replays the answer without a second popup
        self.assertEqual(execute(self.root, "leader-join", dict(data))["data"]["session_id"], joined["session_id"])
        self.assertEqual(len(asked), 1)
        with self.assertRaises(MeshError):
            self.call("project-status", **self.lead, status="paused", reason="old leader is fenced")
        payload = json.loads(self.sql("SELECT payload_json FROM events WHERE action='leader-join' ORDER BY seq DESC LIMIT 1")[0][0])
        self.assertEqual(payload["approval"], "popup click")
        self.assertEqual(self.join("reviewer-join", self.code("reviewer"))["role"], "reviewer")  # codes still work
        self.assertEqual(len(asked), 1)

    def test_0_5_2_no_yes_changes_nothing(self):
        self.coded()
        sessions = self.sql("SELECT COUNT(*) FROM sessions")[0][0]
        for answer, words in (("no", "declined"), ("timeout", "did not answer"), ("unavailable", "could not be shown")):
            self.popup(answer)
            with self.assertRaises(MeshError) as refused:
                self.call("leader-join", project_id="p", tool="codex", label="x", capabilities=[], rulebook_version="3.0")
            self.assertIn(words, str(refused.exception))
            self.assertTrue(refused.exception.details["next_action"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sessions")[0][0], sessions)
        self.assertEqual(self.sql("SELECT session_id FROM leader_leases WHERE project_id='p'")[0][0], self.leader)

    def test_0_5_2_bad_input_never_shows_a_popup(self):
        self.coded()
        asked = self.popup("yes")
        for extra in ({"rulebook_version": "2.8"}, {"project_id": "nope"}, {"tool": "notepad"}, {"label": "  "}):
            values = {"project_id": "p", "tool": "codex", "label": "x", "capabilities": [], "rulebook_version": "3.0", **extra}
            with self.assertRaises(MeshError):
                self.call("integrator-join", **values)
        self.assertEqual(asked, [])

    def test_0_3_2_reviewer_registered_after_submit_cannot_review(self):
        t = self.task()
        a = self.started(t)
        self.call("task-submit", **a, manifest={"paths": [], "summary": "s", "evidence": "e"})
        sock = self.call("session-register", tool="other", label="sock", capabilities=[])["session_id"]
        self.call("membership-set", session_id=sock, project_id="p", role="reviewer", enabled=True)
        with self.assertRaises(MeshError) as refused:
            self.call("review", session_id=sock, attempt_id=a["attempt_id"], outcome="accepted", evidence="ok")
        self.assertIn("registered after", str(refused.exception))
        reviewer = self.join("reviewer-join", self.code("reviewer"))
        self.call("review", session_id=reviewer["session_id"], attempt_id=a["attempt_id"], outcome="accepted", evidence="ok")

    def test_0_3_3_cli_owns_git(self):
        repo = make_repo(Path(self.temp.name) / "app")
        self.call("project-git", **self.lead, repository=str(repo), base_branch="main")
        first, second = self.task("A", publish=False), self.task("B", publish=False)
        self.call("task-depend", **self.lead, task_id=second["task_id"], depends_on_id=first["task_id"])
        for t, version in ((first, 1), (second, 2)):
            self.call("task-publish", **self.lead, task_id=t["task_id"], expected_version=version)
        # (a) the claim creates the worktree on the task branch
        claim = execute(self.root, "task-claim", {"op_id": str(uuid.uuid4()), "session_id": self.worker, "project_id": "p", "task_id": first["task_id"], "start": True})["data"]
        self.assertTrue(claim["git"]["created"], claim["git"])
        self.assertEqual(claim["state"], "in_progress")
        self.assertEqual(claim["task"]["title"], "A")
        wt = Path(claim["worktree"])
        self.assertEqual(git(wt, "branch", "--show-current"), claim["branch"])
        attempt = {"session_id": self.worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        manifest = {"paths": [], "summary": "s", "evidence": "e"}
        with self.assertRaises(MeshError) as empty:
            self.call("task-submit", **attempt, manifest=manifest)
        self.assertIn("no commit", str(empty.exception))
        # (b) uncommitted work is refused
        (wt / "feature.txt").write_text("feature\n", encoding="utf-8")
        with self.assertRaises(MeshError) as dirty:
            self.call("task-submit", **attempt, manifest=manifest)
        self.assertIn("uncommitted", str(dirty.exception))
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "feature")
        head = git(wt, "rev-parse", "HEAD")
        submitted = self.call("task-submit", **attempt, manifest=manifest)
        self.assertEqual(submitted["head_sha"], head)
        # (c) review must name the submitted commit
        review = {"session_id": self.leader, "attempt_id": claim["attempt_id"], "outcome": "accepted", "evidence": "ok", "leader_generation": self.lead["leader_generation"]}
        with self.assertRaises(MeshError):
            self.call("review", **review)
        with self.assertRaises(MeshError):
            self.call("review", **review, reviewed_sha="0" * 40)
        self.call("review", **review, reviewed_sha=head)
        # (d) a commit after review is not merged
        (wt / "late.txt").write_text("late\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "late")
        # (e) dependent blocked until integration
        with self.assertRaises(MeshError) as gate:
            self.call("task-claim", session_id=self.worker, project_id="p", task_id=second["task_id"])
        self.assertEqual(gate.exception.details["blocker"], "dependency_not_integrated")
        integrate = {**self.lead, "task_id": first["task_id"], "evidence": "tests pass on main"}
        with self.assertRaises(MeshError) as not_merged:
            self.call("task-integrate", **integrate)
        self.assertIn("not in the base branch", str(not_merged.exception))
        done = self.call("task-integrate", **integrate, merge=True)
        self.assertEqual(done["head_sha"], head)
        self.assertEqual(done["warnings"][0]["code"], "later_commits_not_merged")
        self.assertTrue((repo / "feature.txt").exists())
        self.assertFalse((repo / "late.txt").exists())
        self.assertEqual(git(repo, "rev-parse", "HEAD"), done["merge_sha"])
        self.call("task-claim", session_id=self.worker, project_id="p", task_id=second["task_id"])

    def test_0_3_3_merge_conflict_leaves_repository_clean(self):
        repo = make_repo(Path(self.temp.name) / "app")
        self.call("project-git", **self.lead, repository=str(repo), base_branch="main")
        t = self.task()
        claim = self.call("task-claim", session_id=self.worker, project_id="p", task_id=t["task_id"], start=True)
        wt = Path(claim["worktree"])
        (wt / "README.md").write_text("worker\n", encoding="utf-8")
        git(wt, "commit", "-am", "worker change")
        head = git(wt, "rev-parse", "HEAD")
        attempt = {"session_id": self.worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-submit", **attempt, manifest={"paths": [], "summary": "s", "evidence": "e"})
        self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="accepted", evidence="ok", leader_generation=self.lead["leader_generation"], reviewed_sha=head)
        (repo / "README.md").write_text("main moved\n", encoding="utf-8")
        git(repo, "commit", "-am", "main change")
        with self.assertRaises(MeshError) as conflict:
            self.call("task-integrate", **self.lead, task_id=t["task_id"], evidence="x", merge=True)
        self.assertEqual(conflict.exception.code, "GIT_ERROR")
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_0_3_3_project_git_refuses_a_plain_folder(self):
        folder = Path(self.temp.name) / "plain"
        folder.mkdir()
        with self.assertRaises(MeshError) as refused:
            self.call("project-git", **self.lead, repository=str(folder), base_branch="main")
        self.assertIn("Do not run git init yourself", refused.exception.details["next_action"])


class Release04(Base):
    def test_0_4_1_next_renews_and_is_small_when_unchanged(self):
        t = self.task()
        first = execute(self.root, "next", {"session_id": self.worker, "project_id": "p"})["data"]
        self.assertTrue(first["changed"])
        self.assertEqual(first["call"]["command"], "task-claim")
        claim = self.call("task-claim", **first["call"]["input"])
        self.assertEqual(claim["state"], "in_progress")
        self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", claim["attempt_id"])
        again = execute(self.root, "next", {"session_id": self.worker, "project_id": "p"})["data"]
        lease = self.sql("SELECT lease_until FROM attempts WHERE id=?", claim["attempt_id"])[0][0]
        self.assertGreater(lease, "2026")
        quiet = execute(self.root, "next", {"session_id": self.worker, "project_id": "p", "since": again["seq"]})["data"]
        self.assertFalse(quiet["changed"])
        self.assertLess(len(json.dumps(quiet)), 400)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM events WHERE action='next'")[0][0], 0)

    def test_0_4_1_leader_next_offers_review_with_sha(self):
        t = self.task()
        a = self.started(t)
        self.call("task-submit", **a, manifest={"paths": [], "summary": "s", "evidence": "e"})
        view = execute(self.root, "next", {"session_id": self.leader, "project_id": "p"})["data"]
        self.assertEqual(view["call"]["command"], "review")
        self.assertEqual(view["call"]["input"]["attempt_id"], a["attempt_id"])
        self.assertEqual(view["leader_generation"], self.lead["leader_generation"])

    def test_0_4_2_join_answers_with_status_commands_and_rules(self):
        process = self.cli("worker-join", {"op_id": str(uuid.uuid4()), "project_id": "p", "tool": "codex", "label": "J", "capabilities": [], "role": "worker", "rulebook_version": "3.0"})
        self.assertEqual(process.returncode, 0, process.stderr)
        data = json.loads(process.stdout)["data"]
        self.assertIn("next_action", data["status"])
        self.assertIn("task-claim", data["commands"])
        self.assertEqual(data["reminder_seconds"], 180)
        self.assertIn("required", data["commands"]["task-submit"])

    def test_0_4_3_op_id_optional_and_plan_import(self):
        plan = [{"key": f"s{i}", "title": f"Stage {i}", "description": "d", "category": "c", "priority": 50, "required_capabilities": [],
                 "outputs": [], "acceptance": ["a"], "depends_on": [f"s{i-1}"] if i else []} for i in range(5)]
        ids = self.call("plan-import", **self.lead, tasks=plan, publish=True)["task_ids"]
        status = self.compact(self.worker)
        claimable = [x["id"] for x in status["tasks"] if x["claimable"]]
        self.assertEqual(claimable, [ids["s0"]])
        claim = self.call("task-claim", session_id=self.worker, project_id="p", task_id=ids["s0"], start=True)
        execute(self.root, "checkpoint", {"session_id": self.worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"], "summary": "s", "paths": []})
        execute(self.root, "task-submit", {"session_id": self.worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"], "manifest": {"paths": [], "summary": "s", "evidence": "e"}})
        self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="accepted", evidence="ok", leader_generation=self.lead["leader_generation"])
        self.assertNotIn(ids["s1"], [x["id"] for x in self.compact(self.worker)["tasks"] if x["claimable"]])
        self.call("task-integrate", **self.lead, task_id=ids["s0"], destination=str(self.root), evidence="ok")
        self.assertIn(ids["s1"], [x["id"] for x in self.compact(self.worker)["tasks"] if x["claimable"]])
        bad = [dict(plan[0], key="a", depends_on=["b"]), dict(plan[0], key="b", depends_on=["a"])]
        with self.assertRaises(MeshError):
            self.call("plan-import", **self.lead, tasks=bad, publish=False)

    def test_0_4_4_rulebook_names_only_real_commands_and_has_role_cards(self):
        import re
        from agentmesh.cli import COMMANDS, role_card
        from agentmesh.admin import OPERATOR
        book = (Path(__file__).resolve().parents[1] / "vault" / "04-Skills" / "agent-mesh" / "SKILL.md").read_text(encoding="utf-8")
        self.assertLess(len(book.encode("utf-8")), 8192)
        known = set(COMMANDS) | set(OPERATOR) | {"help"}
        for name in set(re.findall(r"`([a-z]+(?:-[a-z]+)+|next|review|status|recover|sync|backup|whoami|metrics|checkpoint|heartbeat)`", book)):
            self.assertIn(name, known, f"rulebook names unknown command {name}")
        for role in ("worker", "leader", "reviewer"):
            self.assertTrue(role_card(role), role)


class Release05(Base):
    def test_0_5_1_messages_and_read_receipts(self):
        sent = self.call("message-send", session_id=self.worker, project_id="p", to="leader", task_id=None, body="Which port?")
        view = execute(self.root, "next", {"session_id": self.leader, "project_id": "p"})["data"]
        self.assertEqual(view["inbox"][0]["body"], "Which port?")
        execute(self.root, "message-ack", {"session_id": self.leader, "project_id": "p", "message_ids": sent["message_ids"]})
        self.assertEqual(execute(self.root, "next", {"session_id": self.leader, "project_id": "p"})["data"]["inbox"], [])
        listed = execute(self.root, "list", {"project_id": "p", "kind": "messages", "cursor": None, "limit": 10})["data"]["items"]
        self.assertIsNotNone(listed[0]["read_at"])

    def test_0_5_3_whoami_finds_the_session_by_host(self):
        from unittest.mock import patch
        host = {"name": "codex.exe", "pid": 4242, "started": "99"}
        with patch("agentmesh.coordination.host_identity", return_value=host):
            joined = self.call("worker-join", project_id="p", tool="codex", label="lost", capabilities=[], role="worker")
            found = execute(self.root, "whoami", {"project_id": "p"})["data"]
        self.assertIn(joined["session_id"], [x["session_id"] for x in found["sessions"]])
        with patch("agentmesh.coordination.host_identity", return_value=None):
            with self.assertRaises(MeshError):
                execute(self.root, "whoami", {"project_id": "p"})

    def test_0_5_4_metrics(self):
        t = self.task()
        a = self.started(t)
        self.call("task-submit", **a, manifest={"paths": [], "summary": "s", "evidence": "e"})
        self.call("review", session_id=self.leader, attempt_id=a["attempt_id"], outcome="revision", evidence="fix", leader_generation=self.lead["leader_generation"])
        numbers = execute(self.root, "metrics", {"project_id": "p"})["data"]
        self.assertEqual(numbers["by_author_tool"]["claude-code"]["revision_rate"], 1.0)
        self.assertEqual(numbers["totals"]["tasks"], 1)

    def run_operator(self, argv, terminal):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from agentmesh.cli import main
        buffer = io.StringIO()
        with patch("agentmesh.admin.is_terminal", return_value=terminal), redirect_stdout(buffer):
            status = main(argv)
        return status, buffer.getvalue()

    def test_0_5_6_halt_and_operator_commands_need_a_terminal(self):
        status, text = self.run_operator(["--root", str(self.root), "halt", "stop", "now"], terminal=False)
        self.assertEqual(status, 3)
        status, text = self.run_operator(["--root", str(self.root), "halt", "stop", "now"], terminal=True)
        self.assertEqual(status, 0, text)
        t = self.task  # creating tasks is refused while halted
        with self.assertRaises(MeshError) as halted:
            t()
        self.assertIn("halted", str(halted.exception))
        self.assertIn("halted", execute(self.root, "next", {"session_id": self.worker, "project_id": "p"})["data"]["next_action"])
        self.assertEqual(self.run_operator(["--root", str(self.root), "resume"], terminal=True)[0], 0)
        self.task()
        status, text = self.run_operator(["--root", str(self.root), "appoint", "reviewer"], terminal=True)
        self.assertEqual(status, 0, text)
        code = re.search(r"REVW-[A-Z0-9]{4}-[A-Z0-9]{4}", text).group(0)
        joined = self.call("reviewer-join", project_id="p", tool="codex", label="R", capabilities=[], appointment_code=code, rulebook_version="3.0")
        self.assertEqual(joined["role"], "reviewer")
        self.assertEqual(self.run_operator(["--root", str(self.root), "appoint", "reviewer"], terminal=False)[0], 3)
        status, text = self.run_operator(["--root", str(self.root), "board"], terminal=False)
        self.assertEqual(status, 0, text)
        self.assertIn("Leader:", text)

    def test_0_5_2_watchdog_recovers_syncs_and_backs_up(self):
        from unittest.mock import patch
        from agentmesh.admin import watchdog_project
        t = self.task()
        a = self.started(t)
        self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", a["attempt_id"])
        self.sql("INSERT OR REPLACE INTO session_hosts VALUES(?,?,?,?,?)", self.worker, "codex.exe", 1, "1", "2000-01-01T00:00:00Z")
        with patch("agentmesh.admin.host_state", return_value="gone"):
            project, summary = watchdog_project(self.root)
        self.assertEqual(summary["recovered"], [t["task_id"]])
        self.assertTrue(summary["synced"])
        self.assertTrue(summary["backup"])
        self.assertEqual(self.sql("SELECT state FROM tasks WHERE id=?", t["task_id"])[0][0], "ready")
        with self.assertRaises(MeshError):
            self.call("task-submit", **a, manifest={"paths": [], "summary": "s", "evidence": "e"})
        self.assertTrue((self.root / "vault" / "00-Dashboard" / "Attention.md").is_file())
        # A worker whose program still runs is left alone.
        b = self.started(t)
        self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", b["attempt_id"])
        with patch("agentmesh.admin.host_state", return_value="alive"):
            self.assertEqual(watchdog_project(self.root)[1]["recovered"], [])

    def test_0_5_7_gc_lists_then_removes_merged_worktrees(self):
        repo = make_repo(Path(self.temp.name) / "app")
        self.call("project-git", **self.lead, repository=str(repo), base_branch="main")
        t = self.task()
        claim = self.call("task-claim", session_id=self.worker, project_id="p", task_id=t["task_id"], start=True)
        wt = Path(claim["worktree"])
        (wt / "f.txt").write_text("f\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "f")
        head = git(wt, "rev-parse", "HEAD")
        attempt = {"session_id": self.worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
        self.call("task-submit", **attempt, manifest={"paths": [], "summary": "s", "evidence": "e"})
        self.call("review", session_id=self.leader, attempt_id=claim["attempt_id"], outcome="accepted", evidence="ok", leader_generation=self.lead["leader_generation"], reviewed_sha=head)
        self.call("task-integrate", **self.lead, task_id=t["task_id"], evidence="ok", merge=True)
        status, text = self.run_operator(["--root", str(self.root), "gc"], terminal=True)
        self.assertIn("would remove", text)
        self.assertTrue(wt.exists())
        status, text = self.run_operator(["--root", str(self.root), "gc", "--apply"], terminal=True)
        self.assertFalse(wt.exists(), text)
        self.assertTrue(git(repo, "branch", "--list", claim["branch"]))

    def test_0_5_8_review_by_a_different_tool(self):
        path = self.root / "config" / "settings.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["review_requires_different_tool"] = True
        path.write_text(json.dumps(settings), encoding="utf-8")
        same = self.call("session-register", tool="claude-code", label="r", capabilities=[])["session_id"]
        self.call("membership-set", session_id=same, project_id="p", role="reviewer", enabled=True)
        t = self.task()
        a = self.started(t)
        self.call("task-submit", **a, manifest={"paths": [], "summary": "s", "evidence": "e"})
        with self.assertRaises(MeshError) as refused:
            self.call("review", session_id=same, attempt_id=a["attempt_id"], outcome="accepted", evidence="ok")
        self.assertIn("same tool", str(refused.exception))

    def test_0_5_9_feedback_one_file_per_agent(self):
        self.call("feedback-add", session_id=self.worker, project_id="p", body="## worker\n- fine")
        path = self.root / "feedback" / "p" / (self.worker + ".md")
        self.assertTrue(path.is_file())
        with self.assertRaises(MeshError):
            self.call("feedback-add", session_id=self.worker, project_id="p", body="\n".join(["x"] * 41))

    def test_0_5_11_simulator_keeps_invariants(self):
        """Random legal and illegal commands; after each: valid JSON-able result or MeshError, and the invariants hold."""
        import random
        rng = random.Random(1109)
        workers = [self.worker] + [self.call("worker-join", project_id="p", tool="codex", label=f"w{i}", capabilities=[], role="worker")["session_id"] for i in range(2)]
        tasks = [self.task(f"T{i}")["task_id"] for i in range(4)]
        attempts = []
        ops = 0
        for step in range(400):
            sid = rng.choice(workers + [self.leader])
            choice = rng.randrange(9)
            a = rng.choice(attempts) if attempts else None
            try:
                if choice == 0:
                    attempts.append(self.call("task-claim", session_id=sid, project_id="p", task_id=rng.choice(tasks)) | {"session_id": sid})
                elif choice == 1 and a:
                    self.call("task-start", session_id=a["session_id"], attempt_id=a["attempt_id"], generation=a["generation"])
                elif choice == 2 and a:
                    self.call("task-submit", session_id=a["session_id"], attempt_id=a["attempt_id"], generation=a["generation"], manifest={"paths": [], "summary": "s", "evidence": "e"})
                elif choice == 3 and a:
                    self.call("review", session_id=sid, attempt_id=a["attempt_id"], outcome=rng.choice(["accepted", "revision", "rejected", "blocked"]), evidence="e",
                              leader_generation=self.lead["leader_generation"])
                elif choice == 4 and a:
                    self.sql("UPDATE attempts SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", a["attempt_id"])
                    self.call("recover", **self.lead, task_id=rng.choice(tasks), reason="r")
                elif choice == 5 and a:
                    self.call("attempt-release", session_id=a["session_id"], attempt_id=a["attempt_id"], generation=a["generation"], reason="r", blocker=rng.random() < 0.5)
                elif choice == 6:
                    self.call("task-unblock", **self.lead, task_id=rng.choice(tasks), reason="r")
                elif choice == 7:
                    self.call("task-integrate", **self.lead, task_id=rng.choice(tasks), destination=str(self.root), evidence="e")
                else:
                    execute(self.root, "next", {"session_id": sid, "project_id": "p"})
                ops += 1
            except MeshError:
                pass
            current = self.sql("SELECT t.id,a.session_id FROM tasks t JOIN attempts a ON a.id=t.current_attempt_id WHERE a.state IN ('claimed','in_progress')")
            holders = [r[1] for r in current]
            self.assertEqual(len(holders), len(set(holders)), f"a session holds two active attempts at step {step}")
            self.assertEqual(self.sql("SELECT COUNT(*) FROM tasks t WHERE state='completed' AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.id=t.current_attempt_id AND a.state='accepted')")[0][0], 0)
            self.assertEqual(self.sql("SELECT COUNT(*) FROM integrations i JOIN attempts a ON a.id=i.attempt_id WHERE a.state!='accepted'")[0][0], 0)
            self.assertEqual(self.sql("SELECT COUNT(*) FROM tasks t WHERE state IN ('claimed','in_progress') AND current_attempt_id IS NULL")[0][0], 0)
        self.assertGreater(ops, 50)


import re  # noqa: E402  (used by the operator test)


class Release053(Base):
    """0.5.3: the CLI records each Codex/Antigravity worker's chat id; the leader's next lists how to wake it."""

    def setUp(self):
        super().setUp()
        self.home = Path(self.temp.name) / "home"
        os.environ["AGENTMESH_CHAT_HOME"] = str(self.home)
        self.addCleanup(os.environ.__setitem__, "AGENTMESH_CHAT_HOME", str(Path(__file__).resolve().parent / "no-chat-home"))

    def env(self, **values):
        for name, value in values.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)

    def join(self, tool):
        return self.call("worker-join", project_id="p", tool=tool, label=tool, capabilities=[], role="worker")["session_id"]

    def chats(self, sid):
        return [json.loads(r[0]) for r in self.sql("SELECT payload_json FROM events WHERE session_id=? AND action LIKE '%-chat-id'", sid)]

    def nxt(self, **values):
        return execute(self.root, "next", values)["data"]

    def wake(self):
        return {w["session_id"]: w for w in self.nxt(session_id=self.leader, project_id="p").get("wake", [])}

    def rollout(self, chat, *texts):
        folder = self.home / ".codex" / "sessions" / "2026" / "11" / "10"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "session_meta", "payload": {"id": chat}})] + [json.dumps({"output": t}) for t in texts]
        (folder / f"rollout-2026-11-10T00-00-00-{chat}.jsonl").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    def test_0_5_3_codex_chat_id_from_env_at_join(self):
        chat = str(uuid.uuid4())
        self.env(CODEX_THREAD_ID=chat)
        sid = self.join("codex")
        self.assertEqual([c["chat_id"] for c in self.chats(sid)], [chat])
        self.assertEqual(self.chats(sid)[0]["source"], "CODEX_THREAD_ID")
        w = self.wake()[sid]
        self.assertEqual(w["command"], f'codex queue --thread {chat} --message "Check AgentMesh for updates."')
        self.assertTrue(w["idle"])
        self.assertFalse(w["has_own_reminder"])
        self.nxt(session_id=sid, project_id="p")
        self.assertEqual(len(self.chats(sid)), 1)  # recorded once

    def test_0_5_3_codex_chat_id_from_its_session_file(self):
        sid = self.join("codex")
        self.assertEqual(self.chats(sid), [])  # the join output is not in the file yet
        chat = str(uuid.uuid4())
        self.rollout(chat, f'{{"session_id": "{sid}"}}')
        self.rollout(str(uuid.uuid4()), "another chat")
        self.nxt(session_id=sid, project_id="p")
        self.assertEqual([c["chat_id"] for c in self.chats(sid)], [chat])
        self.assertIn(sid, self.wake())

    def test_0_5_3_two_chats_with_the_id_record_nothing(self):
        sid = self.join("codex")
        self.rollout(str(uuid.uuid4()), sid)
        self.rollout(str(uuid.uuid4()), sid)
        self.nxt(session_id=sid, project_id="p")
        self.assertEqual(self.chats(sid), [])
        self.assertNotIn(sid, self.wake())

    def test_0_5_3_antigravity_conversation_file_and_address_never_token(self):
        self.env(ANTIGRAVITY_LS_ADDRESS="127.0.0.1:5387", ANTIGRAVITY_CSRF_TOKEN="secret-token-value")
        sid = self.join("antigravity")
        chat = str(uuid.uuid4())
        folder = self.home / ".gemini" / "antigravity-cli" / "conversations"
        folder.mkdir(parents=True)
        (folder / f"{chat}.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 50 + f"worker-join ok {sid} label".encode())
        self.nxt(session_id=sid, project_id="p")
        recorded = self.chats(sid)
        self.assertEqual([c["chat_id"] for c in recorded], [chat])
        self.assertEqual(recorded[0]["ls_address"], "127.0.0.1:5387")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM events WHERE payload_json LIKE '%secret-token-value%' OR result LIKE '%secret-token-value%'")[0][0], 0)
        w = self.wake()[sid]
        self.assertIn(f"agentapi send-message {chat}", w["command"])
        self.assertTrue(w["command"].startswith("ANTIGRAVITY_LS_ADDRESS=127.0.0.1:5387 "))
        self.assertTrue(w["has_own_reminder"])

    def test_0_5_3_claude_workers_and_closed_sessions_are_not_listed(self):
        self.env(CODEX_THREAD_ID=str(uuid.uuid4()))
        sid = self.join("codex")
        self.assertIn(sid, self.wake())
        self.sql("UPDATE sessions SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", sid)
        self.assertIn(sid, self.wake())  # an idle Codex worker's lease runs out; it must still be woken
        self.assertNotIn(self.worker, self.wake())  # claude-code worker
        self.call("session-close", session_id=sid)
        self.assertNotIn(sid, self.wake())
        self.assertNotIn("wake", self.nxt(session_id=self.worker, project_id="p"))  # workers never see it

    def test_0_5_4_worker_join_needs_only_the_project_id(self):
        first = execute(self.root, "worker-join", {"project_id": "p"})["data"]  # no op_id, no other field
        self.assertEqual(first["role"], "worker")
        self.assertIn(first["tool"], ("claude-code", "codex", "other"))
        self.assertEqual(first["label"], f"{first['tool']}-worker")
        second = execute(self.root, "worker-join", {"project_id": "p", "rulebook_version": "3.0"})["data"]
        self.assertEqual(second["label"], f"{first['tool']}-worker-2")  # live labels stay distinct
        self.assertEqual(self.sql("SELECT capabilities_json FROM sessions WHERE id=?", second["session_id"])[0][0], "[]")

    def test_0_5_4_worker_join_detects_codex_and_records_its_chat(self):
        chat = str(uuid.uuid4())
        self.env(CODEX_THREAD_ID=chat)
        joined = execute(self.root, "worker-join", {"project_id": "p"})["data"]
        self.assertEqual(joined["tool"], "codex")
        self.assertEqual([c["chat_id"] for c in self.chats(joined["session_id"])], [chat])

    def test_0_5_4_worker_join_still_checks_given_fields(self):
        for bad in ({"role": "leader-candidate"}, {"tool": "notepad"}, {"label": " "}, {"session_id": str(uuid.uuid4())}):
            with self.assertRaises(MeshError):
                execute(self.root, "worker-join", {"project_id": "p", **bad})
        joined = self.call("worker-join", project_id="p", tool="antigravity", label="AGY", capabilities=["js"], role="worker")
        self.assertEqual((joined["tool"], joined["label"]), ("antigravity", "AGY"))


class Release054(Base):
    """0.5.4: fixes from the mini-sheet feedback (feedback/mini-sheet/*.md)."""

    def out(self, process):
        return json.loads(process.stdout.decode("utf-8"))

    def test_0_5_4_first_input_error_carries_the_whole_format(self):
        error = self.out(self.cli("leader-join", {"project_id": "p"}))["error"]
        self.assertEqual(error["code"], "INVALID_INPUT")
        self.assertIn("rulebook_version", error["required"])
        self.assertIn("appointment_code", error["optional"])
        self.assertIn("rulebook_version", error["example"])
        self.assertIn("label", error["optional"])
        t = self.task()
        no_op = self.out(self.cli("task-claim", {"session_id": self.worker, "project_id": "p", "task_id": t["task_id"]}))["error"]
        self.assertEqual(no_op["message"], "op_id required")
        self.assertIn("op_id", no_op["required"])

    def test_0_5_4_help_works_with_root(self):
        env = dict(os.environ, PYTHONPATH=SRC)
        process = subprocess.run([sys.executable, "-m", "agentmesh.cli", "--root", str(self.root), "help", "task-claim"], capture_output=True, env=env, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(process.returncode, 0)
        self.assertIn(b"Required: op_id, session_id, project_id, task_id", process.stdout)

    def test_0_5_4_a_repeated_answered_question_stops_prompting_the_leader(self):
        t = self.task()
        a = self.started(t)
        point = dict(**a, summary="s", paths=[], op_id=str(uuid.uuid4()))
        execute(self.root, "checkpoint", {**point, "question": "no browser here", "blocker": True})
        self.assertIn("attempt-reply", self.compact(self.leader)["next_action"])
        self.call("attempt-reply", **self.lead, task_id=t["task_id"], body="Submit; I will check the browser.")
        execute(self.root, "checkpoint", {**point, "op_id": str(uuid.uuid4()), "question": "no browser here", "blocker": True, "reply_seen": True})
        self.assertNotIn("attempt-reply", self.compact(self.leader)["next_action"])
        execute(self.root, "checkpoint", {**point, "op_id": str(uuid.uuid4()), "question": "a new question", "blocker": True})
        self.assertIn("attempt-reply", self.compact(self.leader)["next_action"])

    def test_0_5_4_paths_may_be_relative_to_the_worktree(self):
        t = self.task()
        a = self.started(t)
        worktree = self.root / "worktrees" / "p" / (t["task_id"] + "-" + a["attempt_id"])
        (worktree / "js").mkdir(parents=True)
        (worktree / "js" / "a.js").write_text("x", encoding="utf-8")
        execute(self.root, "checkpoint", {**a, "summary": "s", "paths": ["js/a.js"]})
        saved = json.loads(self.sql("SELECT checkpoint_json FROM attempts WHERE id=?", a["attempt_id"])[0][0])
        self.assertEqual(saved["paths"], [str(worktree / "js" / "a.js")])
        for bad in ("../outside.txt", "missing.js"):
            with self.assertRaises(MeshError):
                execute(self.root, "checkpoint", {**a, "summary": "s", "paths": [bad]})


from unittest import mock  # noqa: E402
from agentmesh import coordination  # noqa: E402


class Release055(Base):
    """0.5.5: the CLI wakes Codex workers itself (codex queue) when they have news they were not woken for."""

    def setUp(self):
        super().setUp()
        self.woken, self.result = [], (True, "")
        patcher = mock.patch("agentmesh.coordination.run_wake", lambda chat: (self.woken.append(chat), self.result)[1])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.chat = str(uuid.uuid4())
        os.environ["CODEX_THREAD_ID"] = self.chat
        try:
            self.codex = self.call("worker-join", project_id="p", tool="codex", label="cx", capabilities=[], role="worker")["session_id"]
        finally:
            os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("AGENTMESH_NO_WAKE")
        self.addCleanup(os.environ.__setitem__, "AGENTMESH_NO_WAKE", "1")
        self.addCleanup(self.wait, coordination.WAKE_GAP_SECONDS, coordination.WAKE_RETRY_SECONDS)

    def nxt(self, sid=None):
        return execute(self.root, "next", {"session_id": sid or self.leader, "project_id": "p"})["data"]

    def send(self, body="hi", op_id=None):
        return execute(self.root, "message-send", {"op_id": op_id or str(uuid.uuid4()), "session_id": self.leader, "project_id": "p",
                                                   "to": self.codex, "task_id": None, "body": body})["data"]

    def wait(self, gap=0, retry=300):
        """Stand-in for time passing (events are append-only): shorten the wait after a wake and after a failed wake."""
        coordination.WAKE_GAP_SECONDS, coordination.WAKE_RETRY_SECONDS = gap, retry

    def wakes(self):
        return [json.loads(r[0]) for r in self.sql("SELECT payload_json FROM events WHERE action='auto-wake' ORDER BY seq")]

    def test_0_5_5_a_message_wakes_the_worker_once_then_waits(self):
        first = self.send("one")["message_ids"][0]
        self.assertEqual(self.woken, [self.chat])
        self.assertEqual(self.wakes()[0]["news"], [f"m:{first}"])
        self.assertTrue(self.wakes()[0]["ok"])
        self.assertEqual(self.sql("SELECT project_id FROM events WHERE action='auto-wake'")[0][0], None)  # does not move the project's seq
        self.send("two")
        self.nxt()
        self.assertEqual(len(self.woken), 1)  # 30 s gap
        self.wait()
        self.nxt()  # the leader's next is the retry path
        self.assertEqual(len(self.woken), 2)
        self.nxt()
        self.assertEqual(len(self.woken), 2)  # nothing new since the last wake

    def test_0_5_5_a_claimable_task_wakes_an_expired_idle_worker(self):
        self.sql("UPDATE sessions SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", self.codex)
        t = self.task(publish=False)
        self.assertEqual(self.woken, [])  # a draft is not news
        self.call("task-publish", **self.lead, task_id=t["task_id"], expected_version=t["version"])
        self.assertEqual(self.woken, [self.chat])
        self.assertEqual(self.wakes()[0]["news"], [f"t:{t['task_id']}:{t['version'] + 1}"])

    def test_0_5_5_a_failed_wake_is_retried_after_five_minutes(self):
        self.result = (False, "no rollout found")
        self.send()
        self.assertEqual((len(self.woken), self.wakes()[0]["ok"]), (1, False))
        self.wait()
        self.nxt()
        self.assertEqual(len(self.woken), 1)  # 5 minutes after a failure
        self.result = (True, "")
        self.wait(retry=0)
        self.nxt()
        self.assertEqual((len(self.woken), self.wakes()[-1]["ok"]), (2, True))  # the failure did not mark the news as delivered

    def test_0_5_5_no_wake_on_replay_from_the_worker_itself_from_codex_or_when_off(self):
        op = str(uuid.uuid4())
        self.send("same", op)
        self.wait()
        self.send("same", op)  # replayed op_id: nothing runs again
        self.assertEqual(len(self.woken), 1)
        self.wait(gap=30)
        self.send("new")
        self.assertEqual(len(self.woken), 1)
        self.wait()
        self.nxt(self.codex)  # the worker's own call never wakes itself
        self.assertEqual(len(self.woken), 1)
        os.environ["CODEX_THREAD_ID"] = str(uuid.uuid4())
        self.addCleanup(os.environ.pop, "CODEX_THREAD_ID", None)
        self.assertIn(self.codex, [w["session_id"] for w in self.nxt()["wake"]])  # a Codex leader skips auto-wake, so it is told to wake by hand
        self.assertEqual(len(self.woken), 1)
        os.environ.pop("CODEX_THREAD_ID")
        self.assertNotIn("wake", self.nxt())  # the CLI wakes Codex workers itself, so the leader is not asked to
        self.assertEqual(len(self.woken), 2)
        path = self.root / "config" / "settings.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**settings, "auto_wake": False}), encoding="utf-8")
        self.send("three")
        self.assertEqual(len(self.woken), 2)
        self.assertIn(self.codex, [w["session_id"] for w in self.nxt()["wake"]])  # switched off: the leader wakes it as in 0.5.3


class Release056(Base):
    """0.5.6: fixes from the pocket-sql leader's report (docs/IMPLEMENTATION-STATUS.md, open issues)."""

    def nxt(self, sid):
        return execute(self.root, "next", {"session_id": sid, "project_id": "p"})["data"]

    def test_0_5_6_role_joins_need_only_project_and_rulebook(self):
        joined = execute(self.root, "reviewer-join", {"project_id": "p", "rulebook_version": "3.0"})["data"]  # codes off here
        self.assertEqual(joined["role"], "reviewer")
        tool = self.sql("SELECT tool FROM sessions WHERE id=?", joined["session_id"])[0][0]
        self.assertEqual(joined["label"], f"{tool}-reviewer")
        path = self.root / "config" / "settings.json"
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "require_appointment_codes": True}), encoding="utf-8")
        with mock.patch("agentmesh.coordination.ask_user_approval", return_value="yes"):
            lead = execute(self.root, "leader-join", {"project_id": "p", "rulebook_version": "3.0"})["data"]
        self.assertEqual((lead["role"], lead["label"]), ("leader-candidate", f"{tool}-leader"))
        self.assertIn("generation", lead)
        self.assertEqual(json.loads(self.sql("SELECT payload_json FROM events WHERE session_id=? AND action='leader-join'", lead["session_id"])[0][0])["approval"], "popup click")
        for bad in ({}, {"rulebook_version": "2.9"}, {"rulebook_version": "3.0", "role": "worker"}, {"rulebook_version": "3.0", "tool": "notepad"}):
            with self.assertRaises(MeshError):
                execute(self.root, "leader-join", {"project_id": "p", **bad})

    def test_0_5_6_leader_next_lists_the_workers(self):
        workers = {w["session_id"]: w for w in self.nxt(self.leader)["workers"]}
        self.assertEqual(workers[self.worker], {"session_id": self.worker, "label": "W", "tool": "claude-code", "lease": "valid", "task_id": None})
        t = self.task()
        self.started(t)
        second = self.call("worker-join", project_id="p", tool="codex", label="cx", capabilities=[], role="worker")["session_id"]
        self.sql("UPDATE sessions SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", second)
        workers = {w["session_id"]: w for w in self.nxt(self.leader)["workers"]}
        self.assertEqual(workers[self.worker]["task_id"], t["task_id"])
        self.assertEqual(workers[second]["lease"], "expired")  # still listed: an idle Codex worker waits like this
        self.call("session-close", session_id=second)
        self.assertNotIn(second, {w["session_id"] for w in self.nxt(self.leader)["workers"]})
        self.assertNotIn("workers", self.nxt(self.worker))

    def test_0_5_6_plan_import_in_parts(self):
        item = {"title": "T", "description": "d", "category": "c", "priority": 50, "required_capabilities": [], "outputs": [], "acceptance": ["a"]}
        first = self.call("plan-import", **self.lead, publish=True, tasks=[{**item, "key": "t1", "depends_on": []}])["task_ids"]["t1"]
        second = self.call("plan-import", **self.lead, publish=True, tasks=[{**item, "key": "t2", "depends_on": [first]}, {**item, "key": "t3", "depends_on": ["t2", first]}])["task_ids"]
        self.assertEqual(self.sql("SELECT depends_on_id FROM dependencies WHERE task_id=?", second["t2"])[0][0], first)
        self.assertEqual(len(self.sql("SELECT 1 FROM dependencies WHERE task_id=?", second["t3"])), 2)
        with self.assertRaises(MeshError):
            self.call("plan-import", **self.lead, publish=True, tasks=[{**item, "key": "t4", "depends_on": [str(uuid.uuid4())]}])

    def test_0_5_6_leader_is_told_when_a_worker_idles_behind_a_dependency(self):
        item = {"title": "T", "description": "d", "category": "c", "priority": 50, "required_capabilities": [], "outputs": [], "acceptance": ["a"]}
        ids = self.call("plan-import", **self.lead, publish=True, tasks=[{**item, "key": "t1", "depends_on": []}, {**item, "key": "t2", "depends_on": ["t1"]}])["task_ids"]
        self.assertNotIn("is idle", self.compact(self.leader)["next_action"])  # t1 is claimable for the worker
        self.started({"task_id": ids["t1"]})
        self.assertNotIn("is idle", self.compact(self.leader)["next_action"])  # the only worker is busy
        second = self.call("worker-join", project_id="p", tool="claude-code", label="W2", capabilities=[], role="worker")["session_id"]
        hint = self.compact(self.leader)["next_action"]
        self.assertIn(f"Worker W2 ({second}) is idle", hint)  # t2 waits for t1: add independent work
        self.assertNotIn("is idle", self.compact(second)["next_action"])  # only the leader is told
        self.call("plan-import", **self.lead, publish=True, tasks=[{**item, "key": "t3", "depends_on": []}])
        self.assertNotIn("is idle", self.compact(self.leader)["next_action"])  # W2 can claim t3
        self.sql("UPDATE tasks SET state='completed' WHERE project_id='p'")
        self.assertNotIn("is idle", self.compact(self.leader)["next_action"])  # nothing unfinished


if __name__ == "__main__":
    unittest.main()
