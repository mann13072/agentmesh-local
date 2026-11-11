"""Operator commands: run by the user in a terminal (or by the scheduled watchdog), never by an agent.
They print plain text for a person, not JSON."""
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .coordination import (MeshError, execute, load_settings, migrate, create_appointment, project_scope, host_identity, host_state,
                           lease_view, now, new_id, canonical, worktree_info, project_git, rulebook_path, installation_base,
                           leader_view, SCHEMA_VERSION, HELD)

OPERATOR = ("appoint", "halt", "resume", "gc", "rulebook-seal", "board", "doctor", "watchdog-all", "ancestors")
TERMINAL_ONLY = ("appoint", "halt", "resume", "gc", "rulebook-seal")
USAGE = """Operator commands (the user runs them in their own terminal window):
  agentmesh --root <project-root> appoint leader|reviewer|integrator   one-time code to paste into the chosen chat
  agentmesh --root <project-root> halt "<reason>"                      stop all agent mutations in this project
  agentmesh --root <project-root> resume
  agentmesh --root <project-root> board                                 who is doing what, for a person
  agentmesh --root <project-root> gc [--older-than-days 14] [--apply]  remove old merged or abandoned worktrees
  agentmesh --root <installation-root> rulebook-seal                    record the rulebook hash; joins warn if it changes
  agentmesh --root <installation-root> watchdog-all                     the scheduled watchdog (recover, sync, backup, Attention.md)
  agentmesh doctor                                                      health check of the installation
  agentmesh ancestors                                                   parent processes of this call (to identify a host program)
"""


class OperatorError(Exception):
    pass


def out(text=""):
    if sys.stdout is None:  # pythonw.exe (the scheduled watchdog) has no console
        return
    data = (text + "\n").encode("utf-8")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text + "\n")
    else:
        sys.stdout.flush()
        buffer.write(data)
        buffer.flush()


def is_terminal():
    """True only for a person at a console: stdin and stdout are a terminal and no agent program is an ancestor."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty() and host_identity() is None
    except (OSError, ValueError, AttributeError):
        return False


def connect(root):
    settings = load_settings(root)
    db = sqlite3.connect(settings["db"], timeout=settings["busy_timeout_ms"] / 1000, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    migrate(db, root)
    return db, settings


def the_project(db, root):
    scope = project_scope(root)
    if scope:
        return scope[0]
    rows = db.execute("SELECT id FROM projects").fetchall()
    if len(rows) != 1:
        raise OperatorError("this root holds no single project; use --root <installation root>\\projects\\<project-id>")
    return rows[0][0]


def log_event(db, project, action, result, payload):
    db.execute("INSERT INTO events(event_id,at,session_id,project_id,task_id,action,result,payload_json) VALUES(?,?,?,?,?,?,?,?)",
               (new_id(), now(), None, project, payload.pop("task_id", None), action, result, canonical(payload)))


# ---------------------------------------------------------------- appoint

def appoint(root, args):
    if len(args) != 1 or args[0] not in ("leader", "reviewer", "integrator"):
        raise OperatorError("usage: agentmesh --root <project-root> appoint leader|reviewer|integrator")
    db, _ = connect(root)
    try:
        project = the_project(db, root)
        db.execute("BEGIN IMMEDIATE")
        code, expires = create_appointment(db, project, args[0])
        log_event(db, project, "appoint", "success", {"role": args[0], "expires_at": expires})
        db.execute("COMMIT")
    finally:
        db.close()
    join = {"leader": "leader-join", "reviewer": "reviewer-join", "integrator": "integrator-join"}[args[0]]
    out(f"Appointment code for {args[0]} of project {project}: {code}")
    out(f"It works once and expires at {expires} (15 minutes).")
    out("")
    out("Paste this into the chat you chose:")
    out(f'  You are the {args[0]} of AgentMesh project {project}. Join with {join} using appointment code {code}.')
    return 0


# ---------------------------------------------------------------- halt / resume

def halt(root, args):
    reason = " ".join(args).strip()
    if not reason:
        raise OperatorError('usage: agentmesh --root <project-root> halt "<reason>"')
    db, _ = connect(root)
    try:
        project = the_project(db, root)
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR REPLACE INTO project_flags VALUES(?,?,?,?)", (project, "halt", reason, now()))
        log_event(db, project, "halt", "success", {"reason": reason})
        db.execute("COMMIT")
    finally:
        db.close()
    out(f"Project {project} is halted. Agents can still checkpoint, release and close; every other change is refused.")
    out("Undo with: agentmesh --root <project-root> resume")
    return 0


def resume(root, args):
    db, _ = connect(root)
    try:
        project = the_project(db, root)
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM project_flags WHERE project_id=? AND name='halt'", (project,))
        log_event(db, project, "resume", "success", {})
        db.execute("COMMIT")
    finally:
        db.close()
    out(f"Project {project} resumed.")
    return 0


# ---------------------------------------------------------------- rulebook-seal

def rulebook_seal(root, args):
    base = installation_base(root)
    book = rulebook_path(root)
    digest = hashlib.sha256(book.read_bytes()).hexdigest()
    seal = base / "config" / "rulebook.sha256"
    seal.parent.mkdir(parents=True, exist_ok=True)
    seal.write_text(digest + "\n", encoding="utf-8")
    out(f"Sealed {book}")
    out(f"sha256 {digest} -> {seal}")
    return 0


# ---------------------------------------------------------------- gc

def gc(root, args):
    days, apply = 14, False
    rest = list(args)
    while rest:
        item = rest.pop(0)
        if item == "--apply":
            apply = True
        elif item == "--older-than-days" and rest and rest[0].isdigit():
            days = int(rest.pop(0))
        else:
            raise OperatorError("usage: agentmesh --root <project-root> gc [--older-than-days N] [--apply]")
    from . import gitops
    db, _ = connect(root)
    try:
        project = the_project(db, root)
        pg = project_git(db, project)
        if pg is None:
            out("This project has no project-git record, so worktrees are not managed by the CLI. Nothing to do.")
            return 0
        limit = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        rows = db.execute("SELECT a.id,a.task_id,a.state,a.updated_at,(SELECT 1 FROM integrations i WHERE i.task_id=a.task_id) AS merged "
                          "FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE t.project_id=? AND a.state NOT IN ('claimed','in_progress','submitted')",
                          (project,)).fetchall()
    finally:
        db.close()
    found = 0
    for row in rows:
        if not (row["merged"] or (row["state"] in ("abandoned", "revision", "rejected") and row["updated_at"] < limit)):
            continue
        path = Path(worktree_info(root, project, row["task_id"], row["id"])["worktree"])
        if not path.is_dir():
            continue
        try:
            if gitops.dirty(path):
                out(f"skip (uncommitted changes): {path}")
                continue
            found += 1
            if apply:
                gitops.remove_worktree(pg["repository"], path)
                out(f"removed worktree (branch kept): {path}")
            else:
                out(f"would remove (branch kept): {path}")
        except gitops.GitError as exc:
            out(f"skip ({exc}): {path}")
    out(f"{found} worktree(s) {'removed' if apply else 'to remove; run again with --apply'}.")
    return 0


# ---------------------------------------------------------------- board

def board(root, args):
    db, _ = connect(root)
    try:
        project = the_project(db, root)
        at = now()
        l = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (project,)).fetchone()
        view = leader_view(db, l, at)
        out(f"AgentMesh {project}   {at}")
        halt_row = db.execute("SELECT value FROM project_flags WHERE project_id=? AND name='halt'", (project,)).fetchone()
        if halt_row:
            out(f"HALTED: {halt_row[0]}")
        if view:
            owner = db.execute("SELECT label,tool FROM sessions WHERE id=?", (view["session_id"],)).fetchone()
            out(f"Leader: {owner['label'] if owner else '?'} ({owner['tool'] if owner else '?'}), generation {view['generation']}, {view['effective_status']}")
        else:
            out("Leader: none")
        out("")
        out("Workers:")
        for s in db.execute("SELECT DISTINCT s.* FROM sessions s JOIN memberships m ON m.session_id=s.id WHERE m.project_id=? AND m.role='worker' AND s.status!='closed' ORDER BY s.label", (project,)).fetchall():
            if lease_view(s["lease_until"], at)["lease_state"] != "valid":
                continue
            a = db.execute("SELECT a.*,t.title FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE a.session_id=? AND a.state IN ('claimed','in_progress')", (s["id"],)).fetchone()
            if a is None:
                out(f"  {s['label']:<18} idle")
                continue
            saved = json.loads(a["checkpoint_json"])
            since = minutes_since(saved.get("at") or a["updated_at"], at)
            question = f"  QUESTION: {saved['question']}" if saved.get("question") else ""
            out(f"  {s['label']:<18} {a['title'][:40]:<40} last checkpoint {since} min ago{question}")
        out("")
        counts = dict(db.execute("SELECT state,COUNT(*) FROM tasks WHERE project_id=? GROUP BY state", (project,)).fetchall())
        out("Tasks: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "Tasks: none")
    finally:
        db.close()
    attention = attention_file(root, project)
    if attention.is_file():
        lines = [x for x in attention.read_text(encoding="utf-8").splitlines() if x.startswith("- ")]
        if lines:
            out("")
            out("Attention (from the last watchdog run):")
            for x in lines:
                out("  " + x[2:])
    return 0


def minutes_since(stamp, at):
    try:
        return int((datetime.fromisoformat(at.replace("Z", "+00:00")) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() // 60)
    except (AttributeError, TypeError, ValueError):
        return "?"


# ---------------------------------------------------------------- watchdog

def attention_file(root, project):
    scope = project_scope(root)
    base = installation_base(root)
    return (base / "vault" / "01-Projects" / project / "AgentMesh" / "00-Dashboard" / "Attention.md") if scope else (root / "vault" / "00-Dashboard" / "Attention.md")


def auto_recover(db, root, settings, project):
    """Recover attempts whose lease ended long ago and whose host process is gone or was never seen."""
    at = now()
    done = []
    rows = db.execute("SELECT a.*,h.pid,h.started FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id LEFT JOIN session_hosts h ON h.session_id=a.session_id "
                      "WHERE t.project_id=? AND a.state IN ('claimed','in_progress')", (project,)).fetchall()
    for a in rows:
        view = lease_view(a["lease_until"], at)
        if view["lease_state"] != "expired":
            continue
        overdue = (datetime.fromisoformat(at.replace("Z", "+00:00")) - datetime.fromisoformat(a["lease_until"].replace("Z", "+00:00"))).total_seconds()
        if overdue < settings["auto_recover_after_seconds"]:
            continue
        if a["pid"] is not None and host_state({"pid": a["pid"], "started": a["started"]}) == "alive":
            continue  # the worker's program still runs: leave it to the leader
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("SELECT current_attempt_id FROM tasks WHERE id=?", (a["task_id"],)).fetchone()[0]
            if current != a["id"]:
                db.execute("ROLLBACK")
                continue
            info = worktree_info(root, project, a["task_id"], a["id"])
            db.execute("UPDATE attempts SET state='abandoned',updated_at=? WHERE id=?", (at, a["id"]))
            db.execute("UPDATE tasks SET state='ready',current_attempt_id=NULL,version=version+1,updated_at=? WHERE id=?", (at, a["task_id"]))
            db.execute("UPDATE sessions SET status='available' WHERE id=? AND status='busy'", (a["session_id"],))
            log_event(db, project, "recover", "success", {"task_id": a["task_id"], "reason": f"watchdog: lease ended {int(overdue // 60)} min ago and the worker's program is not running",
                                                         "preserved_checkpoint": json.loads(a["checkpoint_json"]), "preserved_worktree": info["worktree"], "preserved_branch": info["branch"]})
            db.execute("COMMIT")
            done.append(a["task_id"])
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return done


def attention(db, project, at):
    items = []
    leader = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (project,)).fetchone()
    view = leader_view(db, leader, at)
    if view is None or view["effective_status"] != "active":
        items.append(f"Leader: {view['effective_status'] if view else 'none'}. Reviews, merges and answers wait until a leader is back.")
    for t in db.execute("SELECT id,title,updated_at FROM tasks WHERE project_id=? AND state='submitted'", (project,)).fetchall():
        waited = minutes_since(t["updated_at"], at)
        if isinstance(waited, int) and waited >= 20:
            items.append(f"Submitted {waited} min ago, not reviewed: {t['title']} ({t['id']})")
    for a in db.execute("SELECT a.*,t.title FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE t.project_id=? AND a.state IN ('claimed','in_progress')", (project,)).fetchall():
        saved = json.loads(a["checkpoint_json"])
        if saved.get("question") or saved.get("blocker"):
            reply = saved.get("leader_reply") or {}
            if (reply.get("at") or "") < (saved.get("at") or ""):
                items.append(f"Unanswered question on {a['title']}: {saved.get('question') or 'blocked'}")
        quiet = minutes_since(saved.get("at") or a["created_at"], at)
        if isinstance(quiet, int) and quiet >= 30:
            items.append(f"No checkpoint for {quiet} min: {a['title']} ({a['task_id']})")
    for t in db.execute("SELECT id,title FROM tasks WHERE project_id=? AND state='blocked'", (project,)).fetchall():
        items.append(f"Blocked: {t['title']} ({t['id']})")
    return items


def watchdog_project(root):
    from .projections import replace
    db, settings = connect(root)
    state_file = root / "runtime" / "watchdog.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    except (OSError, ValueError):
        state = {}
    summary = {"at": now(), "recovered": [], "synced": False, "backup": None}
    try:
        project = the_project(db, root)
        summary["recovered"] = auto_recover(db, root, settings, project)
        seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        items = attention(db, project, now())
    finally:
        db.close()
    if seq != state.get("seq"):
        execute(root, "sync", {"op_id": new_id()})
        summary["synced"] = True
    made = [x for x in state.get("backups_made", []) if Path(x).is_file()]
    newest = max([p.stat().st_mtime for p in (root / "backups").glob("*.db")], default=0)
    if datetime.now().timestamp() - newest > 86400:
        result = execute(root, "backup", {"op_id": new_id()})
        made.append(result["data"]["path"])
        summary["backup"] = result["data"]["path"]
    # Prune only backups this watchdog made itself, beyond the newest backup_keep.
    while len(made) > settings["backup_keep"]:
        old = Path(made.pop(0))
        if old.parent == (root / "backups") and re.fullmatch(r"[0-9a-f-]{36}\.db", old.name):
            old.unlink(missing_ok=True)
    body = "<!-- AgentMesh generated by the watchdog; edits are overwritten -->\n# Attention\n\nAs of " + summary["at"] + ".\n\n"
    body += "".join(f"- {x}\n" for x in items) if items else "Nothing needs you.\n"
    target = attention_file(root, project)
    target.parent.mkdir(parents=True, exist_ok=True)
    replace(target, body)
    summary["attention"] = items
    state.update(seq=seq, backups_made=made, last=summary)
    replace(state_file, json.dumps(state, indent=2) + "\n")
    return project, summary


def watchdog_all(root, args):
    base = installation_base(root)
    projects = sorted(p for p in (base / "projects").iterdir() if (p / "config" / "settings.json").is_file()) if (base / "projects").is_dir() else []
    status = 0
    log = base / "runtime" / "watchdog.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for p in projects:
        try:
            project, summary = watchdog_project(p)
            lines.append(f"{summary['at']} {project}: recovered {len(summary['recovered'])}, synced {summary['synced']}, backup {bool(summary['backup'])}, attention {len(summary['attention'])}")
        except Exception as exc:  # one broken project must not stop the others
            status = 1
            lines.append(f"{now()} {p.name}: ERROR {type(exc).__name__}: {exc}")
    try:  # the log first: under pythonw there is nowhere else to see what happened
        old = log.read_text(encoding="utf-8").splitlines()[-2000:] if log.is_file() else []
        log.write_text("\n".join(old + lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    for x in lines:
        out(x)
    return status


# ---------------------------------------------------------------- doctor / ancestors

def doctor(root, args):
    base = installation_base(root) if root else Path(sys.prefix).resolve().parent
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        out(f"[{'ok' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    check("python", sys.version_info >= (3, 12), sys.version.split()[0])
    site = Path(__file__).resolve()
    check("frozen install (agents run a fixed copy)", "site-packages" in site.parts, str(site.parent))
    check("cli version", True, __version__)
    try:
        "→".encode("utf-8")
        check("UTF-8 output", True)
    except UnicodeError:
        check("UTF-8 output", False)
    from . import gitops
    try:
        check("git", True, gitops.git(base, "--version")[1])
    except gitops.GitError as exc:
        check("git", False, str(exc))
    book = base / "vault" / "04-Skills" / "agent-mesh" / "SKILL.md"
    seal = base / "config" / "rulebook.sha256"
    if book.is_file() and seal.is_file():
        check("rulebook matches the seal", hashlib.sha256(book.read_bytes()).hexdigest() == seal.read_text(encoding="utf-8").strip())
    else:
        check("rulebook sealed", False, "run rulebook-seal")
    home = Path.home()
    for name, host in (("Claude Code", home / ".claude" / "skills" / "agent-mesh" / "SKILL.md"), ("Codex", home / ".codex" / "skills" / "agent-mesh" / "SKILL.md"),
                       ("Antigravity", home / ".gemini" / "config" / "skills" / "agent-mesh" / "SKILL.md")):
        text = host.read_text(encoding="utf-8", errors="replace") if host.is_file() else ""
        check(f"host skill {name}", str(book).lower() in text.lower(), "names the rulebook" if text else f"missing: {host}")
    try:
        import subprocess
        found = subprocess.run(["schtasks", "/Query", "/TN", "AgentMesh Watchdog"], capture_output=True, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
        check("scheduled watchdog", found, "" if found else "not registered (see IMPLEMENTATION-STATUS.md)")
    except OSError as exc:
        check("scheduled watchdog", False, str(exc))
    for p in sorted((base / "projects").iterdir()) if (base / "projects").is_dir() else []:
        if not (p / "config" / "settings.json").is_file():
            continue
        try:
            settings = load_settings(p)
            db = sqlite3.connect(f"file:{Path(settings['db']).as_posix()}?mode=ro", uri=True)
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                db.close()
            backups = sorted((p / "backups").glob("*.db"), key=lambda x: x.stat().st_mtime)
            age = (datetime.now().timestamp() - backups[-1].stat().st_mtime) / 3600 if backups else None
            check(f"project {p.name}", mode == "wal" and version in (1, SCHEMA_VERSION),
                  f"schema {version}{' (migrates on next use)' if version == 1 else ''}, {mode}, newest backup "
                  + (f"{age:.0f} h old" if age is not None else "none"))
            old = [w for w in (p / "worktrees").glob("*/*") if w.is_dir() and (datetime.now().timestamp() - w.stat().st_mtime) > 14 * 86400]
            if old:
                out(f"       {len(old)} worktree(s) older than 14 days (see gc)")
        except Exception as exc:
            check(f"project {p.name}", False, f"{type(exc).__name__}: {exc}")
    return 0 if all(checks) else 1


def ancestors(root, args):
    from .coordination import _win_processes
    table = _win_processes() or {}
    pid, seen = os.getpid(), set()
    while pid in table and pid not in seen and len(seen) < 64:
        seen.add(pid)
        parent, exe = table[pid]
        out(f"{pid:>7}  {exe}")
        pid = parent
    return 0


HANDLERS = {"appoint": appoint, "halt": halt, "resume": resume, "gc": gc, "rulebook-seal": rulebook_seal, "board": board,
            "doctor": doctor, "watchdog-all": watchdog_all, "ancestors": ancestors}


def run(command, root, args, terminal=None):
    """Entry point from cli.main. Returns the exit status."""
    terminal = is_terminal() if terminal is None else terminal
    if command in TERMINAL_ONLY and not terminal:
        out(f"{command} is run by the user in their own terminal window, not by an agent and not through an agent's shell.")
        return 3
    if command not in ("doctor", "ancestors") and root is None:
        out(USAGE)
        return 2
    try:
        if root is not None:
            root = Path(root)
            if not root.is_absolute() or ".." in root.parts:
                raise OperatorError("--root must be an absolute path without ..")
        return HANDLERS[command](root, args)
    except (OperatorError, MeshError, OSError, sqlite3.Error, ValueError) as exc:
        out(f"error: {exc}")
        details = getattr(exc, "details", None) or {}
        if details.get("next_action"):
            out(details["next_action"])
        return 2 if isinstance(exc, OperatorError) else 3
