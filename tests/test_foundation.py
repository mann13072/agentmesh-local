import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentmesh.cli import initialize


class FoundationTests(unittest.TestCase):
    def test_init_idempotent_and_schema_has_foreign_keys(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            result, status = initialize(root, {"root": str(root)})
            self.assertEqual(status, 0)
            self.assertEqual(result["data"]["schema_version"], 2)
            self.assertEqual(initialize(root, {"root": str(root)})[1], 0)
            db = sqlite3.connect(root / "runtime" / "coordination.db")
            try:
                db.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO memberships VALUES ('missing','missing','worker')")
            finally:
                db.close()

    def test_existing_configuration_conflicts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            self.assertEqual(initialize(root, {"root": str(root)})[1], 0)
            config = root / "config" / "settings.json"
            settings = json.loads(config.read_text(encoding="utf-8"))
            settings["version"] = 2
            config.write_text(json.dumps(settings), encoding="utf-8")
            self.assertEqual(initialize(root, {"root": str(root)})[1], 3)

    def test_version_one_database_with_missing_table_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory) / "mesh"
            self.assertEqual(initialize(root, {"root": str(root)})[1], 0)
            db = sqlite3.connect(root / "runtime" / "coordination.db")
            try:
                db.execute("DROP TABLE recommendations")
                db.commit()
            finally:
                db.close()
            response, status = initialize(root, {"root": str(root)})
            self.assertEqual(status, 3)
            self.assertEqual(response["error"]["code"], "CONFLICT")


if __name__ == "__main__":
    unittest.main()
