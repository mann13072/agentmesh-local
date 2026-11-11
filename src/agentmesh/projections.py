"""Rebuild human-readable views from committed coordination records."""
import json
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def projection_lock(root, wait_seconds=10.0):
    # A process-wide Windows file lock avoids concurrent replacements. Another sync holding it is normal: wait for it.
    import msvcrt
    import time
    lock_path = root / "runtime" / "projection.lock"
    with open(lock_path, "a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + wait_seconds
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    from .coordination import MeshError
                    raise MeshError("CONFLICT", "another sync or project-init is running", 3,
                                    {"next_action": "Another sync is running and covers your changes. Do not retry."})
                time.sleep(0.2)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def replace(path, content):
    """Atomic replace, skipped when the file already holds exactly this text (sync then touches only what changed)."""
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return
    except (OSError, UnicodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agentmesh-tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as out:
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def build(db, root):
    """Caller holds the projection lock and a consistent read transaction."""
    from .coordination import verify_managed, project_scope, project_vault, session_view, now
    def save(path, content):
        verify_managed(root, path)
        verify_managed(root, path.with_name(path.name + ".agentmesh-tmp"))
        replace(path, content)

    events = db.execute("SELECT * FROM events ORDER BY seq").fetchall()
    lines = []
    sessions = {}
    for row in events:
        item = {key: row[key] for key in ("seq", "event_id", "at", "session_id", "task_id", "action", "result")}
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        lines.append(line)
        if row["session_id"]:
            sessions.setdefault(row["session_id"], []).append(line)
    save(root / "runtime" / "shared-activity.jsonl", "".join(lines))
    for sid, entries in sessions.items():
        save(root / "runtime" / "sessions" / sid / "activity.jsonl", "".join(entries))
    generated = "<!-- AgentMesh generated; edits are overwritten -->\n"
    scope = project_scope(root)
    vault = project_vault(root)
    project_dir = vault / "01-Projects" / scope[0] if scope else None
    dashboard = project_dir / "AgentMesh" / "00-Dashboard" if scope else vault / "00-Dashboard"
    agents_dir = project_dir / "AgentMesh" / "02-Agents" if scope else vault / "02-Agents"
    projects = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
    as_of = now()
    agents = [session_view(row, as_of, db) for row in db.execute("SELECT * FROM sessions ORDER BY id").fetchall()]
    tasks = db.execute("SELECT * FROM tasks ORDER BY project_id,priority DESC,id").fetchall()
    save(dashboard / "Overview.md", generated + "# AgentMesh\n\nProjects: " + str(len(projects)) + "  \nSessions: " + str(len(agents)) + "  \nTasks: " + str(len(tasks)) + "\n")
    save(dashboard / "Active-Agents.md", generated + f"# Sessions\n\nLease status as of {as_of}; refresh with `sync`. A valid lease does not prove the agent is running.\n\n" + "".join(f"- {a['id']} · {a['tool']} · {a['effective_status']} · lease {'held (current leader)' if a.get('renewal') else a['lease_until']}\n" for a in agents))
    save(agents_dir / "Registry.md", generated + f"# Registry\n\nLease status as of {as_of}; refresh with `sync`.\n\n" + "".join(f"- [[Sessions/{a['id']}|{a['id']}]] · {a['tool']} · {a['effective_status']}\n" for a in agents))
    save(dashboard / "Task-Board.md", generated + "# Tasks\n\n" + "".join(f"- {t['id']} · {t['project_id']} · {t['state']} · {t['title'].replace(chr(10), ' ')}\n" for t in tasks))
    save(dashboard / "Issues.md", generated + "# Issues\n\n" + "".join(f"- {t['id']} · {t['state']}\n" for t in tasks if t['state'] in ('blocked', 'rejected')))
    for a in agents:
        body = f"---\nschema: 1\nid: {a['id']}\nstatus: {a['effective_status']}\nupdated_at: {a['last_seen_at']}\n---\n\n# Session {a['id']}\n\nLease valid until: {'held (current leader, no expiry)' if a.get('renewal') else a['lease_until']}\n\nTool: {a['tool']}\n\nCapabilities: {a['capabilities_json']}\n"
        save(agents_dir / "Sessions" / (a['id'] + ".md"), body.replace("\n---\n\n#", "\n---\n" + generated + "\n#", 1))
    for t in tasks:
        body = f"---\nschema: 1\nid: {t['id']}\nproject_id: {t['project_id']}\nstatus: {t['state']}\nupdated_at: {t['updated_at']}\n---\n\n# {t['title']}\n\n{t['description']}\n\n## Acceptance\n\n" + "".join(f"- {x}\n" for x in json.loads(t['acceptance_json']))
        save(vault / "01-Projects" / t['project_id'] / "Tasks" / (t['id'] + ".md"), body.replace("\n---\n\n#", "\n---\n" + generated + "\n#", 1))
    recommendations = db.execute("SELECT * FROM recommendations ORDER BY id").fetchall()
    for item in recommendations:
        body = generated + f"# Recommendation {item['id']}\n\nStatus: {item['status']}  \nProposed by: {item['session_id']}  \nKey: {item['dedupe_key']}\n\n{item['body']}\n"
        save(vault / "01-Projects" / item['project_id'] / "Recommendations" / (item['id'] + ".md"), body)
    reviews = db.execute("SELECT r.*,t.project_id,t.id AS task_id FROM reviews r JOIN attempts a ON a.id=r.attempt_id JOIN tasks t ON t.id=a.task_id ORDER BY r.id").fetchall()
    for item in reviews:
        body = generated + f"# Review {item['id']}\n\nTask: {item['task_id']}  \nOutcome: {item['outcome']}  \nReviewer: {item['reviewer_session_id']}\n\n{item['evidence']}\n"
        save(vault / "01-Projects" / item['project_id'] / "Reviews" / (item['id'] + ".md"), body)
    if scope:
        from .coordination import metrics, installation_base
        numbers = metrics(db, {"project_id": scope[0]})
        totals = numbers["totals"]
        rows = "".join(f"| {t['title'].replace('|', '/')[:50]} | {t['state']} | {t['attempts']} | {t['claim_to_submit_min']} | {t['submit_to_review_min']} | {t['review_to_integration_min']} |\n"
                       for t in numbers["tasks"])
        tools = "".join(f"- {tool}: {v['reviews']} reviews, revision rate {v['revision_rate']}\n" for tool, v in numbers["by_author_tool"].items())
        save(dashboard / "Metrics.md", generated + "# Metrics\n\n" + "".join(f"- {k}: {v}\n" for k, v in totals.items()) + "\n## By author tool\n\n" + (tools or "none yet\n")
             + "\n## Tasks (minutes)\n\n| Task | State | Attempts | Claim to submit | Submit to review | Review to integration |\n|---|---|---|---|---|---|\n" + rows)
        # Feedback sections arrive one file per agent (feedback-add); join them, but never overwrite a hand-written file.
        folder = installation_base(root) / "feedback" / scope[0]
        joined = installation_base(root) / "feedback" / (scope[0] + ".md")
        parts = sorted(folder.glob("*.md")) if folder.is_dir() else []
        if parts and (not joined.exists() or joined.read_text(encoding="utf-8").startswith(generated)):
            replace(joined, generated + f"# Feedback: {scope[0]}\n\n" + "\n".join(p.read_text(encoding="utf-8").rstrip() + "\n" for p in parts))
    return events[-1]["seq"] if events else 0
