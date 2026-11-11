"""AgentMesh local coordination command line."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import re
import uuid
import hashlib
from . import __version__
from .coordination import MeshError, execute, load_settings, verify_managed, project_vault, canonical, now, SCHEMA_VERSION, schema_text, v1_schema_text


def emit(text):
    """Write UTF-8 whatever the console code page is (cp1252 cannot encode many characters agents use)."""
    if sys.stdout is None:  # pythonw.exe has no console
        return
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def schema_matches(connection, schema):
    """Reject databases that claim v1 but have a different v1 structure."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(schema)
        query = ("SELECT type, name, tbl_name, sql FROM sqlite_master "
                 "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
        return connection.execute(query).fetchall() == reference.execute(query).fetchall()
    finally:
        reference.close()


def fail(code, message, status, details=None):
    error = {"code": code, "message": message}
    error.update(details or {})
    if "next_action" not in error:
        error["next_action"] = DEFAULT_NEXT.get(code, "Run `agentmesh --help`.")
    return {"ok": False, "error": error}, status


DEFAULT_NEXT = {
    "INVALID_INPUT": "Run `agentmesh help <command>` for the exact fields and an example; fix the input; use a new op_id if the input changed.",
    "CONFLICT": "Run status with view compact to see current state before retrying; do not retry the same failing request in a loop.",
    "DATABASE_UNAVAILABLE": "Stop coordination mutations and keep your files. Retry once later with the identical input and the same op_id.",
    "IO_ERROR": "Check the path and your filesystem permissions; report the exact error if access is denied. Do not work around it.",
    "NOT_IMPLEMENTED": "Run `agentmesh --help` for the list of commands.",
    "PROJECT_NOT_FOUND": "Use the project ID (folder name under projects), not the display name; ask the user if unsure.",
    "INTERNAL_ERROR": "This is a CLI defect. Stop, keep your files, and report the exact message to the user.",
    "GIT_ERROR": "Report the exact Git message to the user; do not work around it with your own git commands.",
}

# The installation folder holds .venv; this works for the editable and the frozen install alike.
INSTALL_ROOT = Path(sys.prefix).resolve().parent
U = "<uuid4>"
RB = "3.0"
# name: (who may call, example input, returns). The example keys are the exact fields; OPTIONAL names the optional ones.
COMMANDS = {
    "init": ("operator, once per root (no op_id)", {"root": "<absolute root>"}, "root, schema_version"),
    "project-init": ("operator; --root is the INSTALLATION root", {"op_id": U, "project_id": "<project-id>", "title": "<title>"}, "project_id, root (use for all later commands), database, vault"),
    "project-create": ("operator (project-init already does this)", {"op_id": U, "project_id": "<project-id>", "title": "<title>"}, "project_id"),
    "worker-join": ("a session the user appointed as worker (no code needed)", {"project_id": "<project-id>", "rulebook_version": RB}, "session_id, role, tool, lease_until, label, warnings, status, commands, rules"),
    "leader-join": ("the chat the user asked to lead; the user clicks Yes on a popup; replaces any current leader", {"project_id": "<project-id>", "rulebook_version": RB}, "session_id, generation (your leader_generation), last_handoff, warnings, status, commands, rules"),
    "reviewer-join": ("the chat the user asked to review; the user clicks Yes on a popup", {"project_id": "<project-id>", "rulebook_version": RB}, "session_id, lease_until, status, commands, rules"),
    "integrator-join": ("the chat the user asked to integrate; the user clicks Yes on a popup", {"project_id": "<project-id>", "rulebook_version": RB}, "session_id, lease_until, status, commands, rules"),
    "next": ("any member, on every reminder; renews your leases; no op_id", {"session_id": U, "project_id": "<project-id>", "since": 0}, "changed, seq, next_action, call (template for that command), inbox, my_active_attempt, leader_alert, halt, tasks; leader also workers, wake"),
    "whoami": ("any chat that lost its IDs; read-only", {"project_id": "<project-id>"}, "session_id, roles, leader_generation, active attempt (matched by your host process)"),
    "session-register": ("legacy; only with require_appointment_codes false", {"op_id": U, "tool": "claude-code|codex|antigravity|opencode|other", "label": "<short label>", "capabilities": []}, "session_id, lease_until"),
    "membership-set": ("legacy; trusted roles need appointment codes", {"op_id": U, "session_id": U, "project_id": "<project-id>", "role": "worker|reviewer|leader-candidate|integrator", "enabled": True}, "session_id, role, enabled"),
    "session-close": ("session owner, only when the conversation is ending", {"op_id": U, "session_id": U}, "session_id (the leader slot you hold is released too)"),
    "leader-acquire": ("the current leader after a resume (or legacy without codes)", {"op_id": U, "session_id": U, "project_id": "<project-id>", "takeover": True, "reason": "<the user's instruction>"}, "generation, lease_until (held), host, last_handoff"),
    "leader-renew": ("current leader once after a resumed conversation; no op_id", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1}, "generation, lease_until, session_lease_until, note"),
    "leader-release": ("current leader giving up the role but keeping the session", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1}, "generation, lease_until, session_lease_until"),
    "status": ("any member; read-only, renews nothing (prefer next)", {"session_id": U, "project_id": "<project-id>", "view": "compact", "include_done": False}, "as_of, session, leader, project_status, session_blockers, my_active_attempt, counts, leader_alert, tasks[claimable, blockers], next_action"),
    "list": ("read-only; first page cursor null", {"project_id": "<project-id>", "kind": "tasks|sessions|events|reviews|recommendations|messages", "cursor": None, "limit": 50, "view": "compact"}, "items, next_cursor, as_of (view only for kind tasks)"),
    "task-detail": ("read-only; session_id adds eligibility", {"project_id": "<project-id>", "task_id": U, "session_id": U}, "task, dependencies[integrated], git, attempts (head_sha, diff_command), latest_review, integration, eligibility"),
    "metrics": ("read-only", {"project_id": "<project-id>"}, "per task minutes claim->submit->review->integration, revision rate per tool, recoveries, messages"),
    "project-git": ("current leader, once per project before tasks are claimed", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "repository": "<absolute repository folder>", "base_branch": "main"}, "repository, base_branch, base_sha"),
    "plan-import": ("current leader; a whole plan in one call", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "publish": True, "tasks": [{"key": "t1", "title": "<title>", "description": "<brief>", "category": "<category>", "priority": 50, "required_capabilities": [], "outputs": ["<output>"], "acceptance": ["<criterion>"], "depends_on": []}]}, "task_ids (key -> task_id), published"),
    "task-create": ("current leader", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "title": "<title>", "description": "<brief>", "category": "<category>", "priority": 50, "required_capabilities": [], "outputs": ["<output>"], "acceptance": ["<criterion>"]}, "task_id, version"),
    "task-spec": ("current leader; proposed tasks, or blocked tasks after attempt-release", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "expected_version": 1, "title": "<title>", "description": "<brief>", "category": "<category>", "priority": 50, "required_capabilities": [], "outputs": ["<output>"], "acceptance": ["<criterion>"]}, "task_id, version"),
    "task-depend": ("current leader; proposed tasks only", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "depends_on_id": U}, "task_id, version"),
    "task-publish": ("current leader", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "expected_version": 1}, "task_id, version, warnings"),
    "task-cancel": ("current leader; proposed, ready, blocked or rejected task with no live dependents", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "reason": "<reason>"}, "task_id, state"),
    "task-reopen": ("current leader; a rejected task back to proposed", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "reason": "<reason>"}, "task_id, state, version"),
    "task-reserve": ("current leader", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "worker_session_id": U}, "reservation_until"),
    "task-claim": ("worker; start true also starts it and returns the brief", {"op_id": U, "session_id": U, "project_id": "<project-id>", "task_id": U, "start": True}, "attempt_id, generation, lease_until, worktree, branch, git (worktree created), task"),
    "task-start": ("claimant (not needed with task-claim start true)", {"op_id": U, "session_id": U, "attempt_id": U, "generation": 1}, "task_id, state"),
    "heartbeat": ("session owner; next does this for you; attempt_id null renews only the session", {"op_id": U, "session_id": U, "attempt_id": U, "generation": 1}, "session_lease_until, task_lease_until"),
    "checkpoint": ("claimant; op_id optional; paths inside the attempt worktree", {"op_id": U, "session_id": U, "attempt_id": U, "generation": 1, "summary": "<progress>", "paths": ["<absolute path>"], "note": "<status for the leader>", "question": "<question for the leader>", "blocker": False, "reply_seen": True}, "task_id"),
    "attempt-release": ("claimant who cannot continue; op_id optional", {"op_id": U, "session_id": U, "attempt_id": U, "generation": 1, "reason": "<why you stop>", "blocker": True}, "task_id, task_state, released_attempt_id, preserved_worktree, preserved_branch"),
    "task-submit": ("claimant of an in_progress attempt; commit everything first; op_id optional", {"op_id": U, "session_id": U, "attempt_id": U, "generation": 1, "manifest": {"paths": ["<absolute path>"], "summary": "<what was delivered>", "evidence": "<tests run and results>"}}, "task_id, state, head_sha (the commit under review)"),
    "review": ("reviewer member or current leader (add leader_generation); never the author", {"op_id": U, "session_id": U, "attempt_id": U, "outcome": "accepted|revision|rejected|blocked", "evidence": "<findings>", "leader_generation": 1, "reviewed_sha": "<head_sha from status or task-detail>"}, "review_id, task_state, head_sha, next_action"),
    "attempt-reply": ("current leader; answers the worker on an active attempt", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "body": "<answer>"}, "task_id, attempt_id, at"),
    "task-integrate": ("integrator member or current leader (add leader_generation)", {"op_id": U, "session_id": U, "project_id": "<project-id>", "task_id": U, "evidence": "<verification on the base branch>", "leader_generation": 1, "destination": "<existing absolute path>", "merge": True}, "task_id, attempt_id, destination, head_sha, merge_sha, integrated_at"),
    "task-unblock": ("current leader", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "reason": "<reason>"}, "task_id, state"),
    "recover": ("current leader; only an expired claimed/in_progress attempt", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "task_id": U, "reason": "<reason>"}, "task_id, abandoned_attempt_id"),
    "project-status": ("current leader", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "status": "active|paused", "reason": "<reason>"}, "project_id, status"),
    "message-send": ("any member; to = a session_id, \"leader\" or \"workers\"", {"op_id": U, "session_id": U, "project_id": "<project-id>", "to": "leader", "task_id": None, "body": "<message>"}, "message_id, recipients"),
    "message-ack": ("recipient; marks inbox messages read; no op_id", {"session_id": U, "project_id": "<project-id>", "message_ids": [U]}, "acknowledged"),
    "activity-log": ("any member; one concise line per significant action", {"op_id": U, "session_id": U, "project_id": "<project-id>", "task_id": None, "action": "<what>", "result": "<outcome>"}, "seq, event_id"),
    "feedback-add": ("any member, once at the end (rulebook R14)", {"op_id": U, "session_id": U, "project_id": "<project-id>", "body": "<your feedback section, at most 40 lines>"}, "path"),
    "recommend": ("any member", {"op_id": U, "session_id": U, "project_id": "<project-id>", "task_id": None, "dedupe_key": "<stable key>", "body": "<proposal>"}, "recommendation_id, existing"),
    "recommend-decide": ("current leader", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "recommendation_id": U, "status": "accepted|declined|deferred", "reason": "<reason>"}, "recommendation_id, status"),
    "inbox-import": ("current leader", {"op_id": U, "session_id": U, "project_id": "<project-id>", "leader_generation": 1, "path": "<absolute inbox note path>"}, "task_id"),
    "sync": ("anyone (the watchdog also runs it); op_id optional", {"op_id": U}, "through_seq"),
    "backup": ("operator (the watchdog runs it daily)", {"op_id": U}, "path, through_seq"),
}
OPTIONAL = {"status": {"view", "include_done"}, "list": {"view"}, "task-detail": {"session_id"}, "review": {"leader_generation", "reviewed_sha"},
            "task-integrate": {"leader_generation", "destination", "merge"}, "heartbeat": {"generation", "op_id"}, "leader-renew": {"op_id"},
            "leader-acquire": {"takeover", "reason"}, "leader-join": {"appointment_code", "op_id", "tool", "label", "capabilities"}, "reviewer-join": {"appointment_code", "op_id", "tool", "label", "capabilities"}, "integrator-join": {"appointment_code", "op_id", "tool", "label", "capabilities"}, "worker-join": {"rulebook_version", "op_id", "tool", "label", "capabilities", "role"}, "task-claim": {"start"}, "next": {"since"},
            "checkpoint": {"note", "question", "blocker", "reply_seen", "op_id"}, "attempt-release": {"op_id"}, "task-submit": {"op_id"},
            "task-start": {"op_id"}, "sync": {"op_id"}}

OVERVIEW = f"""AgentMesh {__version__} - local multi-agent coordination CLI.
Rulebook (the only operational rules): {INSTALL_ROOT}\\vault\\04-Skills\\agent-mesh\\SKILL.md

Usage:   agentmesh --root <project-root> <command> --input <file | ->
         agentmesh help worker | help leader | help <command>
         agentmesh --version | doctor
CLI:     {INSTALL_ROOT}\\.venv\\Scripts\\agentmesh.exe
--root:  {INSTALL_ROOT}\\projects\\<project-id>   (installation root only for project-init, doctor, watchdog-all)
Input:   one JSON object <=64 KiB, on stdin with --input - or in a UTF-8 file. Unknown or missing keys are rejected.
         JSON with Windows paths: double every backslash or use forward slashes; prefer a UTF-8 file over shell quoting.
Output:  one UTF-8 JSON line. ok:true -> data. ok:false -> error{{code, message, next_action, blocker?}}.
Exit:    0 ok | 2 invalid input or unknown project | 3 conflict/lease/permission/blocked/git | 4 database busy | 5 I/O or internal error
op_id:   a NEW UUIDv4 per mutation (PowerShell: [guid]::NewGuid().ToString()); retry an uncertain result with the SAME op_id.
         Optional for next, heartbeat, leader-renew, message-ack, task-start, checkpoint, attempt-release, task-submit, sync.

Worker:  worker-join (returns status, commands, rules) -> task-claim with "start": true (creates your worktree)
         -> work and commit in the worktree -> checkpoint -> task-submit -> next on every reminder
Leader:  leader-join (the user clicks Yes on a popup) -> project-git -> plan-import (publish all)
         -> next on every reminder -> review (reviewed_sha) -> task-integrate with "merge": true
Operator (the user, in a terminal): appoint, halt, resume, gc, rulebook-seal, board, doctor, watchdog-all, ancestors
Commands: {", ".join(COMMANDS)}
"""


FLOWS = {
    "worker": ["worker-join", "next", "task-detail", "task-claim", "checkpoint", "task-submit", "attempt-release", "heartbeat",
               "message-send", "message-ack", "activity-log", "recommend", "feedback-add", "whoami", "session-close"],
    "leader": ["leader-join", "next", "project-git", "plan-import", "task-detail", "task-create", "task-spec", "task-depend", "task-publish",
               "task-cancel", "task-reopen", "task-reserve", "attempt-reply", "message-send", "message-ack", "review", "task-integrate",
               "recover", "task-unblock", "project-status", "activity-log", "recommend-decide", "metrics", "feedback-add", "leader-renew",
               "leader-release", "whoami", "session-close"],
    "reviewer": ["reviewer-join", "next", "task-detail", "review", "message-send", "message-ack", "activity-log", "feedback-add", "session-close"],
}
NOTES = {
    "heartbeat": 'Session only: {"session_id":"<session_id>","attempt_id":null}. After your lease ran out you can still heartbeat your attempt until the leader recovers it.',
    "review": "Reviewer membership instead of leader: omit leader_generation. reviewed_sha is required when the task shows head_sha.",
    "activity-log": "Leader handoff: action \"leader-handoff\" (current leader only), result = the full plan, next step and open decisions. Each one replaces the last.",
    "leader-acquire": "With appointment codes on (the default) this only re-confirms the current leader after a resume; a new leader uses leader-join.",
    "leader-renew": "Not needed for renewal. Use it once after your conversation was resumed: it records your new host process. Fails if you were replaced.",
    "checkpoint": "note, question, blocker, reply_seen are optional. note/question/blocker show to the leader and are replaced by each checkpoint. reply_seen true confirms you read the leader's reply.",
    "attempt-release": "Ends your attempt (abandoned); worktree and branch are kept. blocker true = the leader must fix something first.",
    "attempt-reply": "Stored on the attempt and sent as a message; the worker sees it in next.inbox and my_active_attempt.leader_reply.",
    "task-cancel": "Refused while other non-cancelled tasks depend on it; cancel those first.",
    "task-integrate": "With project-git: merge true merges exactly the reviewed commit; without merge the CLI checks that commit is already in the base branch. destination defaults to the repository.",
    "task-claim": "With project-git set, the CLI creates the worktree (git.created). A revision or released task continues from the previous attempt's branch.",
    "next": "since = the seq from your previous next; when nothing changed the answer is a few hundred bytes. call is a ready input for the command next_action names (add op_id where needed).",
    "plan-import": "keys are your own short names; depends_on lists keys, or task_ids of tasks already in the project (import a big plan in parts). Dependents become claimable only after each prerequisite is integrated.",
    "worker-join": "Only project_id is needed. The CLI fills in tool (from the program you run in), role worker, label <tool>-worker and capabilities []; pass them only to override. op_id is optional.",
    "leader-join": "Only project_id and rulebook_version are needed (also for reviewer-join and integrator-join); tool, label <tool>-<role>, capabilities [] and op_id are filled in. Without appointment_code the CLI shows the user a Yes/No popup and waits up to 90 s: give the command 2 minutes. If the popup cannot be shown, the user makes a one-time code with appoint (15 minutes, single-use) and you pass it as appointment_code.",
}


def command_block(name):
    who, example, returns = COMMANDS[name]
    optional = OPTIONAL.get(name, set())
    required = [k for k in example if k not in optional]
    shown = {k: (f"<{k}>" if v == U and k != "op_id" else v) for k, v in example.items()}
    return (f"agentmesh {name}\nWho:      {who}\nRequired: {', '.join(required)}\n"
            + (f"Optional: {', '.join(sorted(optional))}\n" if optional else "")
            + f"Returns:  {returns}\nInput:    {json.dumps(shown, ensure_ascii=False)}\n"
            + (f"Note:     {NOTES[name]}\n" if name in NOTES else ""))


def command_help(name):
    if name in FLOWS:
        header = (f"AgentMesh {name} flow: every command you need, with exact fields. Keep this output; do not run help again "
                  "unless a command returns INVALID_INPUT.\nIn every input: op_id = NEW UUIDv4 per mutation; copy every other id "
                  "exactly from an earlier response; replace every <...>.\n--root = the project root for every command.\n\n")
        return header + "\n".join(command_block(x) for x in FLOWS[name]), 0
    if name not in COMMANDS:
        return f"Unknown command {name!r}.\n\n" + OVERVIEW, 2
    return command_block(name), 0


def initialize(root, data):
    if set(data) != {"root"} or not isinstance(data["root"], str):
        return fail("INVALID_INPUT", "init requires exactly the root string", 2)
    supplied = Path(data["root"])
    if not root.is_absolute() or not supplied.is_absolute() or ".." in root.parts or ".." in supplied.parts:
        return fail("INVALID_INPUT", "root must be an absolute path without traversal", 2)
    root = root.resolve()
    if supplied.resolve() != root:
        return fail("CONFLICT", "input root does not match --root", 3)
    settings = {"version": 1, "root": str(root), "db": str(root / "runtime" / "coordination.db"),
                "vault": str(project_vault(root)), "session_lease_seconds": 900, "task_lease_seconds": 900,
                "reservation_seconds": 900, "busy_timeout_ms": 5000, "require_appointment_codes": True,
                "review_requires_different_tool": False, "auto_recover_after_seconds": 1800, "backup_keep": 14}
    config = root / "config" / "settings.json"
    if config.exists():
        try:
            settings = load_settings(root)
        except (OSError, ValueError, MeshError):
            return fail("CONFLICT", "existing settings cannot be read", 3)
    db_path = Path(settings["db"])
    for name in ("config", "runtime", "runtime/sessions", "backups",
                 "vault/00-Dashboard", "vault/01-Projects", "vault/02-Agents/Sessions",
                 "vault/03-Inbox/New-Tasks", "vault/04-Skills/agent-mesh", "worktrees"):
        if (root / "config" / "project.json").exists() and name.startswith("vault/"):
            continue
        (root / name).mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if version not in (0, 1, SCHEMA_VERSION) or (version == 0 and tables):
            return fail("CONFLICT", "incompatible existing database", 3)
        schema = schema_text()
        # A version 1 database is checked against the version 1 schema; the next command migrates it.
        if version == 1 and not schema_matches(connection, v1_schema_text()):
            return fail("CONFLICT", "existing version-1 database schema differs", 3)
        if version == SCHEMA_VERSION and not schema_matches(connection, schema):
            return fail("CONFLICT", f"existing version-{SCHEMA_VERSION} database schema differs", 3)
        connection.execute("PRAGMA journal_mode=WAL")
        if version == 0:
            connection.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
        if not config.exists():
            temp = config.with_suffix(".tmp")
            temp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, config)
    finally:
        connection.close()
    return {"ok": True, "data": {"root": str(root), "schema_version": SCHEMA_VERSION if version in (0, SCHEMA_VERSION) else version}}, 0


def initialize_project(base, data):
    if set(data) != {"op_id", "project_id", "title"}:
        raise MeshError("INVALID_INPUT", "project-init requires op_id, project_id and title", 2)
    try:
        op = uuid.UUID(data["op_id"])
    except (TypeError, ValueError, AttributeError):
        raise MeshError("INVALID_INPUT", "invalid op_id", 2)
    if op.version != 4 or str(op) != data["op_id"]:
        raise MeshError("INVALID_INPUT", "op_id must be UUIDv4", 2)
    pid = data["project_id"]
    reserved = {"con", "prn", "aux", "nul"} | {f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10)}
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", pid) or pid in reserved or not isinstance(data["title"], str) or not data["title"].strip():
        raise MeshError("INVALID_INPUT", "invalid project ID or title", 2)
    base = base.resolve()
    load_settings(base)
    from .projections import projection_lock
    with projection_lock(base):
        project_root = base / "projects" / pid
        verify_managed(base, project_root / "config" / "project.json")
        marker = project_root / "config" / "project.json"
        if project_root.exists() and not marker.exists():
            entries = list(project_root.iterdir())
            if entries and not (len(entries) == 1 and entries[0].name == "config" and entries[0].is_dir() and not any(entries[0].iterdir())):
                raise MeshError("CONFLICT", "existing project folder is not an AgentMesh project")
        (project_root / "config").mkdir(parents=True, exist_ok=True)
        expected = {"project_id": pid, "installation_root": str(base)}
        if marker.exists():
            if json.loads(marker.read_text(encoding="utf-8")) != expected:
                raise MeshError("CONFLICT", "project folder already has different settings")
        else:
            with marker.open("x", encoding="utf-8") as out:
                json.dump(expected, out)
        initialized, status = initialize(project_root, {"root": str(project_root)})
        if status != 0:
            return initialized, status
        db = sqlite3.connect(project_root / "runtime" / "coordination.db")
        try:
            existing = db.execute("SELECT title FROM projects WHERE id=?", (pid,)).fetchone()
        finally:
            db.close()
        if existing is not None and existing[0] != data["title"]:
            raise MeshError("CONFLICT", "project already exists with another title")
        if existing is None:
            execute(project_root, "project-create", {"op_id": data["op_id"], "project_id": pid, "title": data["title"]})
        else:
            project_request = {"op_id": data["op_id"], "project_id": pid, "title": data["title"]}
            digest = hashlib.sha256(canonical(project_request).encode("utf-8")).hexdigest()
            db = sqlite3.connect(project_root / "runtime" / "coordination.db", timeout=5, isolation_level=None)
            try:
                db.execute("BEGIN IMMEDIATE")
                operation = db.execute("SELECT command,input_hash FROM operations WHERE op_id=?", (data["op_id"],)).fetchone()
                if operation and operation != ("project-create", digest):
                    raise MeshError("CONFLICT", "op_id already used for different input")
                if not operation:
                    response = {"ok": True, "data": {"project_id": pid}}
                    db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (data["op_id"], "project-create", digest, canonical(response), now()))
                db.execute("COMMIT")
            finally:
                db.close()
            from .coordination import finish_files
            finish_files(project_root, "project-create", data, {"data": {"project_id": pid}})
        return {"ok": True, "data": {"project_id": pid, "root": str(project_root), "database": str(project_root / "runtime" / "coordination.db"), "vault": str(base / "vault")}}, 0


JSON_HINT = ("Windows paths need doubled backslashes in JSON (C:\\\\Users\\\\...) or forward slashes (C:/Users/...). "
             "Shell quoting often breaks inline JSON: write the input to a UTF-8 file and pass --input <file>, then retry.")


JOIN_ROLES = {"worker-join": "worker", "leader-join": "leader", "reviewer-join": "reviewer", "integrator-join": "reviewer"}


def role_card(role):
    """The role card from the rulebook (between <!-- card:ROLE --> and <!-- /card -->): one source for the file and the CLI."""
    try:
        text = (INSTALL_ROOT / "vault" / "04-Skills" / "agent-mesh" / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"<!-- card:" + re.escape(role) + r" -->\n(.*?)<!-- /card -->", text, re.S)
    return found.group(1).strip() if found else None


def onboard(root, command, response, project):
    """Everything a new chat needs in its first answer: the status body, its commands with exact fields, its rules."""
    if not response.get("ok"):
        return response
    data = dict(response["data"])
    role = JOIN_ROLES[command]
    try:
        data["status"] = execute(root, "next", {"session_id": data["session_id"], "project_id": project})["data"]
    except MeshError as exc:
        data["status"] = {"error": str(exc)}
    flow = FLOWS["reviewer" if role == "reviewer" else role]
    data["commands"] = {name: {"required": [k for k in COMMANDS[name][1] if k not in OPTIONAL.get(name, set())],
                               "optional": sorted(OPTIONAL.get(name, set())), "example": COMMANDS[name][1]} for name in flow}
    data["rules"] = role_card(role)
    data["reminder_seconds"] = 180
    data["next_action"] = ("Arm one reminder (every reminder_seconds) as your host skill says, then follow status.next_action. "
                           "On every reminder run next with since = the last seq.")
    return {**response, "data": data}


def missing_project(root):
    """A --root without config/settings.json is almost always a wrong project ID (often the display name)."""
    if (root / "config" / "settings.json").exists():
        return
    known = []
    if root.parent.name == "projects" and root.parent.is_dir():
        known = sorted(p.name for p in root.parent.iterdir() if (p / "config" / "settings.json").is_file())
    details = {"known_projects": known}
    guess = re.sub(r"[\s_]+", "-", root.name.strip().casefold())
    if guess in known and guess != root.name:
        details["did_you_mean"] = guess
    hint = (f"Use the project ID, not the display name: --root {root.parent}\\{details['did_you_mean']}. " if "did_you_mean" in details
            else f"Known project IDs: {', '.join(known)}. " if known else "")
    details["next_action"] = hint + "--root must be <installation root>\\projects\\<project-id>. If unsure, ask the user for the project ID."
    raise MeshError("PROJECT_NOT_FOUND", f"no AgentMesh project at {root}", 2, details)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--version":
        emit(__version__ + "\n")
        return 0
    from .admin import OPERATOR, run as run_operator
    words = [a for i, a in enumerate(argv) if a != "--root" and (i == 0 or argv[i - 1] != "--root")]
    if words and words[0] in OPERATOR:
        root = argv[argv.index("--root") + 1] if "--root" in argv and argv.index("--root") + 1 < len(argv) else None
        return run_operator(words[0], root, words[1:])
    # words = argv without "--root <path>", so `--root X help task-claim` works too (mini-sheet feedback).
    if not words or words[0] in ("-h", "--help", "help") or "-h" in words or "--help" in words:
        rest = [a for a in words if a not in ("-h", "--help", "help")]
        if words and words[0] == "help" and rest:
            text, status = command_help(rest[0])
        else:
            text, status = OVERVIEW, 0
        emit(text)
        return status
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("command")
    parser.add_argument("--input", required=True)

    def parser_error(message):
        raise ValueError(f"{message}. Usage: agentmesh --root <project-root> <command> --input <file|->")
    parser.error = parser_error
    args = None
    try:
        args = parser.parse_args(argv)
        if args.input == "-":
            raw = sys.stdin.buffer.read(65537)
        else:
            with open(args.input, "rb") as stream:
                raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("input exceeds 64 KiB")
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        try:
            data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs)
        except json.JSONDecodeError as exc:
            raise MeshError("INVALID_INPUT", f"input is not valid JSON: {exc}", 2, {"next_action": JSON_HINT})
        if not isinstance(data, dict):
            raise ValueError("input must be a JSON object")
        if args.command == "init":
            response, status = initialize(Path(args.root), data)
        elif args.command == "project-init":
            root = Path(args.root)
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError("root must be an absolute path without traversal")
            response, status = initialize_project(root, data)
        else:
            root = Path(args.root)
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError("root must be an absolute path without traversal")
            missing_project(root)
            response, status = execute(root, args.command, data), 0
            if args.command in JOIN_ROLES:
                response = onboard(root, args.command, response, data.get("project_id"))
    except MeshError as exc:
        response, status = fail(exc.code, str(exc), exc.status, exc.details)
        name = args.command if args is not None else None
        if exc.code == "INVALID_INPUT" and name in COMMANDS and (
                (exc.details or {}).get("missing_fields") or (exc.details or {}).get("unknown_fields") or str(exc) == "op_id required"):
            # The whole input format in the first error, so an agent fixes every field in one retry (mini-sheet feedback).
            response["error"]["required"] = [k for k in COMMANDS[name][1] if k not in OPTIONAL.get(name, set())]
            response["error"]["optional"] = sorted(OPTIONAL.get(name, set()))
            response["error"]["example"] = COMMANDS[name][1]
    except sqlite3.IntegrityError as exc:
        response, status = fail("CONFLICT", str(exc), 3)
    except (ValueError, UnicodeError) as exc:
        response, status = fail("INVALID_INPUT", str(exc), 2)
    except sqlite3.OperationalError as exc:
        response, status = fail("DATABASE_UNAVAILABLE", str(exc), 4)
    except (sqlite3.ProgrammingError, sqlite3.InterfaceError) as exc:
        response, status = fail("INVALID_INPUT", str(exc), 2)
    except sqlite3.Error as exc:
        response, status = fail("DATABASE_UNAVAILABLE", str(exc), 4)
    except OSError as exc:
        response, status = fail("IO_ERROR", str(exc), 5)
    except (TypeError, KeyError) as exc:
        response, status = fail("INVALID_INPUT", str(exc), 2)
    except Exception as exc:  # the one-JSON-line contract holds even for a defect
        response, status = fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", 5)
    response["cli_version"] = __version__
    emit(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
