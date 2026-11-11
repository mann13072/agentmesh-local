import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

os.environ["AGENTMESH_NO_POPUP"] = "1"  # never show a real approval popup during tests
os.environ["AGENTMESH_NO_WAKE"] = "1"  # never run a real codex queue during tests
os.environ["AGENTMESH_CHAT_HOME"] = str(Path(__file__).resolve().parent / "no-chat-home")  # never read real Codex/Antigravity chats
for _name in ("CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"):
    os.environ.pop(_name, None)


class CliTests(unittest.TestCase):
    def test_installed_cli_complete_flow(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

            def cli(command, data, expected=0):
                process = subprocess.run([sys.executable, "-m", "agentmesh.cli", "--root", str(root), command, "--input", "-"], input=json.dumps(data), text=True, encoding="utf-8", capture_output=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
                self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
                result = json.loads(process.stdout)
                return result.get("data", result.get("error"))

            def call(command, **values):
                return cli(command, {"op_id": str(uuid.uuid4()), **values})

            cli("init", {"root": str(root)})
            settings = json.loads((root / "config" / "settings.json").read_text(encoding="utf-8"))
            settings["require_appointment_codes"] = False
            (root / "config" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            call("project-create", project_id="demo", title="Demo")
            leader = call("session-register", tool="codex", label="leader", capabilities=[])["session_id"]
            join = {"op_id": str(uuid.uuid4()), "project_id": "demo", "tool": "claude-code", "label": "worker", "capabilities": ["research"], "role": "worker"}
            worker = cli("worker-join", join)["session_id"]
            self.assertEqual(cli("worker-join", join)["session_id"], worker)
            call("membership-set", session_id=leader, project_id="demo", role="leader-candidate", enabled=True)
            generation = call("leader-acquire", session_id=leader, project_id="demo")["generation"]
            leader_context = {"session_id": leader, "project_id": "demo", "leader_generation": generation}
            task = call("task-create", **leader_context, title="Research", description="Study the problem", category="research", priority=10, required_capabilities=["research"], outputs=[], acceptance=["Evidence provided"])
            call("task-publish", **leader_context, task_id=task["task_id"], expected_version=task["version"])
            compact = cli("status", {"session_id": worker, "project_id": "demo", "view": "compact"})
            self.assertNotIn("description", compact["tasks"][0])
            self.assertEqual(cli("task-detail", {"project_id": "demo", "task_id": task["task_id"]})["task"]["description"], "Study the problem")
            claim = call("task-claim", session_id=worker, project_id="demo", task_id=task["task_id"])
            attempt = {"session_id": worker, "attempt_id": claim["attempt_id"], "generation": claim["generation"]}
            call("task-start", **attempt)
            Path(claim["worktree"]).mkdir(parents=True)
            output = Path(claim["worktree"]) / "result.md"
            output.write_text("Result\n", encoding="utf-8")
            call("checkpoint", **attempt, summary="Drafted", paths=[str(output)])
            call("task-submit", **attempt, manifest={"paths": [str(output)], "summary": "Finished", "evidence": "Reviewed sources"})
            call("review", session_id=leader, attempt_id=claim["attempt_id"], outcome="accepted", evidence="Evidence checked", leader_generation=generation)
            call("activity-log", session_id=worker, project_id="demo", task_id=task["task_id"], action="completed\nwork", result="done")
            call("sync")
            status = cli("status", {"session_id": leader, "project_id": "demo"})
            self.assertEqual(status["tasks"][0]["state"], "completed")
            self.assertTrue((root / "vault" / "00-Dashboard" / "Task-Board.md").is_file())
            lines = (root / "runtime" / "sessions" / worker / "activity.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(json.loads(line)["action"] == "completed\nwork" for line in lines))
            error = cli("task-claim", {"op_id": str(uuid.uuid4()), "session_id": worker, "project_id": "demo", "task_id": task["task_id"]}, expected=3)
            self.assertEqual(error["code"], "CONFLICT")
            self.assertEqual(error["blocker"], "not_ready")
            self.assertTrue(error["next_action"])
            process = subprocess.run([sys.executable, "-m", "agentmesh.cli", "--help"], text=True, capture_output=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("worker-join", process.stdout)


if __name__ == "__main__":
    unittest.main()
