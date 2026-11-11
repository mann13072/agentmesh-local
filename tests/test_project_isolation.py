import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

from agentmesh.cli import initialize, initialize_project
from agentmesh.coordination import execute, MeshError
from test_upgrade import legacy


class ProjectIsolationTests(unittest.TestCase):
    def test_project_init_via_cli(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            base = Path(directory) / "mesh"
            self.assertEqual(initialize(base, {"root": str(base)})[1], 0)
            (base / "config" / "per-project.json").write_text('{"version":1}', encoding="utf-8")
            request = {"op_id": str(uuid.uuid4()), "project_id": "cli-project", "title": "CLI Project"}
            result = subprocess.run([sys.executable, "-m", "agentmesh.cli", "--root", str(base), "project-init", "--input", "-"], input=json.dumps(request), text=True, encoding="utf-8", capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)["data"]
            self.assertTrue(Path(data["database"]).is_file())
            self.assertEqual(Path(data["root"]), base / "projects" / "cli-project")

    def test_projects_have_independent_databases_and_shared_vault(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            base = Path(directory) / "mesh"
            self.assertEqual(initialize(base, {"root": str(base)})[1], 0)
            (base / "config" / "per-project.json").write_text('{"version":1}', encoding="utf-8")
            with self.assertRaises(MeshError):
                execute(base, "session-register", {"op_id": str(uuid.uuid4()), "tool": "codex", "label": "legacy", "capabilities": []})
            roots = []
            for project in ("alpha", "beta"):
                data = {"op_id": str(uuid.uuid4()), "project_id": project, "title": project.title()}
                result, status = initialize_project(base, data)
                self.assertEqual(status, 0)
                self.assertEqual(initialize_project(base, data)[0], result)
                root = Path(result["data"]["root"])
                roots.append(root)
                legacy(root)
                sid = execute(root, "session-register", {"op_id": str(uuid.uuid4()), "tool": "codex", "label": project, "capabilities": []})["data"]["session_id"]
                execute(root, "membership-set", {"op_id": str(uuid.uuid4()), "session_id": sid, "project_id": project, "role": "leader-candidate", "enabled": True})
                generation = execute(root, "leader-acquire", {"op_id": str(uuid.uuid4()), "session_id": sid, "project_id": project})["data"]["generation"]
                execute(root, "task-create", {"op_id": str(uuid.uuid4()), "session_id": sid, "project_id": project, "leader_generation": generation, "title": project + " task", "description": "Work", "category": "test", "priority": 1, "required_capabilities": [], "outputs": [], "acceptance": ["Done"]})
                execute(root, "sync", {"op_id": str(uuid.uuid4())})
                self.assertTrue((base / "vault" / "01-Projects" / project / "AgentMesh" / "00-Dashboard" / "Overview.md").exists())
            self.assertNotEqual(roots[0], roots[1])
            for root in roots:
                conn = sqlite3.connect(root / "runtime" / "coordination.db")
                try:
                    self.assertEqual(conn.execute("SELECT id FROM projects").fetchall(), [(root.name,)])
                    self.assertEqual(conn.execute("SELECT title FROM tasks").fetchall(), [(root.name + " task",)])
                finally:
                    conn.close()
            self.assertTrue((base / "vault" / "01-Projects" / "alpha" / "Tasks").is_dir())
            self.assertTrue((base / "vault" / "01-Projects" / "beta" / "Tasks").is_dir())
            with self.assertRaises(MeshError):
                initialize_project(base, {"op_id": str(uuid.uuid4()), "project_id": "../outside", "title": "Bad"})


if __name__ == "__main__":
    unittest.main()
