"""Transactional AgentMesh coordination primitives (under development)."""
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


class MeshError(Exception):
    def __init__(self, code, message, status=3, details=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.details = details or {}


NEXT = {
    "session": "This session_id is missing, closed or expired and cannot be revived. Register a fresh session with a NEW op_id (workers: worker-join) and use the new session_id. If you still hold an attempt, heartbeat it with attempt_id and generation instead: that revives both.",
    "leader": "You are not the current leader: your leadership was released or taken over by another session (see status leader). Stop all leader work now and tell the user. Run leader-acquire again only if the user tells you to lead again; never re-acquire on your own.",
    "attempt": "Stop protected work on this attempt and keep its files. Run status with view compact; if the attempt expired, log it and wait for the leader to run recover, then claim a new attempt.",
    "membership": "This session lacks the required project role. Workers appointed by the user use worker-join; any other role must be granted as instructed by the user.",
    "op_id": "Generate a fresh canonical UUIDv4 for each new mutation, e.g. PowerShell [guid]::NewGuid().ToString(). Reuse an op_id only to retry identical input.",
    "op_reuse": "This op_id was already used for a different command or input. Use a new op_id for a changed request; reuse an op_id only with byte-identical input.",
    "fields": "Run `agentmesh help <command>` for the exact required fields and an example.",
}


SCHEMA_VERSION = 2


def schema_text():
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def v2_statements():
    return [x for x in Path(__file__).with_name("schema_v2.sql").read_text(encoding="utf-8").splitlines() if x.strip()]


def v1_schema_text():
    """The version 1 schema: today's schema.sql without the version 2 statements."""
    added = set(v2_statements())
    kept = [x for x in schema_text().splitlines() if x.strip() and x not in added and not x.startswith("PRAGMA")]
    return "\n".join(kept) + "\nPRAGMA user_version=1;\n"


def migrate(db, root):
    """Version 1 -> 2 adds tables and indexes only. A backup is taken first; the re-check under the write lock makes a race harmless."""
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = root / "backups" / f"pre-v2-{stamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    copy = sqlite3.connect(target)
    try:
        db.backup(copy)
        require(copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "pre-migration backup failed its integrity check")
    finally:
        copy.close()
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] == 1:
            for statement in v2_statements():
                db.execute(statement)
            for row in db.execute("SELECT task_id,at,session_id,payload_json FROM events WHERE action='task-integrate' AND result='success' AND task_id IS NOT NULL ORDER BY seq").fetchall():
                payload = json.loads(row[3])
                attempt_id = payload.get("integrated_attempt_id")
                if isinstance(payload.get("destination"), str) and db.execute("SELECT 1 FROM attempts WHERE id=?", (attempt_id,)).fetchone():
                    db.execute("INSERT OR REPLACE INTO integrations(task_id,attempt_id,destination,evidence,session_id,at) VALUES(?,?,?,?,?,?)",
                               (row[0], attempt_id, payload["destination"], payload.get("evidence"), row[2], row[1]))
            db.execute("PRAGMA user_version=2")
        db.execute("COMMIT")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id():
    return str(uuid.uuid4())


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def require(condition, message, code="CONFLICT", status=3, next_action=None):
    if not condition:
        raise MeshError(code, message, status, {"next_action": next_action} if next_action else None)


def fields(data, names):
    expected = set(names.split())
    missing, unknown = sorted(expected - set(data)), sorted(set(data) - expected)
    if missing or unknown:
        raise MeshError("INVALID_INPUT", "incorrect or unknown input fields", 2, {"missing_fields": missing, "unknown_fields": unknown, "next_action": NEXT["fields"]})


TEXT_KEYS = ("project_id", "kind", "view", "label", "tool", "role", "title", "description", "category", "reason", "body", "evidence",
             "destination", "action", "result", "dedupe_key", "status", "outcome", "summary", "path", "note", "question",
             "repository", "base_branch", "appointment_code", "rulebook_version", "reviewed_sha", "to")


def validate_input(data):
    for key in TEXT_KEYS:
        if key in data:
            require(isinstance(data[key], str), f"{key} must be a string", "INVALID_INPUT", 2, NEXT["fields"])
    for key in ("op_id", "session_id", "worker_session_id", "task_id", "depends_on_id", "attempt_id", "recommendation_id"):
        if key not in data or data[key] is None and key in ("task_id", "attempt_id"):
            continue
        value = data[key]
        try:
            parsed = uuid.UUID(value) if isinstance(value, str) else None
        except (ValueError, AttributeError):
            parsed = None
        require(parsed is not None and parsed.version == 4 and str(parsed) == value, f"{key} must be a canonical UUIDv4", "INVALID_INPUT", 2,
                NEXT["op_id"] if key == "op_id" else f"Copy {key} exactly from an earlier AgentMesh response; do not invent or retype it.")
    for key in ("generation", "leader_generation", "expected_version"):
        if key in data:
            require(type(data[key]) is int and data[key] > 0, f"{key} must be a positive integer", "INVALID_INPUT", 2)


# name: (type, default). leader_lease_seconds and max_poll_seconds are legacy: accepted and ignored.
OPTIONAL_SETTINGS = {
    "require_appointment_codes": (bool, True),
    "review_requires_different_tool": (bool, False),
    "auto_recover_after_seconds": (int, 1800),
    "backup_keep": (int, 14),
    "auto_wake": (bool, True),
    "leader_lease_seconds": (int, 900),
    "max_poll_seconds": (int, 300),
}


def load_settings(root):
    require(root.is_absolute() and ".." not in root.parts and not str(root).startswith("\\\\"), "invalid root", "INVALID_INPUT", 2)
    root = root.resolve()
    settings = json.loads((root / "config" / "settings.json").read_text(encoding="utf-8"))
    keys = {"version", "root", "db", "vault", "session_lease_seconds", "task_lease_seconds", "reservation_seconds", "busy_timeout_ms"}
    require(isinstance(settings, dict) and keys <= set(settings) <= keys | set(OPTIONAL_SETTINGS) and settings["version"] == 1, "invalid configuration",
            next_action=f"config\\settings.json may hold only {sorted(keys | set(OPTIONAL_SETTINGS))}.")
    for name, (kind, default) in OPTIONAL_SETTINGS.items():
        settings.setdefault(name, default)
        require(type(settings[name]) is kind, f"invalid {name}")
    for name, target in (("root", root), ("db", root / "runtime" / "coordination.db"), ("vault", project_vault(root))):
        require(isinstance(settings[name], str) and Path(settings[name]).resolve() == target.resolve(), f"invalid {name} location")
    for name in ("session_lease_seconds", "task_lease_seconds", "reservation_seconds"):
        require(type(settings[name]) is int and 60 <= settings[name] <= 86400, f"invalid {name}")
    require(60 <= settings["auto_recover_after_seconds"] <= 86400 and 1 <= settings["backup_keep"] <= 1000, "invalid watchdog settings")
    require(type(settings["busy_timeout_ms"]) is int and 1 <= settings["busy_timeout_ms"] <= 60000, "invalid busy timeout")
    for path in (root / "runtime", root / "runtime" / "coordination.db", project_vault(root), root / "worktrees", root / "backups"):
        require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(), "reparse points are not allowed in managed paths")
    return settings


def project_scope(root):
    marker = root / "config" / "project.json"
    if not marker.exists():
        return None
    info = json.loads(marker.read_text(encoding="utf-8"))
    require(set(info) == {"project_id", "installation_root"} and info["project_id"] == root.name and root.parent.name == "projects", "invalid project scope")
    base = root.parent.parent
    require(Path(info["installation_root"]).resolve() == base.resolve(), "invalid installation root")
    return info["project_id"], base


def project_vault(root):
    scope = project_scope(root)
    return scope[1] / "vault" if scope else root / "vault"


def verify_managed(root, path):
    root, path = root.absolute(), path.absolute()
    scope = project_scope(root)
    project_notes = scope[1] / "vault" / "01-Projects" / scope[0] if scope else None
    within_project = path == root or root in path.parents
    within_notes = project_notes is not None and (path == project_notes or project_notes in path.parents)
    require(within_project or within_notes, "managed path escapes root")
    stop = root.parent if within_project else scope[1].parent
    for component in (path, *path.parents):
        if component == stop:
            break
        require(not component.is_symlink() and not getattr(component, "is_junction", lambda: False)(), "reparse points are not allowed in managed paths")


def session(db, sid, allow_expired=False):
    """A usable session. allow_expired: an expired (not closed) session may still finish the attempt it holds."""
    row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    require(row is not None and row["status"] != "closed" and (allow_expired or lease_view(row["lease_until"], now())["lease_state"] == "valid"), "session missing, closed, expired or invalid", next_action=NEXT["session"])
    return row


def lease_view(until, at):
    """Compute lease validity without asserting whether an agent process is alive."""
    try:
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        current = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if end.tzinfo is None or current.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        seconds = (end - current).total_seconds()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"lease_state": "unknown", "lease_seconds_remaining": None}
    return {"lease_state": "valid" if seconds > 0 else "expired", "lease_seconds_remaining": max(0, math.ceil(seconds))}


# The leader holds its role for the whole conversation: the leader row and the leader's own session row carry
# this "held" timestamp instead of a renewable deadline. Only session-close, leader-release, a detected end of the
# leader's host process, or an explicit takeover frees the slot. Storing it as a timestamp keeps the schema unchanged.
HELD = "9999-12-31T23:59:59.999999Z"

# Host programs found by walking up from the CLI call. Verified 2026-11-08: Claude Code runs one claude.exe with
# --session-id per conversation under a shared daemon; the Codex CLI runs one codex.exe per terminal tab. A host that
# serves several chats from one process can only make a closed chat look alive (the takeover flag is then needed),
# never make a live leader look gone. Hosts not listed get no process check (liveness "unknown").
HOST_EXES = ("claude.exe", "codex.exe")


def _win_processes():
    """pid -> (parent pid, exe name) for every process, from one Toolhelp32 snapshot. None if unavailable."""
    import ctypes
    from ctypes import wintypes

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ENTRY))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    try:
        entry, found = ENTRY(), {}
        entry.dwSize = ctypes.sizeof(ENTRY)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            found[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        return found
    finally:
        k32.CloseHandle(snap)


def _win_started(pid):
    """(state, creation time) of a process: ("alive", t), ("gone", None) or ("unknown", None)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # ERROR_INVALID_PARAMETER: no such process. Anything else (e.g. access denied) proves nothing.
        return ("gone", None) if ctypes.get_last_error() == 87 else ("unknown", None)
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        code = wintypes.DWORD()
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]) or not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return ("unknown", None)
        if code.value != 259:  # STILL_ACTIVE
            return ("gone", None)
        return ("alive", str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime))
    finally:
        k32.CloseHandle(handle)


def host_identity():
    """The nearest known host process above this CLI call ({name, pid, started}), or None if there is none."""
    import os
    if os.name != "nt":
        return None
    try:
        table = _win_processes()
        if not table:
            return None
        pid, seen = os.getpid(), set()
        while pid in table and pid not in seen and len(seen) < 64:
            seen.add(pid)
            parent, exe = table[pid]
            if exe.lower() in HOST_EXES:
                state, started = _win_started(pid)
                return {"name": exe, "pid": pid, "started": started} if state == "alive" else None
            pid = parent
    except (OSError, AttributeError, ValueError):
        return None
    return None


def detect_tool():
    """The tool value for a worker that did not give one: from the env variable its host sets, else its host process."""
    import os
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
        return "antigravity"
    return {"claude.exe": "claude-code", "codex.exe": "codex"}.get(((host_identity() or {}).get("name") or "").lower(), "other")


def host_state(host):
    """alive / gone / unknown for a recorded host. A different creation time means the PID was reused: gone."""
    import os
    if not isinstance(host, dict) or not host.get("pid") or not host.get("started") or os.name != "nt":
        return "unknown"
    try:
        state, started = _win_started(int(host["pid"]))
    except (OSError, AttributeError, ValueError):
        return "unknown"
    if state == "alive" and started != host["started"]:
        return "gone"
    return state


def last_handoff(db, project):
    """The newest leader-handoff note (activity-log written by the current leader), or None. Each note replaces the last."""
    row = db.execute("SELECT at,session_id,result FROM events WHERE project_id=? AND action='leader-handoff' ORDER BY seq DESC LIMIT 1", (project,)).fetchone()
    return {"text": row["result"], "at": row["at"], "session_id": row["session_id"]} if row else None


LEADER_ALERTS = {
    "gone": "The leader's chat is gone (its host process ended).",
    "owner_unavailable": "The leader's session is closed or expired.",
    "released": "There is no leader: the last one released the role.",
}


def recorded_host(db, project, sid):
    """The host process last recorded for this leader session (leader-acquire or leader-host event), or None."""
    row = db.execute("SELECT payload_json FROM events WHERE project_id=? AND session_id=? AND result='success' "
                     "AND action IN ('leader-acquire','leader-host','leader-join') ORDER BY seq DESC LIMIT 1", (project, sid)).fetchone()
    return json.loads(row["payload_json"]).get("host") if row else None


def session_view(row, at, db=None):
    result = dict(row)
    if result["status"] == "closed":
        details = {"lease_state": "closed", "lease_seconds_remaining": 0}
    else:
        details = lease_view(result["lease_until"], at)
        if result["lease_until"] == HELD:
            # The current leader's session does not expire and needs no heartbeat.
            details.update(lease_seconds_remaining=None, renewal="not needed (current leader)")
    result.update(details)
    result["effective_status"] = (result["status"] if details["lease_state"] == "valid" else
                                  "lease_expired" if details["lease_state"] == "expired" else
                                  details["lease_state"])
    if db is not None and result["effective_status"] == "busy":
        active = db.execute("SELECT a.id, a.lease_until FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE a.session_id=? AND a.state IN ('claimed','in_progress')", (result["id"],)).fetchall()
        if len(active) != 1:
            result["effective_status"] = "work_state_unknown"
        else:
            attempt_state = lease_view(active[0]["lease_until"], at)["lease_state"]
            result["attempt_lease_state"] = attempt_state
            if attempt_state == "expired":
                result["effective_status"] = "attempt_lease_expired"
            elif attempt_state == "unknown":
                result["effective_status"] = "work_state_unknown"
    return result


def leader_view(db, row, at):
    if row is None:
        return None
    """One leader slot per project. effective_status: active (host process seen alive), unknown (no process check
    possible, e.g. a Codex host), gone (host process ended: the slot is free), owner_unavailable (owner session
    closed or expired: the slot is free) or released."""
    result = {"project_id": row["project_id"], "session_id": row["session_id"], "generation": row["generation"]}
    held = lease_view(row["lease_until"], at)["lease_state"] == "valid"
    owner = db.execute("SELECT * FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
    owner_state = session_view(owner, at, db)["lease_state"] if owner else "unknown"
    result["owner_session_lease_state"] = owner_state
    host = recorded_host(db, row["project_id"], row["session_id"]) if held else None
    result["owner_host"] = {"name": host["name"], "pid": host["pid"]} if host else None
    result["host_state"] = host_state(host) if held else "unknown"
    if not held:
        result["effective_status"] = "released"
    elif owner_state != "valid":
        result["effective_status"] = "owner_unavailable"
    elif result["host_state"] == "alive":
        result["effective_status"] = "active"
    else:
        result["effective_status"] = result["host_state"]
    result["slot_free"] = result["effective_status"] in ("released", "owner_unavailable", "gone")
    return result


def member(db, sid, project, roles):
    session(db, sid)
    found = db.execute("SELECT role FROM memberships WHERE session_id=? AND project_id=?", (sid, project)).fetchall()
    require(any(x[0] in roles for x in found), "project membership required", next_action=NEXT["membership"])


def leader(db, data):
    sid, project = data["session_id"], data["project_id"]
    member(db, sid, project, {"leader-candidate"})
    row = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (project,)).fetchone()
    require(row is not None and row["session_id"] == sid and row["generation"] == data["leader_generation"] and lease_view(row["lease_until"], now())["lease_state"] == "valid", "not the current leader (released, taken over or fenced)", next_action=NEXT["leader"])
    # A resumed conversation runs in a new host process: record it, so status does not report the leader as gone.
    host = host_identity()
    if host is not None and host != recorded_host(db, project, sid):
        db.execute("INSERT INTO events(event_id,at,session_id,project_id,task_id,action,result,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                   (new_id(), now(), sid, project, None, "leader-host", "success", canonical({"host": host})))


def attempt(db, data, allow_expired=False):
    """The caller's current attempt. allow_expired: a holder whose lease ran out keeps the attempt until the leader
    or the watchdog runs recover (that clears current_attempt_id, which still fences the old holder)."""
    session(db, data["session_id"], allow_expired)
    row = db.execute("SELECT a.*, t.current_attempt_id, t.project_id FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (data["attempt_id"],)).fetchone()
    require(row is not None and row["session_id"] == data["session_id"] and row["generation"] == data["generation"] and row["current_attempt_id"] == row["id"] and (allow_expired or lease_view(row["lease_until"], now())["lease_state"] == "valid") and row["state"] in ("claimed", "in_progress"), "attempt expired, invalid, inactive or fenced", next_action=NEXT["attempt"])
    return row


def touch(db, cfg, sid, timestamp):
    """Any successful mutation by a session proves it is alive: renew its lease and its current attempt's lease."""
    row = db.execute("SELECT status,lease_until FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None or row["status"] == "closed":
        return
    if row["lease_until"] != HELD:
        db.execute("UPDATE sessions SET last_seen_at=?,lease_until=? WHERE id=?", (timestamp, later(cfg["session_lease_seconds"]), sid))
    else:
        db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (timestamp, sid))
    db.execute("UPDATE attempts SET lease_until=? WHERE session_id=? AND state IN ('claimed','in_progress') "
               "AND id IN (SELECT current_attempt_id FROM tasks WHERE current_attempt_id IS NOT NULL)", (later(cfg["task_lease_seconds"]), sid))
    # Remember which host process this session runs in (whoami, watchdog). Only a known host is recorded.
    host = host_identity()
    if host is not None:
        db.execute("INSERT INTO session_hosts VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET name=excluded.name,pid=excluded.pid,started=excluded.started,seen_at=excluded.seen_at",
                   (sid, host["name"], host["pid"], host["started"], timestamp))
    record_chat(db, sid, timestamp)


# Chat ids, so the leader can wake an idle worker (0.5.3). Keyed on the session's tool, not the host process name,
# because the Antigravity process name is still unknown. The env variable is what the host gives the commands it runs;
# the files are where the host records each command and its output (verified on this PC 2026-11-10: Codex 0.167
# rollout files, Antigravity CLI 1.5.2 conversations/<conversation-id>.db).
CHAT_HOSTS = {"codex": {"env": "CODEX_THREAD_ID", "action": "codex-chat-id"},
              "antigravity": {"env": "ANTIGRAVITY_CONVERSATION_ID", "action": "antigravity-chat-id"}}
WAKE_MESSAGE = "Check AgentMesh for updates."


def as_uuid(text):
    try:
        return str(uuid.UUID(str(text).strip()))
    except ValueError:
        return None


def chat_home():
    import os
    return Path(os.environ.get("AGENTMESH_CHAT_HOME") or Path.home())  # tests point this at a temporary folder


def chat_files(tool):
    """(file, chat id or None) pairs where the host records its chats. Codex keeps the id in the file's first line."""
    import os
    home = chat_home()
    if tool == "codex":
        base = home / ".codex" if os.environ.get("AGENTMESH_CHAT_HOME") else Path(os.environ.get("CODEX_HOME") or home / ".codex")
        return [(p, None) for p in (base / "sessions").glob("*/*/*/rollout-*.jsonl")]
    return [(p, as_uuid(p.name.split(".")[0])) for folder in ("antigravity-cli", "antigravity")
            for p in (home / ".gemini" / folder / "conversations").glob("*.db*")]


def find_chat(tool, sid):
    """(chat id, source) of the chat this CLI call runs in, or None. First the host's env variable; otherwise the one chat
    whose files, written in the last 15 minutes, contain this AgentMesh session_id. Two or more chats: nothing is guessed."""
    import os
    import time
    chat = as_uuid(os.environ.get(CHAT_HOSTS[tool]["env"], ""))
    if chat:
        return chat, CHAT_HOSTS[tool]["env"]
    cutoff, found = time.time() - 15 * 60, set()
    try:
        recent = sorted(((p, c) for p, c in chat_files(tool) if p.stat().st_mtime > cutoff), key=lambda pc: pc[0].stat().st_mtime, reverse=True)[:12]
    except OSError:
        return None
    for path, chat in recent:
        try:
            data = path.read_bytes()
            if sid.encode("ascii") not in data:
                continue
            if chat is None:
                chat = as_uuid((json.loads(data.split(b"\n", 1)[0]).get("payload") or {}).get("id", ""))
        except (OSError, ValueError, AttributeError):
            continue
        if chat:
            found.add(chat)
    return (found.pop(), "chat file") if len(found) == 1 else None


def record_chat(db, sid, timestamp):
    """Once per Codex or Antigravity session: store its chat id as an event (<tool>-chat-id) for the leader's next.wake."""
    import os
    row = db.execute("SELECT tool FROM sessions WHERE id=?", (sid,)).fetchone()
    host = CHAT_HOSTS.get(row["tool"]) if row else None
    if host is None or db.execute("SELECT 1 FROM events WHERE session_id=? AND action=? LIMIT 1", (sid, host["action"])).fetchone():
        return
    project = db.execute("SELECT project_id FROM memberships WHERE session_id=? LIMIT 1", (sid,)).fetchone()
    chat = find_chat(row["tool"], sid) if project else None
    if chat:
        payload = {"chat_id": chat[0], "source": chat[1], "recorded_by": "cli"}
        address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
        if row["tool"] == "antigravity" and address and len(address) < 100:
            payload["ls_address"] = address  # where agentapi reaches this Antigravity; the CSRF token is a secret and is never stored
        db.execute("INSERT INTO events(event_id,at,session_id,project_id,task_id,action,result,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                   (new_id(), timestamp, sid, project[0], None, host["action"], f"{chat[0]} for session {sid}", canonical(payload)))


def wake_command(tool, chat, payload):
    if tool == "codex":
        return f'codex queue --thread {chat} --message "{WAKE_MESSAGE}"'
    agy = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
    prefix = f"ANTIGRAVITY_LS_ADDRESS={payload['ls_address']} " if payload.get("ls_address") else ""
    return f'{prefix}"{agy.as_posix() if agy.exists() else "agy"}" agentapi send-message {chat} "{WAKE_MESSAGE}"'


def chat_workers(db, project, tools=("codex", "antigravity")):
    """(session row, chat id, chat payload) of the project's workers on these tools that are not closed and have a recorded chat id."""
    # Expired sessions stay listed: a Codex worker cannot remind itself, so its lease runs out while it waits for a wake.
    rows = db.execute("SELECT s.id,s.tool,s.label,(SELECT e.payload_json FROM events e WHERE e.session_id=s.id AND e.action IN ('codex-chat-id','antigravity-chat-id') "
                      "ORDER BY e.seq DESC LIMIT 1) AS chat FROM sessions s JOIN memberships m ON m.session_id=s.id AND m.project_id=? AND m.role='worker' "
                      f"WHERE s.status!='closed' AND s.tool IN ({','.join('?' * len(tools))})", (project, *tools)).fetchall()
    found = []
    for r in rows:
        try:
            payload = json.loads(r["chat"] or "{}")
        except ValueError:
            payload = {}
        chat = as_uuid(payload.get("chat_id", ""))
        if chat:
            found.append((r, chat, payload))
    return found


def wake_list(db, project, auto=False):
    """For the leader: live Codex and Antigravity workers with a known chat id, and the command that wakes each one.
    With auto-wake on, the CLI wakes Codex workers itself, so only Antigravity workers are listed."""
    wake = []
    for r, chat, payload in chat_workers(db, project, ("antigravity",) if auto else ("codex", "antigravity")):
        unread = db.execute("SELECT COUNT(*) FROM messages WHERE to_session_id=? AND read_at IS NULL", (r["id"],)).fetchone()[0]
        busy = db.execute("SELECT 1 FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE a.session_id=? AND a.state IN ('claimed','in_progress')", (r["id"],)).fetchone()
        wake.append({"session_id": r["id"], "tool": r["tool"], "label": r["label"], "chat_id": chat, "idle": busy is None, "unread": unread,
                     "has_own_reminder": r["tool"] == "antigravity", "command": wake_command(r["tool"], chat, payload)})
    return wake


# Auto-wake (0.5.5): after a committed command the CLI itself runs `codex queue` for each Codex worker that has news it was
# not woken for yet, so a Codex worker (no self-reminder) reacts within seconds instead of waiting for the leader.
# `codex queue` returns in about 2 s (measured 2026-11-10), so it runs synchronously and only a real success counts.
WAKE_GAP_SECONDS = 30          # at most one wake per worker per 30 s
WAKE_RETRY_SECONDS = 300       # after a failed wake, wait 5 minutes before trying that worker again
WAKE_PER_HOUR = 30


def auto_wake_on(cfg):
    import os
    return cfg.get("auto_wake", True) and not os.environ.get("AGENTMESH_NO_WAKE")  # tests switch it off


def wake_news(db, sid, project, at):
    """What this worker could act on now: unread messages, and tasks it could claim (an expired session lease is ignored:
    that is what waiting for a wake looks like). A task's version is part of its key, so a revision is news again."""
    news = {f"m:{r[0]}" for r in db.execute("SELECT id FROM messages WHERE project_id=? AND to_session_id=? AND read_at IS NULL", (project, sid))}
    found, row = session_blockers(db, sid, project, at)
    if row is not None and all(b["code"] == "session_expired" for b in found):
        status = project_status_of(db, project)
        for t in db.execute("SELECT * FROM tasks WHERE project_id=? AND state='ready'", (project,)).fetchall():
            if not task_blockers(db, t, status, row, sid, at):
                news.add(f"t:{t['id']}:{t['version']}")
    return news


def codex_exe():
    import os
    import shutil
    found = shutil.which("codex")
    default = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    return found or (str(default) if default.exists() else None)


def run_wake(chat):
    """(ok, detail): queue the wake message for this Codex chat. The same command the leader ran by hand in 0.5.3."""
    import subprocess
    exe = codex_exe()
    if exe is None:
        return False, "codex not found"
    try:
        done = subprocess.run([exe, "queue", "--thread", chat, "--message", WAKE_MESSAGE], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:200]
    return done.returncode == 0, (done.stderr or done.stdout).decode("utf-8", "replace").strip()[:200]


def auto_wake(db, cfg, caller):
    """Run after COMMIT; never raises, never changes the command's answer. Each attempt is an `auto-wake` event of the worker
    (project_id left empty so it does not move the project's seq, which would make every next look changed)."""
    import os
    try:
        # A command run from inside Codex skips it (its sandbox may block codex queue); the leader's next catches up.
        if not auto_wake_on(cfg) or os.environ.get("CODEX_THREAD_ID"):
            return
        at = now()
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for (project,) in db.execute("SELECT id FROM projects WHERE status='active'").fetchall():
            for r, chat, _ in chat_workers(db, project, ("codex",)):
                if r["id"] == caller:
                    continue
                news = wake_news(db, r["id"], project, at)
                if not news:
                    continue
                tries = [(x["at"], json.loads(x["payload_json"])) for x in db.execute(
                    "SELECT at,payload_json FROM events WHERE session_id=? AND action='auto-wake' AND at>? ORDER BY seq DESC", (r["id"], hour_ago)).fetchall()]
                if len(tries) >= WAKE_PER_HOUR:
                    continue
                if tries:
                    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(tries[0][0].replace("Z", "+00:00"))).total_seconds()
                    if seconds < (WAKE_GAP_SECONDS if tries[0][1].get("ok") else WAKE_RETRY_SECONDS):
                        continue
                last = db.execute("SELECT payload_json FROM events WHERE session_id=? AND action='auto-wake' AND payload_json LIKE '%\"ok\":true%' "
                                  "ORDER BY seq DESC LIMIT 1", (r["id"],)).fetchone()
                if news <= set(json.loads(last[0]).get("news", []) if last else []):
                    continue  # already woken for all of it
                ok, detail = run_wake(chat)
                payload = {"project_id": project, "chat_id": chat, "news": sorted(news), "ok": ok, "detail": detail}
                db.execute("INSERT INTO events(event_id,at,session_id,project_id,task_id,action,result,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                           (new_id(), now(), r["id"], None, None, "auto-wake", "woken" if ok else "failed", canonical(payload)))
    except Exception:  # noqa: BLE001 - a wake is a convenience; the command already succeeded
        pass


BLOCKERS = {
    "session_missing": ("session not found", "Register a session first; workers use worker-join with a new op_id."),
    "session_closed": ("session is closed", "Register a fresh session with a new op_id; a closed session cannot be reopened."),
    "session_expired": ("session lease expired or invalid", NEXT["session"]),
    "not_worker": ("session has no worker membership in this project", "Only worker members can claim. A user-appointed worker joins with worker-join."),
    "has_active_attempt": ("session already has an active attempt", "Submit the active attempt (see my_active_attempt in compact status) before claiming another task."),
    "project_paused": ("project is not active", "The leader paused the project. Do not claim; check status again on your next reminder."),
    "not_ready": ("task is not in ready state", "Only ready tasks can be claimed. Choose a task with claimable=true."),
    "reserved_elsewhere": ("task is reserved for another worker", "Choose another claimable task; the reservation lapses at reservation_until."),
    "capability_mismatch": ("session lacks required capabilities", "Choose another task. Never re-register with capabilities you do not actually have."),
    "dependency_incomplete": ("dependency is incomplete", "A prerequisite is not accepted yet. Choose another claimable task or wait for its review."),
    "same_tool_review": ("reviewer uses the same tool as the author", "Ask the user to appoint a reviewer that runs a different tool, or let the leader review."),
    "dependency_not_integrated": ("dependency is accepted but not merged", "The prerequisite is accepted but not merged into the base branch yet. Choose another task or wait."),
}


def blocker(code, **extra):
    return {"code": code, **extra}


def session_blockers(db, sid, project, at):
    """Blockers that depend only on the caller; shared by advisory reads and task-claim."""
    row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return [blocker("session_missing")], None
    found = []
    if row["status"] == "closed":
        found.append(blocker("session_closed"))
    elif lease_view(row["lease_until"], at)["lease_state"] != "valid":
        found.append(blocker("session_expired", lease_until=row["lease_until"]))
    if not db.execute("SELECT 1 FROM memberships WHERE session_id=? AND project_id=? AND role='worker'", (sid, project)).fetchone():
        found.append(blocker("not_worker"))
    # An expired but unrecovered attempt is still the session's work: no second claim until submit, release or recover.
    active = db.execute("SELECT a.id,a.task_id FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE a.session_id=? AND a.state IN ('claimed','in_progress')", (sid,)).fetchone()
    if active:
        found.append(blocker("has_active_attempt", attempt_id=active["id"], task_id=active["task_id"]))
    return found, row


def task_blockers(db, task, project_status, session_row, sid, at):
    """Blockers that depend on the task; shared by advisory reads and task-claim."""
    found = []
    if project_status != "active":
        found.append(blocker("project_paused", project_status=project_status))
    if task["state"] != "ready":
        found.append(blocker("not_ready", state=task["state"]))
    if not (task["reservation_until"] is None or task["reservation_until"] <= at or task["reservation_session_id"] == sid):
        found.append(blocker("reserved_elsewhere", reservation_until=task["reservation_until"]))
    if session_row is not None:
        missing = sorted(set(json.loads(task["required_capabilities_json"])) - set(json.loads(session_row["capabilities_json"])))
        if missing:
            found.append(blocker("capability_mismatch", missing=missing))
    pending = db.execute("SELECT t.id,t.title,t.state FROM dependencies d JOIN tasks t ON t.id=d.depends_on_id WHERE d.task_id=? AND t.state!='completed' ORDER BY t.id", (task["id"],)).fetchall()
    if pending:
        found.append(blocker("dependency_incomplete", prerequisites=[dict(x) for x in pending]))
    # The integration gate (plan 0.3.3): an accepted prerequisite counts only once it is merged and recorded.
    unmerged = db.execute("SELECT t.id,t.title FROM dependencies d JOIN tasks t ON t.id=d.depends_on_id LEFT JOIN integrations i ON i.task_id=t.id "
                          "WHERE d.task_id=? AND t.state='completed' AND i.task_id IS NULL ORDER BY t.id", (task["id"],)).fetchall()
    if unmerged:
        found.append(blocker("dependency_not_integrated", prerequisites=[dict(x) for x in unmerged]))
    return found


def live_worker_capabilities(db, project, at):
    """Capability sets of worker sessions in the project that are not closed and hold a valid session lease."""
    rows = db.execute("SELECT DISTINCT s.id,s.capabilities_json,s.status,s.lease_until FROM sessions s JOIN memberships m ON m.session_id=s.id "
                      "WHERE m.project_id=? AND m.role='worker' ORDER BY s.id", (project,)).fetchall()
    return [set(json.loads(r["capabilities_json"])) for r in rows
            if r["status"] != "closed" and lease_view(r["lease_until"], at)["lease_state"] == "valid"]


def capable_count(workers, required):
    return sum(1 for caps in workers if set(required) <= caps)


def raise_blocked(found):
    first = found[0]
    raise MeshError("CONFLICT", BLOCKERS[first["code"]][0], 3, {"blocker": first["code"], "blockers": found, "next_action": BLOCKERS[first["code"]][1]})


def worktree_info(root, project, task_id, attempt_id):
    """Deterministic worktree path and branch for an attempt. With project-git set, task-claim creates the worktree."""
    return {"worktree": str(root / "worktrees" / project / (task_id + "-" + attempt_id)), "branch": "agentmesh/" + task_id + "-" + attempt_id}


def integrations(db, project, task_id=None):
    """The recorded integration per task (table integrations, schema 2)."""
    sql = "SELECT i.* FROM integrations i JOIN tasks t ON t.id=i.task_id WHERE t.project_id=?"
    args = [project]
    if task_id is not None:
        sql += " AND i.task_id=?"
        args.append(task_id)
    return {row["task_id"]: {"at": row["at"], "by_session_id": row["session_id"], "destination": row["destination"], "attempt_id": row["attempt_id"],
                             "evidence": row["evidence"], "head_sha": row["head_sha"], "merge_sha": row["merge_sha"]}
            for row in db.execute(sql, args).fetchall()}


def attempt_paths(root, row, paths):
    """Check output paths and return them absolute. A relative path is taken inside the attempt worktree (0.5.4)."""
    require(isinstance(paths, list) and all(isinstance(x, str) for x in paths), "paths must be strings", "INVALID_INPUT", 2)
    worktree = root / "worktrees" / row["project_id"] / (row["task_id"] + "-" + row["id"])
    allowed, checked = [worktree.resolve()], []
    for item in paths:
        p = Path(item) if Path(item).is_absolute() else worktree / item
        require(p.is_absolute() and ".." not in p.parts and not item.startswith("\\\\") and p.exists(), "invalid or missing output path", "INVALID_INPUT", 2,
                f"Give paths that exist inside your worktree {worktree}, absolute or relative to it.")
        checked.append(str(p))
        actual = p.resolve(strict=True)
        require(any(actual == base or base in actual.parents for base in allowed), "output path escapes attempt worktree", "INVALID_INPUT", 2)
        for parent in [p, *p.parents]:
            if parent == root.parent:
                break
            require(not parent.is_symlink() and not getattr(parent, "is_junction", lambda: False)(), "reparse points are not allowed", "INVALID_INPUT", 2)
    return checked


def inbox_note(root, value):
    path = Path(value)
    folder = (project_vault(root) / "03-Inbox" / "New-Tasks").resolve()
    require(path.is_absolute() and ".." not in path.parts and not value.startswith("\\\\") and path.is_file(), "invalid inbox path", "INVALID_INPUT", 2)
    resolved = path.resolve(strict=True)
    require(folder in resolved.parents, "inbox path escapes inbox", "INVALID_INPUT", 2)
    for part in [path, *path.parents]:
        if part == root.parent:
            break
        require(not part.is_symlink() and not getattr(part, "is_junction", lambda: False)(), "reparse points are not allowed", "INVALID_INPUT", 2)
    raw = path.read_bytes()
    require(len(raw) <= 65536, "inbox note exceeds 64 KiB", "INVALID_INPUT", 2)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise MeshError("INVALID_INPUT", "inbox note must be UTF-8", 2)
    rows = text.splitlines()
    require(rows and rows[0].strip() == "---", "missing inbox frontmatter", "INVALID_INPUT", 2)
    try:
        end = rows.index("---", 1)
    except ValueError:
        raise MeshError("INVALID_INPUT", "missing frontmatter terminator", 2)
    values = {}
    for row in rows[1:end]:
        require(":" in row, "invalid frontmatter", "INVALID_INPUT", 2)
        key, value = row.split(":", 1)
        key, value = key.strip(), value.strip()
        require(key in ("request_id", "project_id", "title", "category", "priority") and key not in values and value, "unknown, duplicate or empty field", "INVALID_INPUT", 2)
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise MeshError("INVALID_INPUT", "invalid quoted scalar", 2)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        else:
            require(not value.startswith(("!", "&", "*", "[", "{", "|", ">", "'")) and " #" not in value, "unsupported frontmatter scalar", "INVALID_INPUT", 2)
        require(isinstance(value, str) and value.strip(), "invalid frontmatter scalar", "INVALID_INPUT", 2)
        values[key] = value
    require(set(values) == {"request_id", "project_id", "title", "category", "priority"}, "missing frontmatter field", "INVALID_INPUT", 2)
    body = "\n".join(rows[end + 1:]).strip()
    require(bool(body), "empty inbox request", "INVALID_INPUT", 2)
    try:
        identifier = uuid.UUID(values["request_id"])
        priority = int(values["priority"])
    except ValueError:
        raise MeshError("INVALID_INPUT", "invalid request ID or priority", 2)
    require(identifier.version == 4 and str(identifier) == values["request_id"] and 0 <= priority <= 100 and str(priority) == values["priority"], "invalid request ID or priority", "INVALID_INPUT", 2)
    return values, body, hashlib.sha256(raw).hexdigest(), str(resolved), priority


CHECKPOINT_OPTIONAL = ("note", "question", "blocker", "reply_seen")
UNLOGGED = ("heartbeat", "leader-renew", "next", "message-ack")
READS = ("status", "list", "task-detail", "whoami", "metrics")
# A repeat of these is harmless or fails cleanly, so the CLI makes the op_id when none is given (plan 0.4.3).
AUTO_OP = ("task-start", "checkpoint", "attempt-release", "task-submit", "sync", "worker-join", "leader-join", "reviewer-join", "integrator-join")
TOOLS = ("codex", "claude-code", "antigravity", "opencode", "other")
STATE_ORDER = ("CASE state WHEN 'ready' THEN 0 WHEN 'claimed' THEN 1 WHEN 'in_progress' THEN 1 WHEN 'submitted' THEN 2 "
               "WHEN 'in_review' THEN 2 WHEN 'blocked' THEN 3 WHEN 'proposed' THEN 4 ELSE 5 END")
CONTROL = set(chr(c) for c in range(32)) | {"\x7f"}


def capability_list(value, name):
    """Capabilities compare case-insensitively: trim, lowercase, de-duplicate, sort."""
    require(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value), f"{name} must be a list of non-empty strings", "INVALID_INPUT", 2)
    return sorted({x.strip().lower() for x in value})


def single_line(d, *names):
    for name in names:
        if name in d:
            require(isinstance(d[name], str) and not (set(d[name]) & CONTROL), f"{name} must be a single line", "INVALID_INPUT", 2)


def event(db, data, command, result):
    payload = {k: data[k] for k in ("reason", "summary", "evidence", "paths", "manifest", "preserved_checkpoint", "preserved_worktree", "preserved_branch", "destination", "integrated_attempt_id",
                                    "note", "question", "blocker", "body", "takeover", "host", "head_sha", "merge_sha", "reviewed_sha",
                                    "extra_commits", "repository", "base_branch", "role", "approval") if k in data}
    db.execute("INSERT INTO events(event_id,at,session_id,project_id,task_id,action,result,payload_json) VALUES(?,?,?,?,?,?,?,?)", (new_id(), now(), data.get("session_id"), data.get("project_id"), data.get("task_id"), command, result, canonical(payload)))


HALT_ALLOWED = ("checkpoint", "attempt-release", "session-close", "heartbeat", "message-ack", "activity-log", "feedback-add")


def halt_reason(db):
    row = db.execute("SELECT value FROM project_flags WHERE name='halt' LIMIT 1").fetchone()
    return row[0] if row else None


def halted(db, data, command):
    """The user's emergency stop (operator command `halt`): only saving and stopping work stays possible."""
    if command in HALT_ALLOWED:
        return
    reason = halt_reason(db)
    require(reason is None, f"the user halted this project: {reason}", next_action=HALT_HINT.format(reason=reason))


HALT_HINT = "The user halted this project: {reason}. Commit your work, send a checkpoint, and wait for the user. Do not try to resume it yourself."


def finish_files(root, command, request, response):
    if command == "feedback-add":
        # One file per agent (no shared file to overwrite); sync joins them into feedback\<project>.md.
        path = Path(response["data"]["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        from .projections import replace
        replace(path, request["body"].rstrip("\n") + "\n")
    if command == "project-create":
        project = response["data"]["project_id"]
        project_dir = project_vault(root) / "01-Projects" / project
        for name in ("Tasks", "Research", "Recommendations", "Decisions", "Reviews", "Reports"):
            verify_managed(root, project_dir / name)
            (project_dir / name).mkdir(parents=True, exist_ok=True)
        project_file = project_dir / "Project.md"
        verify_managed(root, project_file)
        try:
            with project_file.open("x", encoding="utf-8", newline="\n") as out:
                out.write("# " + request["title"].replace("\n", " ") + "\n\nAdd project context here.\n")
        except FileExistsError:
            pass


def execute(root, command, data):
    """Execute a mutation atomically, with idempotency and an audit event."""
    settings = load_settings(root)
    require(not (root / "config" / "per-project.json").exists(), "select a project root; use project-init with the installation root to create a project")
    validate_input(data)
    db = sqlite3.connect(settings["db"], timeout=settings["busy_timeout_ms"] / 1000, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(f"PRAGMA busy_timeout={settings['busy_timeout_ms']}")
    try:
        migrate(db, root)
        if command in READS:
            return read(db, root, command, data)
        if command in UNLOGGED:
            # Renewals are safe to repeat: no op_id needed, and no event or operations row (they were half the log).
            db.execute("BEGIN IMMEDIATE")
            result = mutate(db, root, settings, command, data)
            db.execute("COMMIT")
            auto_wake(db, settings, data.get("session_id"))
            return {"ok": True, "data": result}
        if command in AUTO_OP and "op_id" not in data:
            data = {**data, "op_id": new_id()}
        require(isinstance(data.get("op_id"), str), "op_id required", "INVALID_INPUT", 2, NEXT["op_id"])
        try:
            parsed = uuid.UUID(data["op_id"])
            require(parsed.version == 4 and str(parsed) == data["op_id"], "op_id must be canonical UUIDv4", "INVALID_INPUT", 2, NEXT["op_id"])
        except (ValueError, AttributeError):
            raise MeshError("INVALID_INPUT", "op_id must be canonical UUIDv4", 2, {"next_action": NEXT["op_id"]})
        digest = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()
        if command in ("sync", "backup"):
            fields(data, "op_id")
            from .projections import build, projection_lock
            with projection_lock(root):
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT * FROM operations WHERE op_id=?", (data["op_id"],)).fetchone()
                if previous:
                    require(previous["command"] == command and previous["input_hash"] == digest, "op_id already used for different input", next_action=NEXT["op_reuse"])
                    response = json.loads(previous["response_json"])
                else:
                    sequence = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                    if command == "sync":
                        response = {"ok": True, "data": {"through_seq": sequence}}
                    else:
                        backup_file = root / "backups" / (data["op_id"] + ".db")
                        require(not backup_file.exists(), "backup path already exists")
                        response = {"ok": True, "data": {"path": str(backup_file), "through_seq": sequence}}
                    db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (data["op_id"], command, digest, canonical(response), now()))
                db.execute("COMMIT")
                if command == "sync":
                    db.execute("BEGIN")
                    build(db, root)
                    db.execute("COMMIT")
                else:
                    import os
                    target = Path(response["data"]["path"])
                    if not target.exists():
                        temp = target.with_suffix(".db.partial")
                        require(not temp.exists(), "partial backup exists; inspect it before retrying")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with temp.open("xb"):
                            pass
                        destination = sqlite3.connect(temp)
                        try:
                            db.backup(destination)
                            require(destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "backup integrity check failed")
                        finally:
                            destination.close()
                        os.replace(temp, target)
                    require(not target.is_symlink() and not getattr(target, "is_junction", lambda: False)(), "backup path is a reparse point")
                    check = sqlite3.connect(str(target))
                    try:
                        require(check.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "backup integrity check failed")
                    finally:
                        check.close()
                return response
        pre = None
        if command in PREPARED and not db.execute("SELECT 1 FROM operations WHERE op_id=?", (data["op_id"],)).fetchone():
            pre = prepare(db, root, settings, command, data)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM operations WHERE op_id=?", (data["op_id"],)).fetchone()
        if previous:
            require(previous["command"] == command and previous["input_hash"] == digest, "op_id already used for different input", next_action=NEXT["op_reuse"])
            db.execute("COMMIT")
            response = json.loads(previous["response_json"])
            finish_files(root, command, data, response)
            return after(root, command, data, response)
        halted(db, data, command)
        result = mutate(db, root, settings, command, data, pre)
        if command != "activity-log":
            event(db, data, command, "success")
        if isinstance(data.get("session_id"), str) and command not in ("membership-set", "session-close"):
            touch(db, settings, data["session_id"], now())
        response = {"ok": True, "data": result}
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (data["op_id"], command, digest, canonical(response), now()))
        db.execute("COMMIT")
        finish_files(root, command, data, response)
        response = after(root, command, data, response)
        auto_wake(db, settings, data.get("session_id"))
        return response
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def project_status_of(db, project):
    row = db.execute("SELECT status FROM projects WHERE id=?", (project,)).fetchone()
    return row["status"] if row else "missing"


def task_detail(db, root, data):
    fields(data, "project_id task_id session_id" if "session_id" in data else "project_id task_id")
    project = data["project_id"]
    task = db.execute("SELECT * FROM tasks WHERE project_id=? AND id=?", (project, data["task_id"])).fetchone()
    require(task is not None, "task not found in project", next_action="Copy the task id exactly from compact status for this project_id.")
    at = now()
    integrated = integrations(db, project)
    dependencies = db.execute("SELECT t.id,t.title,t.state FROM dependencies d JOIN tasks t ON t.id=d.depends_on_id WHERE d.task_id=? ORDER BY t.id", (task["id"],)).fetchall()
    result = {"task": dict(task), "dependencies": [dict(x, integrated=x["id"] in integrated) for x in dependencies], "as_of": at}
    rows = db.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY generation DESC", (task["id"],)).fetchall()
    pg = project_git(db, project)
    result["git"] = {"repository": pg["repository"], "base_branch": pg["base_branch"]} if pg else None
    history = []
    for index, row in enumerate(rows[:5]):
        item = {"attempt_id": row["id"], "generation": row["generation"], "state": row["state"], "session_id": row["session_id"],
                "updated_at": row["updated_at"], **worktree_info(root, project, task["id"], row["id"])}
        ag = attempt_git(db, row["id"])
        if ag:
            item.update(start_ref=ag["start_ref"], base_sha=ag["base_sha"], head_sha=ag["head_sha"])
            if ag["head_sha"] and pg is not None:
                item["diff_command"] = f"git -C \"{pg['repository']}\" diff {pg['base_branch']}...{ag['head_sha']}"
        if index == 0:
            item["checkpoint"] = json.loads(row["checkpoint_json"])
            item["manifest"] = json.loads(row["manifest_json"]) if row["manifest_json"] else None
        history.append(item)
    result["attempts"], result["attempts_total"] = history, len(rows)
    review = db.execute("SELECT r.* FROM reviews r JOIN attempts a ON a.id=r.attempt_id WHERE a.task_id=? ORDER BY r.created_at DESC,r.id DESC LIMIT 1", (task["id"],)).fetchone()
    result["latest_review"] = None if review is None else {"review_id": review["id"], "attempt_id": review["attempt_id"], "outcome": review["outcome"],
                                                           "evidence": review["evidence"], "reviewer_session_id": review["reviewer_session_id"], "at": review["created_at"]}
    result["integration"] = integrated.get(task["id"])
    if "session_id" in data:
        found, srow = session_blockers(db, data["session_id"], project, at)
        found += task_blockers(db, task, project_status_of(db, project), srow, data["session_id"], at)
        result["eligibility"] = {"claimable": not found, "blockers": found,
                                 "next_action": BLOCKERS[found[0]["code"]][1] if found else "Claim it with task-claim and a new op_id; the claim re-checks everything."}
    return {"ok": True, "data": result}


def compact_status(db, root, data, s, l, tasks, at):
    project, sid = data["project_id"], data["session_id"]
    found, srow = session_blockers(db, sid, project, at)
    pstatus = project_status_of(db, project)
    integrated = integrations(db, project)
    outcomes = {}
    for row in db.execute("SELECT a.task_id,r.outcome FROM reviews r JOIN attempts a ON a.id=r.attempt_id JOIN tasks t ON t.id=a.task_id WHERE t.project_id=? ORDER BY r.created_at,r.id", (project,)).fetchall():
        outcomes[row["task_id"]] = row["outcome"]
    latest = {}
    for row in db.execute("SELECT a.* FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE t.project_id=? ORDER BY a.task_id,a.generation", (project,)).fetchall():
        latest[row["task_id"]] = row
    workers = live_worker_capabilities(db, project, at)
    items = []
    for row in tasks:
        item = task_summary(row)
        blockers = task_blockers(db, row, pstatus, srow, sid, at)
        item["claimable"] = not found and not blockers
        item["blockers"] = [b["code"] for b in blockers]
        if row["state"] == "ready" and outcomes.get(row["id"]) == "revision":
            item["revision_requested"] = True
        if row["state"] == "completed":
            item["integrated"] = row["id"] in integrated
        if row["state"] == "ready":
            item["capable_workers"] = capable_count(workers, json.loads(row["required_capabilities_json"]))
        if row["state"] == "submitted" and row["current_attempt_id"]:
            ag = attempt_git(db, row["current_attempt_id"])
            if ag and ag["head_sha"]:
                item["head_sha"] = ag["head_sha"]
        last = latest.get(row["id"])
        if last is not None:
            saved = json.loads(last["checkpoint_json"])
            if row["state"] in ("claimed", "in_progress") and row["current_attempt_id"] == last["id"]:
                # What the leader needs on every reminder: is the attempt alive, and has the worker said anything?
                info = {"session_id": last["session_id"], **lease_view(last["lease_until"], at), "last_checkpoint_at": saved.get("at")}
                for key, name in (("note", "worker_note"), ("question", "question"), ("blocker", "blocker_flag")):
                    if key in saved:
                        info[name] = saved[key]
                reply = saved.get("leader_reply")
                if isinstance(reply, dict):
                    info["leader_reply_at"], info["leader_reply_seen"] = reply.get("at"), bool(reply.get("seen_at"))
                    answered = reply.get("answered")
                    info["question_answered"] = isinstance(answered, dict) and answered == {"question": saved.get("question"), "blocker": saved.get("blocker")}
                item["attempt"] = info
            elif row["current_attempt_id"] is None and last["state"] == "abandoned" and isinstance(saved.get("released"), dict):
                item["released"] = {**saved["released"], "by_session_id": last["session_id"], "attempt_id": last["id"]}
        items.append(item)
    mine = db.execute("SELECT a.* FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE a.session_id=? AND t.project_id=? AND a.state IN ('claimed','in_progress') ORDER BY a.created_at DESC LIMIT 1", (sid, project)).fetchone()
    active = None
    if mine is not None:
        active = {"attempt_id": mine["id"], "task_id": mine["task_id"], "generation": mine["generation"], "state": mine["state"],
                  "lease_until": mine["lease_until"], **worktree_info(root, project, mine["task_id"], mine["id"]), **lease_view(mine["lease_until"], at)}
        reply = json.loads(mine["checkpoint_json"]).get("leader_reply")
        if isinstance(reply, dict):
            active["leader_reply"] = reply
    counts = {row["state"]: row["n"] for row in db.execute("SELECT state,COUNT(*) AS n FROM tasks WHERE project_id=? GROUP BY state ORDER BY state", (project,)).fetchall()}
    leads = l is not None and l["session_id"] == sid and lease_view(l["lease_until"], at)["lease_state"] == "valid"
    session_codes = {b["code"] for b in found}
    attempts = [x for x in items if "attempt" in x]
    expired = [x for x in attempts if x["attempt"]["lease_state"] == "expired"]
    asking = [x for x in attempts if (x["attempt"].get("question") or x["attempt"].get("blocker_flag"))
              and (x["attempt"].get("leader_reply_at") or "") < (x["attempt"].get("last_checkpoint_at") or "")
              and not x["attempt"].get("question_answered")]
    released = [x for x in items if x["state"] == "blocked" and "released" in x]
    # Only a real mismatch (live workers exist, none qualifies) is worth a hint; no live worker at all is the normal wait.
    orphaned = [x for x in items if x.get("capable_workers") == 0] if workers else []
    if session_codes & {"session_missing", "session_closed"} or ("session_expired" in session_codes and not active):
        hint = BLOCKERS["session_expired"][1]
    elif active and (active["lease_state"] != "valid" or "session_expired" in session_codes):
        hint = (f"Your attempt lease expired but attempt {active['attempt_id']} is still yours (not recovered): run heartbeat "
                "with attempt_id and generation now, then continue. If heartbeat fails, the leader recovered it: stop and keep your files.")
    elif active and "leader_reply" in active and not active["leader_reply"].get("seen_at"):
        hint = ("The leader replied on your attempt: read my_active_attempt.leader_reply.text and act on it, "
                "then send checkpoint with reply_seen true.")
    elif active:
        hint = (f"Continue attempt {active['attempt_id']} in its worktree; heartbeat before {active['lease_until']}; finish with task-submit. "
                "If you cannot continue, run attempt-release with a reason (blocker true if the leader must act) instead of just ending your turn.")
    elif leads and expired:
        hint = (f"The attempt on task {expired[0]['id']} has an expired lease (its worker may have stopped). "
                "Run recover with a reason, then log that the task is ready again.")
    elif leads and asking:
        x = asking[0]
        hint = (f"The worker on task {x['id']} asks or is blocked (tasks[].attempt.question / blocker_flag / worker_note). "
                "Answer with attempt-reply.")
    elif leads and released:
        hint = (f"A worker released task {released[0]['id']} as blocked: {released[0]['released']['reason']!r}. "
                "Fix the brief with task-spec, then task-unblock; or task-cancel it.")
    elif leads and counts.get("submitted"):
        hint = "Review the submitted task(s) with review (list them with state submitted)."
    elif leads and any(row["id"] not in integrated for row in db.execute("SELECT id FROM tasks WHERE project_id=? AND state='completed'", (project,)).fetchall()):
        hint = "Completed tasks are not integrated. If a task delivers files to the application folder, integrate, verify there, then record task-integrate."
    elif any(x["claimable"] for x in items):
        first = next(x for x in items if x["claimable"])
        hint = f"Read task-detail for {first['id']} with your session_id, then claim it with task-claim and a new op_id."
    elif leads and orphaned:
        hint = (f"Ready task {orphaned[0]['id']} has capable_workers 0: no live worker has its required_capabilities. "
                "If the requirement is wrong, task-cancel it and publish a corrected task; otherwise wait for a suitable worker.")
    elif leads and (idle := idle_workers(db, project, at)):
        hint = (f"Worker {idle[0]['label']} ({idle[0]['session_id']}) is idle and nothing is claimable for it while tasks are unfinished. "
                "Add an independent task now (leader skill C1, O4): task-create + task-publish, no depends_on, only files no running task owns "
                "(tests against the contract, fixtures, edge cases, docs). If no useful independent work exists, wait.")
    elif (not leads and l is not None and leader_view(db, l, at)["slot_free"]
          and db.execute("SELECT 1 FROM memberships WHERE session_id=? AND project_id=? AND role='leader-candidate'", (sid, project)).fetchone()):
        hint = ("The leader slot is free (status leader.effective_status). Tell the user. Run leader-acquire only if the user "
                "named you leader; never take the role on your own.")
    else:
        hint = ("No action available now. Keep your reminder armed: if it is one-shot, re-arm it now, even if every task is completed "
                "(rulebook R5.5). Wait for the next reminder or user message; do not poll in a loop.")
    alert = None
    if l is not None and not leads:
        state = leader_view(db, l, at)["effective_status"]
        if state in LEADER_ALERTS:
            # Separate from next_action on purpose: a worker with an active attempt keeps working and submitting.
            alert = {"state": state, "message": LEADER_ALERTS[state] + " Tell the user once (not on every reminder) that "
                     "reviews, merges, answers and new tasks wait until a leader is back. Keep working: finish and submit as normal."}
    return {"project_status": pstatus, "session_blockers": found, "my_active_attempt": active, "counts": counts, "leader_alert": alert,
            "tasks": items, "next_action": hint}


def status_data(db, root, data, compact, include_done=False):
    project = data["project_id"]
    s = db.execute("SELECT * FROM sessions WHERE id=?", (data["session_id"],)).fetchone()
    membership = db.execute("SELECT 1 FROM memberships WHERE session_id=? AND project_id=?", (data["session_id"], project)).fetchone()
    require(s is not None and membership is not None, "project membership required", next_action=NEXT["membership"])
    l = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (project,)).fetchone()
    # Work first: claimable and active tasks must never fall outside the 100-task window behind finished ones.
    # Compact status leaves finished tasks out (counts still covers them) unless include_done is true.
    done = "" if not compact or include_done else " AND state NOT IN ('completed','cancelled','rejected')"
    tasks = db.execute("SELECT * FROM tasks WHERE project_id=?" + done + " ORDER BY " + STATE_ORDER + ",priority DESC,id LIMIT 100", (project,)).fetchall()
    events = [] if compact else db.execute("SELECT * FROM events WHERE project_id=? ORDER BY seq DESC LIMIT 20", (project,)).fetchall()
    at = now()
    result = {"as_of": at, "session": session_view(s, at, db), "leader": leader_view(db, l, at)}
    if result["leader"] is not None and db.execute("SELECT 1 FROM memberships WHERE session_id=? AND project_id=? AND role='leader-candidate'", (data["session_id"], project)).fetchone():
        # Leaders and candidates see the newest handoff note; workers do not need it.
        result["leader"]["last_handoff"] = last_handoff(db, project)
    if compact:
        result.update(compact_status(db, root, data, s, l, tasks, at))
        result["tasks_may_be_truncated"] = len(tasks) == 100
    else:
        result["tasks"] = [dict(x) for x in tasks]
        result["recent_events"] = [dict(x) for x in events]
    reason = halt_reason(db)
    if reason is not None:
        result["halt"] = reason
        if compact:
            result["next_action"] = HALT_HINT.format(reason=reason)
    return result


def read(db, root, command, data):
    if command == "task-detail":
        return task_detail(db, root, data)
    if command == "whoami":
        return {"ok": True, "data": whoami(db, data)}
    if command == "metrics":
        return {"ok": True, "data": metrics(db, data)}
    if command == "status":
        compact = data.get("view") == "compact"
        fields(data, " ".join(["session_id", "project_id"] + [k for k in ("view", "include_done") if k in data]))
        require("include_done" not in data or type(data["include_done"]) is bool, "include_done must be true or false", "INVALID_INPUT", 2)
        require("view" not in data or compact, "invalid view", "INVALID_INPUT", 2)
        return {"ok": True, "data": status_data(db, root, data, compact, data.get("include_done", False))}
    compact = data.get("view") == "compact"
    fields(data, "project_id kind cursor limit view" if "view" in data else "project_id kind cursor limit")
    require("view" not in data or compact and data.get("kind") == "tasks", "invalid view", "INVALID_INPUT", 2)
    kind = data["kind"]
    require(kind in ("tasks", "sessions", "events", "reviews", "recommendations", "messages"), "invalid list kind", "INVALID_INPUT", 2)
    require(type(data["limit"]) is int and 1 <= data["limit"] <= 100, "invalid limit", "INVALID_INPUT", 2)
    require(data["cursor"] is None or isinstance(data["cursor"], (str, int)), "invalid cursor", "INVALID_INPUT", 2)
    key = "seq" if kind == "events" else "id"
    project = data["project_id"]
    if kind == "sessions":
        sql = "SELECT DISTINCT s.* FROM sessions s JOIN memberships m ON m.session_id=s.id WHERE m.project_id=? AND s.id>? ORDER BY s.id LIMIT ?"
    elif kind == "reviews":
        sql = "SELECT r.* FROM reviews r JOIN attempts a ON a.id=r.attempt_id JOIN tasks t ON t.id=a.task_id WHERE t.project_id=? AND r.id>? ORDER BY r.id LIMIT ?"
    else:
        sql = f"SELECT * FROM {kind} WHERE project_id=? AND {key}>? ORDER BY {key} LIMIT ?"
    rows = db.execute(sql, (project, data["cursor"] or (0 if key == "seq" else ""), data["limit"] + 1)).fetchall()
    more = len(rows) > data["limit"]
    rows = rows[:data["limit"]]
    at = now()
    items = [session_view(x, at, db) if kind == "sessions" else task_summary(x) if compact else dict(x) for x in rows]
    return {"ok": True, "data": {"items": items, "next_cursor": rows[-1][key] if more else None, "as_of": at}}


def task_summary(row):
    return {key: row[key] for key in ("id", "project_id", "title", "state", "priority", "version", "reservation_session_id", "reservation_until", "current_attempt_id")}


# ---- Appointment codes (plan 0.3.2): the user's choice of a trusted role, in a form the CLI can check ----

RULEBOOK_VERSION = "3.0"
ROLE_JOINS = {"leader-join": ("leader", "leader-candidate"), "reviewer-join": ("reviewer", "reviewer"), "integrator-join": ("integrator", "integrator")}
CODE_ROLES = {"leader-candidate": "leader", "reviewer": "reviewer", "integrator": "integrator"}
CODE_PREFIX = {"leader": "LEAD", "reviewer": "REVW", "integrator": "INTG"}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_MINUTES = 15
APPOINT_HINT = ("This role needs an appointment code from the user. Tell the user to run, in their own terminal window (not through you): "
                "agentmesh --root <project root> appoint {role}. Then join with {role}-join and the code the user gives you. "
                "Never guess a code or reuse one meant for another chat.")
APPROVAL_SECONDS = 90


def ask_user_approval(caption, text):
    """Yes/No popup on the user's desktop: "yes", "no", "timeout" or "unavailable". No is the default button,
    so a stray Enter never approves; no answer within APPROVAL_SECONDS counts as no. Tests replace this function.
    AGENTMESH_NO_POPUP=1 (set by the test suite) only ever refuses, so it opens no way around the user."""
    import os
    if os.name != "nt" or os.environ.get("AGENTMESH_NO_POPUP") == "1":
        return "unavailable"
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        flags = 0x4 | 0x20 | 0x100 | 0x1000 | 0x10000 | 0x40000  # YESNO ICONQUESTION DEFBUTTON2 SYSTEMMODAL SETFOREGROUND TOPMOST
        box = getattr(user32, "MessageBoxTimeoutW", None)
        if box is not None:
            box.restype = ctypes.c_int
            box.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT, wintypes.WORD, wintypes.DWORD)
            answer = box(None, text, caption, flags, 0, APPROVAL_SECONDS * 1000)
        else:
            user32.MessageBoxW.restype = ctypes.c_int
            user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
            answer = user32.MessageBoxW(None, text, caption, flags)
    except (OSError, AttributeError, ValueError):
        return "unavailable"
    return {6: "yes", 7: "no", 32000: "timeout"}.get(answer, "unavailable")


def approve_join(db, cfg, command, d):
    """A role join without a code: check the input, then ask the user with a popup. Runs before the write
    transaction, so other agents are not blocked while the popup waits. Returns {"approved": "popup"} or raises."""
    if "appointment_code" in d or not cfg["require_appointment_codes"]:
        return None
    role_defaults(command, d)
    check_rulebook(d, required=True)
    project_exists(db, d["project_id"])
    require(d["tool"] in TOOLS and d["label"].strip(), "invalid session details", "INVALID_INPUT", 2)
    role = ROLE_JOINS[command][0]
    host = host_identity()
    where = f"{host['name']} (process {host['pid']})" if host else "a program AgentMesh does not recognise"
    text = (f"An agent asks to become {role} of AgentMesh project {d['project_id']}.\n\n"
            f"Tool: {d['tool']}\nLabel: {d['label'].strip()[:80]}\nRunning in: {where}\n\n"
            f"Click Yes only if you just asked this chat to be the {role}.\n"
            f"No answer within {APPROVAL_SECONDS} seconds counts as No.")
    answer = ask_user_approval(f"AgentMesh: approve {role}?", text)
    if answer == "yes":
        return {"approved": "popup"}
    if answer == "no":
        raise MeshError("CONFLICT", f"the user declined this {role} request", 3,
                        {"next_action": "Stop. Tell the user the request was declined. Do not retry unless the user asks you to."})
    if answer == "timeout":
        raise MeshError("CONFLICT", f"the user did not answer the approval popup within {APPROVAL_SECONDS} seconds", 3,
                        {"next_action": "Tell the user an approval popup will appear, then retry once with a new op_id."})
    raise MeshError("CONFLICT", "the approval popup could not be shown", 3, {"next_action": APPOINT_HINT.format(role=role)})


def role_defaults(command, d):
    """A role join needs only project_id and rulebook_version (0.5.6), like worker-join since 0.5.4: a leader in the
    pocket-sql run wrote the join from memory and missed a field. tool is detected, label is <tool>-<role>, capabilities []."""
    fields(d, " ".join(["op_id project_id rulebook_version"] + [k for k in ("tool", "label", "capabilities", "appointment_code") if k in d]))
    d.setdefault("tool", detect_tool())
    d.setdefault("label", f"{d['tool']}-{ROLE_JOINS[command][0]}")
    d.setdefault("capabilities", [])


def code_hash(code):
    return hashlib.sha256(code.strip().upper().replace(" ", "").encode("utf-8")).hexdigest()


def new_code(role):
    import secrets
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{CODE_PREFIX[role]}-{body[:4]}-{body[4:]}"


def create_appointment(db, project, role, timestamp=None):
    """Store a one-time code (only its hash) and return the code. Called by the operator command `appoint`."""
    require(role in CODE_PREFIX, f"role must be one of {sorted(CODE_PREFIX)}", "INVALID_INPUT", 2)
    timestamp = timestamp or now()
    code = new_code(role)
    expires = (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + timedelta(minutes=CODE_MINUTES)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    db.execute("INSERT INTO appointments VALUES(?,?,?,?,?,NULL,NULL)", (code_hash(code), project, role, timestamp, expires))
    return code, expires


def redeem(db, project, role, code, sid, timestamp):
    row = db.execute("SELECT * FROM appointments WHERE code_hash=?", (code_hash(code),)).fetchone()
    retry = APPOINT_HINT.format(role=role) + " Ask the user for a new code."
    require(row is not None and row["project_id"] == project and row["role"] == role, "appointment code is not valid for this project and role", next_action=retry)
    require(row["redeemed_by"] is None, "appointment code was already used", next_action=retry)
    require(row["expires_at"] > timestamp, "appointment code expired", next_action=retry)
    db.execute("UPDATE appointments SET redeemed_by=?,redeemed_at=? WHERE code_hash=?", (sid, timestamp, row["code_hash"]))


def installation_base(root):
    scope = project_scope(root)
    return scope[1] if scope else root


def rulebook_path(root):
    return installation_base(root) / "vault" / "04-Skills" / "agent-mesh" / "SKILL.md"


def rulebook_warnings(root):
    """rulebook_modified when the rulebook no longer matches the hash the user sealed with `rulebook-seal`."""
    seal = installation_base(root) / "config" / "rulebook.sha256"
    book = rulebook_path(root)
    try:
        if seal.is_file() and book.is_file() and hashlib.sha256(book.read_bytes()).hexdigest() != seal.read_text(encoding="utf-8").strip():
            return [{"code": "rulebook_modified", "message": "The rulebook changed since the user sealed it. Tell the user once; follow the user's instructions."}]
    except OSError:
        pass
    return []


def check_rulebook(d, required):
    if "rulebook_version" not in d:
        require(not required, "rulebook_version required", "INVALID_INPUT", 2, f"Read the rulebook, then pass \"rulebook_version\": \"{RULEBOOK_VERSION}\".")
        return [{"code": "rulebook_version_missing", "message": f"Pass rulebook_version {RULEBOOK_VERSION} after reading the rulebook."}]
    require(d["rulebook_version"] == RULEBOOK_VERSION, f"your rules are out of date (you read {d['rulebook_version']}, current {RULEBOOK_VERSION})", "INVALID_INPUT", 2,
            f"Read the rulebook again, then join with rulebook_version {RULEBOOK_VERSION} and a new op_id.")
    return []


def project_exists(db, project):
    if db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone() is None:
        known = [x[0] for x in db.execute("SELECT id FROM projects ORDER BY id").fetchall()]
        raise MeshError("PROJECT_NOT_FOUND", "project_id not found in this project root", 2,
                        {"known_projects": known, "next_action": "Use the project ID, not the display name: "
                         + (f"this root holds project_id {known[0]!r}." if len(known) == 1 else f"known IDs here: {known}.")
                         + " Retry with a new op_id."})


def take_leader_slot(db, cfg, project, sid, timestamp, row):
    """Give the project's single leader slot to sid with a new generation; the old owner gets an ordinary lease back."""
    if row is not None and row["session_id"] != sid:
        db.execute("UPDATE sessions SET lease_until=? WHERE id=? AND lease_until=?", (later(cfg["session_lease_seconds"]), row["session_id"], HELD))
    generation = 1 if row is None else row["generation"] + 1
    db.execute("INSERT INTO leader_leases VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET session_id=excluded.session_id,generation=excluded.generation,lease_until=excluded.lease_until", (project, sid, generation, HELD))
    db.execute("UPDATE sessions SET last_seen_at=?,lease_until=? WHERE id=?", (timestamp, HELD, sid))
    return generation


def role_join(db, root, cfg, command, d, timestamp, pre=None):
    """leader-join / reviewer-join / integrator-join: register a session, approved by the user's code or popup click, in one transaction."""
    code_role, member_role = ROLE_JOINS[command]
    coded = "appointment_code" in d
    role_defaults(command, d)
    if not coded:
        require(not cfg["require_appointment_codes"] or (pre or {}).get("approved") == "popup",
                f"becoming {code_role} needs the user's approval", next_action=APPOINT_HINT.format(role=code_role))
    check_rulebook(d, required=True)
    project_exists(db, d["project_id"])
    require(d["tool"] in TOOLS and isinstance(d["label"], str) and d["label"].strip(), "invalid session details", "INVALID_INPUT", 2)
    single_line(d, "label")
    caps = capability_list(d["capabilities"], "capabilities")
    sid = new_id()
    leading = code_role == "leader"
    lease = HELD if leading else later(cfg["session_lease_seconds"])
    db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (sid, d["tool"], d["label"], canonical(caps), "available", timestamp, timestamp, lease))
    db.execute("INSERT INTO memberships VALUES(?,?,?)", (sid, d["project_id"], member_role))
    if coded:
        redeem(db, d["project_id"], code_role, d["appointment_code"], sid, timestamp)
    d["approval"] = "appointment code" if coded else ("popup click" if pre else "codes switched off")
    d["session_id"] = sid
    result = {"session_id": sid, "role": member_role, "label": d["label"], "lease_until": lease, "warnings": rulebook_warnings(root)}
    if leading:
        row = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (d["project_id"],)).fetchone()
        if row is not None:
            view = leader_view(db, row, timestamp)
            d["reason"] = f"{d['approval']}: took generation {row['generation']} from session {row['session_id']} ({view['effective_status']})"
        result["generation"] = take_leader_slot(db, cfg, d["project_id"], sid, timestamp, row)
        d["host"] = host_identity()
        result["last_handoff"] = last_handoff(db, d["project_id"])
    return result


# ---- Git owned by the CLI (plan 0.3.3). Git never runs inside a write transaction. ----

PREPARED = ("project-git", "task-submit", "task-integrate", *ROLE_JOINS)


def git_error(exc, next_action):
    return MeshError("GIT_ERROR", str(exc), 3, {"next_action": next_action})


def project_git(db, project):
    return db.execute("SELECT * FROM project_git WHERE project_id=?", (project,)).fetchone()


def attempt_git(db, attempt_id):
    return db.execute("SELECT * FROM attempt_git WHERE attempt_id=?", (attempt_id,)).fetchone()


def can_integrate(db, d, project):
    """Read-only role check, done before any merge is run."""
    sid = d.get("session_id")
    if "leader_generation" in d:
        member(db, sid, project, {"leader-candidate"})
        row = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (project,)).fetchone()
        require(row is not None and row["session_id"] == sid and row["generation"] == d["leader_generation"] and lease_view(row["lease_until"], now())["lease_state"] == "valid",
                "not the current leader (released, taken over or fenced)", next_action=NEXT["leader"])
    else:
        member(db, sid, project, {"integrator"})


def prepare(db, root, cfg, command, d):
    """Read-only checks, Git work and the user's approval popup before the write transaction. Returns data for mutate, or None."""
    from . import gitops
    if command in ROLE_JOINS:
        return approve_join(db, cfg, command, d)
    try:
        if command == "project-git":
            repo = d.get("repository")
            require(isinstance(repo, str) and Path(repo).is_absolute() and Path(repo).is_dir(), "repository must be an existing absolute folder", "INVALID_INPUT", 2)
            try:
                top = gitops.toplevel(repo)
            except gitops.GitError:
                raise MeshError("CONFLICT", "the folder is not a Git repository", 3, {"next_action":
                                "Ask the user to create the repository and make the first commit (show them `git status --short` and the .gitignore first). Do not run git init yourself."})
            require(top == Path(repo).resolve(), f"the folder is not the top of its own Git repository (it lies inside {top})", next_action=
                    "Ask the user which repository to use: the top folder above, or a new repository in this folder that the user creates. Do not run git init yourself.")
            base_sha = gitops.rev(repo, d.get("base_branch") or "")
            require(base_sha is not None, "base_branch does not exist in the repository", "INVALID_INPUT", 2)
            require(not gitops.dirty(repo), "the repository has uncommitted changes", next_action="Ask the user to commit or stash them first.")
            return {"repository": str(top), "base_sha": base_sha}
        if command == "task-submit":
            row = db.execute("SELECT a.*,t.project_id FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (d.get("attempt_id"),)).fetchone()
            pg = project_git(db, row["project_id"]) if row else None
            if pg is None:
                return None
            path = Path(worktree_info(root, row["project_id"], row["task_id"], row["id"])["worktree"])
            require(path.is_dir(), "the attempt worktree is missing", next_action=f"Create it as task-claim described ({path}), commit your work there, then submit again with a new op_id.")
            require(not gitops.dirty(path), "the worktree has uncommitted changes", next_action="Commit all work in the worktree, then submit again with the same input and a new op_id.")
            head = gitops.rev(path, "HEAD")
            ag = attempt_git(db, row["id"])
            require(not (ag and ag["base_sha"] and head == ag["base_sha"]), "no commit on this attempt's branch", next_action="Commit your work on the task branch, then submit again with a new op_id.")
            return {"head_sha": head}
        if command == "task-integrate":
            project = d.get("project_id")
            task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d.get("task_id"), project)).fetchone()
            pg = project_git(db, project)
            if task is None or pg is None or task["state"] != "completed":
                return None
            ag = attempt_git(db, task["current_attempt_id"])
            if not ag or not ag["head_sha"]:
                return None
            can_integrate(db, d, project)
            repo, base, head = pg["repository"], pg["base_branch"], ag["head_sha"]
            branch = worktree_info(root, project, task["id"], task["current_attempt_id"])["branch"]
            if d.get("merge") is True:
                require(gitops.current_branch(repo) == base, f"the repository must have {base} checked out", next_action=f"Ask the user to check out {base} in {repo}.")
                require(not gitops.dirty(repo), "the repository has uncommitted changes", next_action="Ask the user to commit or stash them; do not merge over them.")
                merge_sha = gitops.rev(repo, base) if gitops.is_ancestor(repo, head, base) else gitops.merge(repo, head, f"AgentMesh: merge task {task['id']} ({task['title']})")
            else:
                require(gitops.is_ancestor(repo, head, base), "the reviewed commit is not in the base branch yet",
                        next_action=f"Merge it with task-integrate and \"merge\": true, or run: git -C \"{repo}\" merge --no-ff {head}")
                merge_sha = None
            extra = gitops.commits_between(repo, head, branch) if gitops.rev(repo, "refs/heads/" + branch) else []
            merged = [c for c in extra if gitops.is_ancestor(repo, c, base)]
            require(not merged, "unreviewed commits were merged into the base branch", next_action="Tell the user. The commits after the reviewed one need a review: " + ", ".join(merged))
            return {"head_sha": head, "merge_sha": merge_sha, "extra_commits": extra, "repository": repo}
    except gitops.GitError as exc:
        raise git_error(exc, "Report the exact Git message to the user; do not work around it.")
    return None


def after(root, command, data, response):
    """Work after COMMIT that is safe to repeat on a retry: create the attempt worktree."""
    if command != "task-claim" or not response.get("ok") or not isinstance(response["data"].get("git"), dict):
        return response
    from . import gitops
    info = dict(response["data"]["git"])
    repo = info["repository"]
    try:
        start = info["start_ref"] if gitops.rev(repo, info["start_ref"]) else info["base_branch"]
        info["created"] = gitops.add_worktree(repo, response["data"]["worktree"], response["data"]["branch"], start)
        info["start_ref"], info["base_sha"] = start, gitops.rev(repo, start)
        db = sqlite3.connect(load_settings(root)["db"], timeout=5)
        try:
            db.execute("UPDATE attempt_git SET start_ref=?,base_sha=COALESCE(base_sha,?) WHERE attempt_id=?", (start, info["base_sha"], response["data"]["attempt_id"]))
            db.commit()
        finally:
            db.close()
    except (gitops.GitError, OSError, sqlite3.Error) as exc:
        info["created"], info["error"] = False, str(exc)
        info["next_action"] = "The claim is yours, but the worktree could not be created. Report the error to the user; do not create it elsewhere."
    out = json.loads(json.dumps(response))
    out["data"]["git"] = info
    return out


# ---- One call per reminder (plan 0.4.1), lost IDs (0.5.3), metrics (0.5.4), messages (0.5.1) ----

def roles_of(db, sid, project):
    return sorted(x[0] for x in db.execute("SELECT role FROM memberships WHERE session_id=? AND project_id=?", (sid, project)).fetchall())


def leads_now(db, sid, project):
    row = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (project,)).fetchone()
    if row is not None and row["session_id"] == sid and lease_view(row["lease_until"], now())["lease_state"] == "valid":
        return row["generation"]
    return None


def inbox(db, sid, project):
    return [dict(x) for x in db.execute("SELECT id,from_session_id,task_id,body,created_at FROM messages WHERE project_id=? AND to_session_id=? AND read_at IS NULL "
                                        "ORDER BY created_at LIMIT 20", (project, sid)).fetchall()]


def call_template(db, sid, project, view, generation):
    """A ready input for the command next_action names, with every known ID filled in (add op_id where the command needs one)."""
    active = view.get("my_active_attempt")
    base = {"session_id": sid}
    if view.get("halt"):
        return None
    if active:
        ids = {**base, "attempt_id": active["attempt_id"], "generation": active["generation"]}
        if active.get("lease_state") != "valid":
            return {"command": "heartbeat", "input": ids}
        return {"command": "checkpoint", "input": {**ids, "summary": "<progress>", "paths": []}}
    tasks = view.get("tasks", [])
    if generation is not None:
        lead = {**base, "project_id": project, "leader_generation": generation}
        for t in tasks:
            if t.get("attempt", {}).get("lease_state") == "expired":
                return {"command": "recover", "input": {**lead, "task_id": t["id"], "reason": "<why>"}}
        for t in tasks:
            if t["state"] == "submitted" and t.get("current_attempt_id"):
                item = {**base, "attempt_id": t["current_attempt_id"], "outcome": "accepted|revision|rejected|blocked", "evidence": "<findings>", "leader_generation": generation}
                if t.get("head_sha"):
                    item["reviewed_sha"] = t["head_sha"]
                return {"command": "review", "input": item}
        done = db.execute("SELECT t.id FROM tasks t LEFT JOIN integrations i ON i.task_id=t.id WHERE t.project_id=? AND t.state='completed' AND i.task_id IS NULL LIMIT 1", (project,)).fetchone()
        if done:
            item = {**lead, "task_id": done[0], "evidence": "<verification on the base branch>"}
            if project_git(db, project):
                item["merge"] = True
            return {"command": "task-integrate", "input": item}
    for t in tasks:
        if t.get("claimable"):
            return {"command": "task-claim", "input": {**base, "project_id": project, "task_id": t["id"], "start": True}}
    return None


def next_view(db, root, cfg, d):
    """One call per reminder: renews the caller's leases and returns what to do. Small when nothing changed since `since`."""
    fields(d, " ".join(["session_id", "project_id"] + [k for k in ("since",) if k in d]))
    require("since" not in d or type(d["since"]) is int, "since must be an integer (the seq from your previous next)", "INVALID_INPUT", 2)
    sid, project = d["session_id"], d["project_id"]
    row = db.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
    require(row is not None and row["status"] != "closed", "session missing or closed", next_action=NEXT["session"])
    require(db.execute("SELECT 1 FROM memberships WHERE session_id=? AND project_id=?", (sid, project)).fetchone(), "project membership required", next_action=NEXT["membership"])
    timestamp = now()
    touch(db, cfg, sid, timestamp)
    seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE project_id=?", (project,)).fetchone()[0]
    view = status_data(db, root, {"session_id": sid, "project_id": project}, compact=True)
    generation = leads_now(db, sid, project)
    mail = inbox(db, sid, project)
    lease = db.execute("SELECT lease_until FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    small = {"changed": False, "seq": seq, "next_action": view["next_action"], "lease_until": lease, "leader_generation": generation}
    if view.get("halt"):
        small["halt"] = view["halt"]
    if d.get("since") == seq and not mail and not (view.get("my_active_attempt") or {}).get("lease_state") == "expired":
        return small
    mine = view.get("my_active_attempt")
    keep = []
    for t in view["tasks"]:
        needs_leader = generation is not None and (t["state"] in ("submitted", "blocked") or t.get("attempt", {}).get("lease_state") == "expired"
                                                   or t.get("attempt", {}).get("question") or t.get("attempt", {}).get("blocker_flag") or t.get("capable_workers") == 0)
        if t["claimable"] or (mine and t["id"] == mine["task_id"]) or needs_leader:
            keep.append(t)
    out = {**small, "changed": True, "inbox": mail, "my_active_attempt": mine, "leader_alert": view["leader_alert"], "counts": view["counts"],
           "project_status": view["project_status"], "tasks": keep, "roles": roles_of(db, sid, project)}
    if generation is not None and view["leader"]:
        out["last_handoff"] = view["leader"].get("last_handoff")
    if generation is not None:
        out["workers"] = worker_list(db, project, timestamp)
        import os
        # Codex workers are left out only when this call wakes them itself (a Codex leader's calls skip auto-wake).
        wake = wake_list(db, project, auto=auto_wake_on(cfg) and not os.environ.get("CODEX_THREAD_ID"))
        if wake:
            out["wake"] = wake
    out["call"] = call_template(db, sid, project, {**view, "tasks": view["tasks"]}, generation)
    return out


UNFINISHED = ("ready", "claimed", "in_progress", "submitted", "in_review", "blocked")


def idle_workers(db, project, at):
    """For the leader's next_action (0.5.6): workers with no attempt and nothing they could claim while tasks are unfinished.
    In pocket-sql a worker sat idle behind a dependency chain until the user told the leader to add independent work.
    A Codex worker counts with an expired lease (it waits for a wake); other tools only while their lease is valid."""
    if not db.execute(f"SELECT 1 FROM tasks WHERE project_id=? AND state IN ({','.join('?' * len(UNFINISHED))}) LIMIT 1", (project, *UNFINISHED)).fetchone():
        return []
    idle = []
    for w in worker_list(db, project, at):
        if w["task_id"] is None and (w["lease"] == "valid" or w["tool"] == "codex") and not any(x.startswith("t:") for x in wake_news(db, w["session_id"], project, at)):
            idle.append(w)
    return idle


def worker_list(db, project, at):
    """For the leader (0.5.6): every worker that joined and is not closed. A pocket-sql leader took the in_progress
    count for the number of workers. lease 'expired' is normal for an idle Codex worker waiting to be woken."""
    rows = db.execute("SELECT s.id,s.label,s.tool,s.lease_until,(SELECT a.task_id FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id "
                      "WHERE a.session_id=s.id AND a.state IN ('claimed','in_progress') LIMIT 1) AS task_id FROM sessions s "
                      "JOIN memberships m ON m.session_id=s.id AND m.project_id=? AND m.role='worker' WHERE s.status!='closed' ORDER BY s.created_at", (project,)).fetchall()
    return [{"session_id": r["id"], "label": r["label"], "tool": r["tool"], "lease": lease_view(r["lease_until"], at)["lease_state"], "task_id": r["task_id"]} for r in rows]


def whoami(db, data):
    """Find this chat's session by its host process (name, pid, start time), for a chat that lost its IDs."""
    fields(data, "project_id")
    host = host_identity()
    require(host is not None, "unknown host: this chat's program is not one AgentMesh can recognise",
            next_action="Your IDs cannot be recovered automatically. If you were a worker, join again with worker-join; otherwise ask the user.")
    rows = db.execute("SELECT s.* FROM session_hosts h JOIN sessions s ON s.id=h.session_id JOIN memberships m ON m.session_id=s.id "
                      "WHERE h.name=? AND h.pid=? AND h.started=? AND m.project_id=? AND s.status!='closed' GROUP BY s.id ORDER BY h.seen_at DESC",
                      (host["name"], host["pid"], host["started"], data["project_id"])).fetchall()
    require(rows, "no session of this project was seen from this chat", next_action="Join again (worker-join, or ask the user for an appointment code).")
    found = []
    for s in rows:
        active = db.execute("SELECT a.id,a.task_id,a.generation,a.state FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE a.session_id=? AND a.state IN ('claimed','in_progress')", (s["id"],)).fetchone()
        found.append({"session_id": s["id"], "label": s["label"], "tool": s["tool"], "roles": roles_of(db, s["id"], data["project_id"]),
                      "leader_generation": leads_now(db, s["id"], data["project_id"]),
                      "active_attempt": {"attempt_id": active["id"], "task_id": active["task_id"], "generation": active["generation"], "state": active["state"]} if active else None})
    return {"host": {"name": host["name"], "pid": host["pid"]}, "sessions": found}


def minutes(a, b):
    try:
        return round((datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 60, 1)
    except (AttributeError, TypeError, ValueError):
        return None


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else round((values[mid - 1] + values[mid]) / 2, 1)


def metrics(db, data):
    """Numbers from the database only: where time goes, how often work is revised, how often it is recovered."""
    fields(data, "project_id")
    project = data["project_id"]
    first = {}
    for row in db.execute("SELECT task_id,action,MIN(at) AS at FROM events WHERE project_id=? AND result='success' AND task_id IS NOT NULL "
                          "AND action IN ('task-claim','task-submit','review','task-integrate') GROUP BY task_id,action", (project,)).fetchall():
        first.setdefault(row["task_id"], {})[row["action"]] = row["at"]
    tasks = []
    for t in db.execute("SELECT id,title,state FROM tasks WHERE project_id=? ORDER BY created_at", (project,)).fetchall():
        e = first.get(t["id"], {})
        attempts = db.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (t["id"],)).fetchone()[0]
        tasks.append({"task_id": t["id"], "title": t["title"], "state": t["state"], "attempts": attempts,
                      "claim_to_submit_min": minutes(e.get("task-claim"), e.get("task-submit")),
                      "submit_to_review_min": minutes(e.get("task-submit"), e.get("review")),
                      "review_to_integration_min": minutes(e.get("review"), e.get("task-integrate"))})
    by_tool = {}
    for row in db.execute("SELECT s.tool,r.outcome FROM reviews r JOIN attempts a ON a.id=r.attempt_id JOIN sessions s ON s.id=a.session_id "
                          "JOIN tasks t ON t.id=a.task_id WHERE t.project_id=?", (project,)).fetchall():
        item = by_tool.setdefault(row["tool"], {"reviews": 0, "revisions": 0})
        item["reviews"] += 1
        item["revisions"] += row["outcome"] == "revision"
    for item in by_tool.values():
        item["revision_rate"] = round(item["revisions"] / item["reviews"], 2)
    reads = [minutes(r["created_at"], r["read_at"]) for r in db.execute("SELECT created_at,read_at FROM messages WHERE project_id=? AND read_at IS NOT NULL", (project,)).fetchall()]
    count = lambda sql: db.execute(sql, (project,)).fetchone()[0]
    return {"tasks": tasks, "by_author_tool": by_tool,
            "totals": {"tasks": len(tasks), "completed": sum(t["state"] == "completed" for t in tasks),
                       "integrated": count("SELECT COUNT(*) FROM integrations i JOIN tasks t ON t.id=i.task_id WHERE t.project_id=?"),
                       "recoveries": count("SELECT COUNT(*) FROM events WHERE project_id=? AND action='recover'"),
                       "events": count("SELECT COUNT(*) FROM events WHERE project_id=?"),
                       "messages": count("SELECT COUNT(*) FROM messages WHERE project_id=?"),
                       "median_claim_to_submit_min": median(t["claim_to_submit_min"] for t in tasks),
                       "median_submit_to_review_min": median(t["submit_to_review_min"] for t in tasks),
                       "median_message_read_min": median(reads)},
            "as_of": now()}


def message_recipients(db, project, sender, to):
    if to == "leader":
        row = db.execute("SELECT session_id,lease_until FROM leader_leases WHERE project_id=?", (project,)).fetchone()
        require(row is not None and lease_view(row["lease_until"], now())["lease_state"] == "valid", "there is no current leader", next_action="Wait for a leader; tell the user once.")
        return [row["session_id"]]
    if to == "workers":
        at = now()
        rows = db.execute("SELECT DISTINCT s.id,s.status,s.lease_until FROM sessions s JOIN memberships m ON m.session_id=s.id WHERE m.project_id=? AND m.role='worker'", (project,)).fetchall()
        found = [r["id"] for r in rows if r["id"] != sender and r["status"] != "closed" and lease_view(r["lease_until"], at)["lease_state"] == "valid"]
        require(found, "no live worker to send to", next_action="Nobody would read it now; wait until a worker joins.")
        return found
    try:
        parsed = uuid.UUID(to)
    except ValueError:
        parsed = None
    require(parsed is not None and str(parsed) == to, "to must be a session_id, \"leader\" or \"workers\"", "INVALID_INPUT", 2)
    require(db.execute("SELECT 1 FROM memberships WHERE session_id=? AND project_id=?", (to, project)).fetchone(), "that session is not in this project")
    return [to]


def send_message(db, project, sender, recipients, body, task_id, timestamp):
    ids = []
    for target in recipients:
        mid = new_id()
        db.execute("INSERT INTO messages VALUES(?,?,?,?,NULL,?,?,?,NULL)", (mid, project, sender, target, task_id, body, timestamp))
        ids.append(mid)
    return ids


def import_plan(db, d, timestamp):
    items = d["tasks"]
    require(isinstance(items, list) and 1 <= len(items) <= 100 and type(d["publish"]) is bool, "tasks must be a list of 1 to 100 objects and publish true or false", "INVALID_INPUT", 2)
    names = "key title description category priority required_capabilities outputs acceptance depends_on"
    keys = {}
    for item in items:
        require(isinstance(item, dict), "each task must be an object", "INVALID_INPUT", 2)
        fields(item, names)
        require(isinstance(item["key"], str) and item["key"].strip() and item["key"] not in keys, "every key must be a unique non-empty string", "INVALID_INPUT", 2)
        require(all(isinstance(item[x], str) and item[x].strip() for x in ("title", "description", "category")) and type(item["priority"]) is int and 0 <= item["priority"] <= 100,
                f"invalid task {item['key']!r}", "INVALID_INPUT", 2)
        require(all(isinstance(item[x], list) and all(isinstance(y, str) and y for y in item[x]) for x in ("outputs", "acceptance", "depends_on")) and item["acceptance"],
                f"task {item['key']!r}: outputs, acceptance (non-empty) and depends_on must be lists of strings", "INVALID_INPUT", 2)
        single_line(item, "title", "category")
        keys[item["key"]] = new_id()
    # depends_on names a key of this import, or (0.5.6) the task_id of a task already in the project, so a plan too big
    # for one 64 KiB input can be imported in parts.
    existing = {r[0] for r in db.execute("SELECT id FROM tasks WHERE project_id=?", (d["project_id"],)).fetchall()}
    for item in items:
        for dep in item["depends_on"]:
            require((dep in keys or dep in existing) and dep != item["key"], f"task {item['key']!r} depends on unknown key {dep!r}", "INVALID_INPUT", 2,
                    "depends_on lists keys of this plan or task_ids of tasks already in this project.")
    # Cycle check over the keys (depth-first).
    state = {}

    def visit(key):
        require(state.get(key) != 1, f"dependency cycle through {key!r}", "INVALID_INPUT", 2)
        if state.get(key) == 2:
            return
        state[key] = 1
        for dep in next(i for i in items if i["key"] == key)["depends_on"]:
            if dep in keys:
                visit(dep)
        state[key] = 2
    for item in items:
        visit(item["key"])
    for item in items:
        db.execute("INSERT INTO tasks(id,project_id,title,description,category,priority,required_capabilities_json,outputs_json,acceptance_json,state,version,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
                   (keys[item["key"]], d["project_id"], item["title"], item["description"], item["category"], item["priority"],
                    canonical(capability_list(item["required_capabilities"], "required_capabilities")), canonical(item["outputs"]), canonical(item["acceptance"]),
                    "ready" if d["publish"] else "proposed", timestamp, timestamp))
    for item in items:
        for dep in item["depends_on"]:
            db.execute("INSERT INTO dependencies VALUES(?,?)", (keys[item["key"]], keys.get(dep, dep)))
    return {"task_ids": keys, "published": d["publish"]}


def feedback_dir(root, project):
    return installation_base(root) / "feedback" / project


def mutate(db, root, cfg, command, d, pre=None):
    timestamp = now()
    if command == "next":
        return next_view(db, root, cfg, d)
    if command == "message-ack":
        fields(d, "session_id project_id message_ids")
        session(db, d["session_id"], allow_expired=True)
        ids = d["message_ids"]
        require(isinstance(ids, list) and len(ids) <= 100 and all(isinstance(x, str) for x in ids), "message_ids must be a list of message ids", "INVALID_INPUT", 2)
        done = 0
        for mid in ids:
            done += db.execute("UPDATE messages SET read_at=? WHERE id=? AND to_session_id=? AND project_id=? AND read_at IS NULL", (timestamp, mid, d["session_id"], d["project_id"])).rowcount
        return {"acknowledged": done}
    if command == "message-send":
        fields(d, "op_id session_id project_id to task_id body")
        member(db, d["session_id"], d["project_id"], {"worker", "reviewer", "leader-candidate", "integrator"})
        require(d["body"].strip() and len(d["body"]) <= 2000, "body must be non-empty text of at most 2000 characters", "INVALID_INPUT", 2)
        if d["task_id"] is not None:
            require(db.execute("SELECT 1 FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone(), "task not in project")
        recipients = message_recipients(db, d["project_id"], d["session_id"], d["to"])
        return {"message_ids": send_message(db, d["project_id"], d["session_id"], recipients, d["body"], d["task_id"], timestamp), "recipients": recipients}
    if command == "plan-import":
        fields(d, "op_id session_id project_id leader_generation tasks publish")
        leader(db, d)
        result = import_plan(db, d, timestamp)
        d["summary"] = f"{len(result['task_ids'])} tasks" + (" published" if d["publish"] else "")
        return result
    if command == "feedback-add":
        fields(d, "op_id session_id project_id body")
        member(db, d["session_id"], d["project_id"], {"worker", "reviewer", "leader-candidate", "integrator"})
        require(d["body"].strip() and len(d["body"].splitlines()) <= 40 and len(d["body"]) <= 8000, "body must be non-empty, at most 40 lines", "INVALID_INPUT", 2)
        return {"path": str(feedback_dir(root, d["project_id"]) / (d["session_id"] + ".md"))}
    if command == "worker-join":
        # Only project_id is needed (0.5.4): agents kept guessing the other fields wrong on their first call.
        fields(d, " ".join(["op_id project_id"] + [k for k in ("tool", "label", "capabilities", "role", "rulebook_version") if k in d]))
        d.setdefault("role", "worker")
        d.setdefault("tool", detect_tool())
        d.setdefault("label", f"{d['tool']}-worker")
        d.setdefault("capabilities", [])
        require(d["role"] == "worker", "worker-join only grants the explicitly requested worker role", "INVALID_INPUT", 2)
        project_exists(db, d["project_id"])
        # Workers need no code (the user's appointment in the chat is the authorization); rulebook_version is checked if given.
        warnings = check_rulebook(d, required=False) + rulebook_warnings(root)
        require(d["tool"] in TOOLS and isinstance(d["label"], str) and d["label"].strip(), "invalid session details", "INVALID_INPUT", 2)
        single_line(d, "label")
        caps = capability_list(d["capabilities"], "capabilities")
        # Labels must tell live workers apart: suffix -2, -3, ... when a live session in this project already uses it.
        taken = {x["label"].casefold() for x in db.execute("SELECT DISTINCT s.label,s.status,s.lease_until FROM sessions s JOIN memberships m ON m.session_id=s.id WHERE m.project_id=?", (d["project_id"],)).fetchall()
                 if x["status"] != "closed" and lease_view(x["lease_until"], timestamp)["lease_state"] == "valid"}
        label, n = d["label"], 2
        while label.casefold() in taken:
            label, n = f"{d['label']}-{n}", n + 1
        sid, expiry = new_id(), later(cfg["session_lease_seconds"])
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (sid, d["tool"], label, canonical(caps), "available", timestamp, timestamp, expiry))
        db.execute("INSERT INTO memberships VALUES(?,?,?)", (sid, d["project_id"], "worker"))
        d["session_id"] = sid
        result = {"session_id": sid, "role": "worker", "tool": d["tool"], "lease_until": expiry, "label": label, "warnings": warnings}
        if label != d["label"]:
            result["label_note"] = f"label {d['label']!r} is used by a live session in this project; registered as {label!r}. Use this label in logs and messages."
        return result
    if command == "project-create":
        import re
        fields(d, "op_id project_id title")
        reserved = {"con", "prn", "aux", "nul"} | {f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10)}
        require(isinstance(d["project_id"], str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", d["project_id"]) and d["project_id"] not in reserved and isinstance(d["title"], str) and d["title"].strip(), "invalid project", "INVALID_INPUT", 2)
        scope = project_scope(root)
        require(scope is None or scope[0] == d["project_id"], "project does not match this database")
        db.execute("INSERT INTO projects VALUES(?,?,?,?)", (d["project_id"], d["title"], "active", timestamp))
        return {"project_id": d["project_id"]}
    if command == "session-register":
        fields(d, "op_id tool label capabilities")
        require(d["tool"] in TOOLS and isinstance(d["label"], str) and d["label"].strip(), "invalid session details", "INVALID_INPUT", 2)
        single_line(d, "label")
        caps = capability_list(d["capabilities"], "capabilities")
        sid, expiry = new_id(), later(cfg["session_lease_seconds"])
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (sid, d["tool"], d["label"], canonical(caps), "available", timestamp, timestamp, expiry))
        d["session_id"] = sid
        return {"session_id": sid, "lease_until": expiry}
    if command in ROLE_JOINS:
        return role_join(db, root, cfg, command, d, timestamp, pre)
    if command == "project-git":
        fields(d, "op_id session_id project_id leader_generation repository base_branch")
        leader(db, d)
        repo = pre or {}
        require(repo.get("repository"), "repository check missing")
        db.execute("INSERT OR REPLACE INTO project_git VALUES(?,?,?,?)", (d["project_id"], repo["repository"], d["base_branch"], timestamp))
        return {"project_id": d["project_id"], "repository": repo["repository"], "base_branch": d["base_branch"], "base_sha": repo["base_sha"]}
    if command == "membership-set":
        fields(d, "op_id session_id project_id role enabled")
        require(d["role"] in ("worker", "reviewer", "leader-candidate", "integrator") and type(d["enabled"]) is bool, "invalid membership", "INVALID_INPUT", 2)
        if cfg["require_appointment_codes"]:
            # Granting a trusted role, or taking any role away, is the user's decision: it needs an appointment code.
            require(not (d["enabled"] and d["role"] != "worker") and (d["enabled"] or d["role"] == "worker"),
                    f"the {d['role']} role needs an appointment code from the user",
                    next_action=APPOINT_HINT.format(role=CODE_ROLES.get(d["role"], d["role"])))
        if d["enabled"]:
            db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,?)", (d["session_id"], d["project_id"], d["role"]))
        else:
            db.execute("DELETE FROM memberships WHERE session_id=? AND project_id=? AND role=?", (d["session_id"], d["project_id"], d["role"]))
        return {"session_id": d["session_id"], "role": d["role"], "enabled": d["enabled"]}
    if command == "leader-acquire":
        fields(d, " ".join(["op_id session_id project_id"] + [k for k in ("takeover", "reason") if k in d]))
        require("takeover" not in d or type(d["takeover"]) is bool, "takeover must be true or false", "INVALID_INPUT", 2)
        require("reason" not in d or isinstance(d["reason"], str) and d["reason"].strip(), "reason must be a non-empty string", "INVALID_INPUT", 2)
        member(db, d["session_id"], d["project_id"], {"leader-candidate"})
        row = db.execute("SELECT * FROM leader_leases WHERE project_id=?", (d["project_id"],)).fetchone()
        view = leader_view(db, row, timestamp) if row is not None else None
        if (view is not None and row["session_id"] == d["session_id"]
                and view["effective_status"] in ("active", "unknown", "gone")):
            # Already the leader in this conversation, including a resumed one whose old host process ended:
            # keep the generation, refresh the recorded host.
            leader(db, {"session_id": d["session_id"], "project_id": d["project_id"], "leader_generation": row["generation"]})
            return {"generation": row["generation"], "lease_until": HELD, "already_leader": True, "last_handoff": last_handoff(db, d["project_id"])}
        require(not cfg["require_appointment_codes"], "becoming leader needs an appointment code from the user",
                next_action=APPOINT_HINT.format(role="leader"))
        if view is not None and not view["slot_free"]:
            if d.get("takeover") is not True or "reason" not in d:
                raise MeshError("CONFLICT", "the leader slot is held", 3, {
                    "owner_session_id": row["session_id"], "owner_host": view["owner_host"], "owner_host_state": view["host_state"],
                    "owner_session_state": view["owner_session_lease_state"],
                    "next_action": "Another session is the leader and it is not known to be gone. Do not retry in a loop. "
                                   "Only if the user explicitly told you to become the leader now (an earlier appointment you lost does not count): retry leader-acquire with a new op_id, "
                                   "\"takeover\": true and a \"reason\" naming the user's instruction. Otherwise tell the user who leads."})
            d["reason"] = f"user-directed takeover of generation {row['generation']} from session {row['session_id']} (host {view['host_state']}): {d['reason']}"
        elif view is not None and view["effective_status"] != "released":
            d["reason"] = (f"took over generation {row['generation']} from session {row['session_id']} ({view['effective_status']})"
                           + (f": {d['reason']}" if "reason" in d else ""))
        generation = take_leader_slot(db, cfg, d["project_id"], d["session_id"], timestamp, row)
        d["host"] = host_identity()
        # A new leader starts from the previous leader's newest handoff note (plan, next step, open decisions).
        return {"generation": generation, "lease_until": HELD, "host": d["host"], "last_handoff": last_handoff(db, d["project_id"])}
    if command in ("leader-renew", "leader-release"):
        fields(d, "session_id project_id leader_generation" + (" op_id" if command == "leader-release" or "op_id" in d else ""))
        leader(db, d)
        if command == "leader-renew":
            # No longer needed: leadership lasts for the whole conversation. Kept so older skills do not fail.
            # It also moves a leader from an older CLI version onto the held form.
            db.execute("UPDATE leader_leases SET lease_until=? WHERE project_id=?", (HELD, d["project_id"]))
            db.execute("UPDATE sessions SET last_seen_at=?,lease_until=? WHERE id=?", (timestamp, HELD, d["session_id"]))
            return {"generation": d["leader_generation"], "lease_until": HELD, "session_lease_until": HELD,
                    "note": "leader-renew is not needed: you lead until session-close, leader-release or a takeover."}
        db.execute("UPDATE leader_leases SET lease_until=? WHERE project_id=?", (timestamp, d["project_id"]))
        se = later(cfg["session_lease_seconds"])
        db.execute("UPDATE sessions SET last_seen_at=?,lease_until=? WHERE id=?", (timestamp, se, d["session_id"]))
        return {"generation": d["leader_generation"], "lease_until": timestamp, "session_lease_until": se}
    if command == "inbox-import":
        fields(d, "op_id session_id project_id leader_generation path")
        leader(db, d)
        require(isinstance(d["path"], str), "invalid inbox path", "INVALID_INPUT", 2)
        note, body, digest, path, priority = inbox_note(root, d["path"])
        require(note["project_id"] == d["project_id"], "inbox project mismatch")
        previous = db.execute("SELECT * FROM imports WHERE request_id=?", (note["request_id"],)).fetchone()
        if previous:
            require(previous["sha256"] == digest, "request ID already imported with different content")
            d["task_id"] = previous["task_id"]
            return {"task_id": previous["task_id"]}
        tid = new_id()
        db.execute("INSERT INTO tasks(id,project_id,request_id,title,description,category,priority,required_capabilities_json,outputs_json,acceptance_json,state,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'[]','[]','[]','proposed',1,?,?)", (tid, d["project_id"], note["request_id"], note["title"], body, note["category"], priority, timestamp, timestamp))
        db.execute("INSERT INTO imports VALUES(?,?,?,?)", (note["request_id"], digest, path, tid))
        d["task_id"] = tid
        return {"task_id": tid}
    if command == "session-close":
        fields(d, "op_id session_id")
        session(db, d["session_id"], allow_expired=True)
        active = db.execute("SELECT 1 FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE a.session_id=? AND a.state IN ('claimed','in_progress')", (d["session_id"],)).fetchone()
        require(not active, "session has an active attempt", next_action="Submit it with task-submit or give it up with attempt-release, then close the session.")
        db.execute("UPDATE sessions SET status='closed' WHERE id=?", (d["session_id"],))
        # A closing leader releases its leader lease, so a successor can acquire at once.
        db.execute("UPDATE leader_leases SET lease_until=? WHERE session_id=? AND lease_until>?", (timestamp, d["session_id"], timestamp))
        return {"session_id": d["session_id"]}
    if command in ("task-create", "task-spec"):
        names = "op_id session_id project_id leader_generation title description category priority required_capabilities outputs acceptance"
        if command == "task-spec":
            names += " task_id expected_version"
        fields(d, names)
        leader(db, d)
        require(all(isinstance(d[x], str) and d[x].strip() for x in ("title", "description", "category")) and type(d["priority"]) is int and 0 <= d["priority"] <= 100, "invalid task specification", "INVALID_INPUT", 2)
        require(all(isinstance(d[x], list) and all(isinstance(y, str) and y for y in d[x]) for x in ("required_capabilities", "outputs", "acceptance")), "invalid task arrays", "INVALID_INPUT", 2)
        single_line(d, "title", "category")
        values = (d["title"], d["description"], d["category"], d["priority"], canonical(capability_list(d["required_capabilities"], "required_capabilities")), canonical(d["outputs"]), canonical(d["acceptance"]))
        if command == "task-create":
            tid = new_id()
            db.execute("INSERT INTO tasks(id,project_id,title,description,category,priority,required_capabilities_json,outputs_json,acceptance_json,state,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'proposed',1,?,?)", (tid, d["project_id"], *values, timestamp, timestamp))
            d["task_id"] = tid
            return {"task_id": tid, "version": 1}
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        editable = task is not None and (task["state"] == "proposed" or task["state"] == "blocked" and task["current_attempt_id"] is None)
        require(editable and task["version"] == d["expected_version"], "task missing, not editable (proposed, or blocked after attempt-release) or version changed")
        db.execute("UPDATE tasks SET title=?,description=?,category=?,priority=?,required_capabilities_json=?,outputs_json=?,acceptance_json=?,version=version+1,updated_at=? WHERE id=?", (*values, timestamp, d["task_id"]))
        return {"task_id": d["task_id"], "version": task["version"] + 1}
    if command == "task-depend":
        fields(d, "op_id session_id project_id leader_generation task_id depends_on_id")
        leader(db, d)
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        dependency = db.execute("SELECT id FROM tasks WHERE id=? AND project_id=?", (d["depends_on_id"], d["project_id"])).fetchone()
        require(task is not None and task["state"] == "proposed" and dependency is not None and d["task_id"] != d["depends_on_id"], "invalid dependency")
        cycle = db.execute("WITH RECURSIVE ancestors(id) AS (SELECT depends_on_id FROM dependencies WHERE task_id=? UNION SELECT d.depends_on_id FROM dependencies d JOIN ancestors a ON d.task_id=a.id) SELECT 1 FROM ancestors WHERE id=?", (d["depends_on_id"], d["task_id"])).fetchone()
        require(not cycle, "dependency cycle")
        db.execute("INSERT INTO dependencies VALUES(?,?)", (d["task_id"], d["depends_on_id"]))
        db.execute("UPDATE tasks SET version=version+1,updated_at=? WHERE id=?", (timestamp, d["task_id"]))
        return {"task_id": d["task_id"], "version": task["version"] + 1}
    if command == "task-publish":
        fields(d, "op_id session_id project_id leader_generation task_id expected_version")
        leader(db, d)
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        require(task is not None and task["state"] == "proposed" and task["version"] == d["expected_version"] and task["description"].strip() and json.loads(task["acceptance_json"]), "task not publishable or version changed")
        db.execute("UPDATE tasks SET state='ready',version=version+1,updated_at=? WHERE id=?", (timestamp, d["task_id"]))
        required = json.loads(task["required_capabilities_json"])
        workers = live_worker_capabilities(db, d["project_id"], timestamp)
        warnings = []
        if not workers:
            warnings.append({"code": "no_live_worker", "required_capabilities": required,
                             "message": "No worker session is live in this project yet. This is normal before workers join: do not cancel; "
                                        "check capable_workers in compact status after they join."})
        elif not capable_count(workers, required):
            warnings.append({"code": "no_capable_worker", "required_capabilities": required,
                             "live_worker_capabilities": [sorted(x) for x in workers],
                             "message": "No live worker has all required_capabilities, so nobody can claim this task now. "
                                        "If that is a mistake, task-cancel it and publish a corrected task; otherwise wait for a suitable worker."})
        return {"task_id": d["task_id"], "version": task["version"] + 1, "warnings": warnings}
    if command == "task-reserve":
        fields(d, "op_id session_id project_id leader_generation task_id worker_session_id")
        leader(db, d)
        member(db, d["worker_session_id"], d["project_id"], {"worker"})
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        worker = session(db, d["worker_session_id"])
        require(task is not None and task["state"] == "ready" and (task["reservation_until"] is None or task["reservation_until"] <= timestamp) and set(json.loads(task["required_capabilities_json"])).issubset(json.loads(worker["capabilities_json"])), "task not reservable or worker not suitable")
        expiry = later(cfg["reservation_seconds"])
        db.execute("UPDATE tasks SET reservation_session_id=?,reservation_until=?,updated_at=? WHERE id=?", (d["worker_session_id"], expiry, timestamp, d["task_id"]))
        return {"reservation_until": expiry}
    if command == "task-claim":
        fields(d, " ".join(["op_id session_id project_id task_id"] + [k for k in ("start",) if k in d]))
        require("start" not in d or type(d["start"]) is bool, "start must be true or false", "INVALID_INPUT", 2)
        found, worker = session_blockers(db, d["session_id"], d["project_id"], timestamp)
        if found:
            raise_blocked(found)
        task = db.execute("SELECT t.*,p.status AS project_status FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=? AND t.project_id=?", (d["task_id"], d["project_id"])).fetchone()
        require(task is not None, "task unavailable", next_action="Copy the task id exactly from compact status for this project_id.")
        found = task_blockers(db, task, task["project_status"], worker, d["session_id"], timestamp)
        if found:
            raise_blocked(found)
        generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM attempts WHERE task_id=?", (d["task_id"],)).fetchone()[0]
        aid, expiry = new_id(), later(cfg["task_lease_seconds"])
        db.execute("INSERT INTO attempts VALUES(?,?,?,?,'claimed',?,'{}',NULL,?,?)", (aid, d["task_id"], d["session_id"], generation, expiry, timestamp, timestamp))
        db.execute("UPDATE tasks SET state='claimed',current_attempt_id=?,reservation_session_id=NULL,reservation_until=NULL,version=version+1,updated_at=? WHERE id=?", (aid, timestamp, d["task_id"]))
        db.execute("UPDATE sessions SET status='busy' WHERE id=?", (d["session_id"],))
        result = {"attempt_id": aid, "generation": generation, "lease_until": expiry, "state": "claimed", **worktree_info(root, d["project_id"], d["task_id"], aid)}
        pg = project_git(db, d["project_id"])
        if pg is not None:
            # A revision or a released attempt continues from the previous attempt's branch; otherwise from the base branch.
            prev = db.execute("SELECT id,state FROM attempts WHERE task_id=? AND id!=? ORDER BY generation DESC LIMIT 1", (d["task_id"], aid)).fetchone()
            start = worktree_info(root, d["project_id"], d["task_id"], prev["id"])["branch"] if prev and prev["state"] in ("revision", "abandoned") else pg["base_branch"]
            db.execute("INSERT INTO attempt_git(attempt_id,start_ref) VALUES(?,?)", (aid, start))
            result["git"] = {"repository": pg["repository"], "base_branch": pg["base_branch"], "start_ref": start}
        if d.get("start"):
            # claim and start in one call (plan 0.4.3)
            db.execute("UPDATE attempts SET state='in_progress',updated_at=? WHERE id=?", (timestamp, aid))
            db.execute("UPDATE tasks SET state='in_progress',version=version+1,updated_at=? WHERE id=?", (timestamp, d["task_id"]))
            result["state"] = "in_progress"
            result["task"] = {k: task[k] for k in ("id", "title", "description", "category", "priority")}
            result["task"].update(outputs=json.loads(task["outputs_json"]), acceptance=json.loads(task["acceptance_json"]))
            latest_review = db.execute("SELECT r.outcome,r.evidence FROM reviews r JOIN attempts a ON a.id=r.attempt_id WHERE a.task_id=? ORDER BY r.created_at DESC,r.id DESC LIMIT 1", (d["task_id"],)).fetchone()
            if latest_review:
                result["latest_review"] = dict(latest_review)
        return result
    if command == "task-start":
        fields(d, "op_id session_id attempt_id generation")
        a = attempt(db, d)
        require(a["state"] == "claimed", "attempt not claimed")
        db.execute("UPDATE attempts SET state='in_progress',updated_at=? WHERE id=?", (timestamp, a["id"]))
        db.execute("UPDATE tasks SET state='in_progress',updated_at=?,version=version+1 WHERE id=?", (timestamp, a["task_id"]))
        d["task_id"], d["project_id"] = a["task_id"], a["project_id"]
        return {"task_id": a["task_id"], "state": "in_progress"}
    if command == "heartbeat":
        require("attempt_id" in d, "heartbeat requires attempt_id (null renews only the session)", "INVALID_INPUT", 2,
                'Session only: {"op_id":"<new uuid4>","session_id":"<session_id>","attempt_id":null}. '
                'With an active attempt: add "attempt_id" and "generation" from task-claim.')
        fields(d, " ".join(["session_id", "attempt_id"] + [k for k in ("op_id",) if k in d]) + (" generation" if d.get("attempt_id") is not None else ""))
        # A holder whose leases ran out may still renew the attempt it holds; a session-only heartbeat needs a valid session.
        session(db, d["session_id"], allow_expired=d["attempt_id"] is not None)
        a = attempt(db, d, allow_expired=True) if d["attempt_id"] is not None else None
        # The current leader's session is held (never expires); a heartbeat must not shorten it.
        se = HELD if db.execute("SELECT lease_until FROM sessions WHERE id=?", (d["session_id"],)).fetchone()[0] == HELD else later(cfg["session_lease_seconds"])
        te = later(cfg["task_lease_seconds"]) if a else None
        db.execute("UPDATE sessions SET last_seen_at=?,lease_until=? WHERE id=?", (timestamp, se, d["session_id"]))
        if a:
            db.execute("UPDATE attempts SET lease_until=?,updated_at=? WHERE id=?", (te, timestamp, a["id"]))
            d["task_id"], d["project_id"] = a["task_id"], a["project_id"]
        return {"session_lease_until": se, "task_lease_until": te}
    if command in ("checkpoint", "task-submit"):
        extra = " ".join(k for k in CHECKPOINT_OPTIONAL if k in d) if command == "checkpoint" else ""
        fields(d, "op_id session_id attempt_id generation " + ("summary paths " + extra if command == "checkpoint" else "manifest"))
        a = attempt(db, d, allow_expired=True)
        if command == "checkpoint":
            require(isinstance(d["summary"], str) and d["summary"].strip(), "summary required", "INVALID_INPUT", 2)
            for key in ("note", "question"):
                require(key not in d or isinstance(d[key], str) and d[key].strip() and len(d[key]) <= 2000,
                        f"{key} must be non-empty text of at most 2000 characters (omit it to clear)", "INVALID_INPUT", 2)
            for key in ("blocker", "reply_seen"):
                require(key not in d or type(d[key]) is bool, f"{key} must be true or false", "INVALID_INPUT", 2)
            d["paths"] = attempt_paths(root, a, d["paths"])
            # note/question/blocker describe the current state: each checkpoint replaces them (absent = cleared).
            # The leader's reply is kept; reply_seen true records the read receipt.
            previous = json.loads(a["checkpoint_json"])
            saved = {"summary": d["summary"], "paths": d["paths"], "at": timestamp, **{k: d[k] for k in ("note", "question", "blocker") if k in d}}
            if isinstance(previous.get("leader_reply"), dict):
                saved["leader_reply"] = dict(previous["leader_reply"])
                if d.get("reply_seen") and not saved["leader_reply"].get("seen_at"):
                    saved["leader_reply"]["seen_at"] = timestamp
            db.execute("UPDATE attempts SET checkpoint_json=?,updated_at=? WHERE id=?", (canonical(saved), timestamp, a["id"]))
            result = {"task_id": a["task_id"]}
        else:
            manifest = d["manifest"]
            require(isinstance(manifest, dict) and set(manifest) == {"paths", "evidence", "summary"}, "invalid manifest", "INVALID_INPUT", 2)
            require(a["state"] == "in_progress" and all(isinstance(manifest[k], str) and manifest[k].strip() for k in ("summary", "evidence")), "attempt or manifest invalid", "INVALID_INPUT", 2)
            manifest["paths"] = attempt_paths(root, a, manifest["paths"])
            db.execute("UPDATE attempts SET state='submitted',manifest_json=?,updated_at=? WHERE id=?", (canonical(manifest), timestamp, a["id"]))
            db.execute("UPDATE tasks SET state='submitted',version=version+1,updated_at=? WHERE id=?", (timestamp, a["task_id"]))
            db.execute("UPDATE sessions SET status='available' WHERE id=? AND status='busy'", (d["session_id"],))
            result = {"task_id": a["task_id"], "state": "submitted"}
            if pre and pre.get("head_sha"):
                # The reviewed commit is pinned here: review must name it, and integration merges exactly it.
                db.execute("INSERT OR IGNORE INTO attempt_git(attempt_id,start_ref) VALUES(?,?)", (a["id"], "unknown"))
                db.execute("UPDATE attempt_git SET head_sha=?,submitted_at=? WHERE attempt_id=?", (pre["head_sha"], timestamp, a["id"]))
                result["head_sha"] = d["head_sha"] = pre["head_sha"]
        d["task_id"], d["project_id"] = a["task_id"], a["project_id"]
        return result
    if command == "attempt-release":
        fields(d, "op_id session_id attempt_id generation reason blocker")
        a = attempt(db, d, allow_expired=True)
        require(isinstance(d["reason"], str) and d["reason"].strip() and len(d["reason"]) <= 2000 and type(d["blocker"]) is bool,
                "reason (text, at most 2000 characters) and blocker (true or false) required", "INVALID_INPUT", 2)
        saved = json.loads(a["checkpoint_json"])
        saved["released"] = {"reason": d["reason"], "blocker": d["blocker"], "at": timestamp}
        state = "blocked" if d["blocker"] else "ready"
        db.execute("UPDATE attempts SET state='abandoned',checkpoint_json=?,updated_at=? WHERE id=?", (canonical(saved), timestamp, a["id"]))
        db.execute("UPDATE tasks SET state=?,current_attempt_id=NULL,version=version+1,updated_at=? WHERE id=?", (state, timestamp, a["task_id"]))
        db.execute("UPDATE sessions SET status='available' WHERE id=? AND status='busy'", (d["session_id"],))
        d["task_id"], d["project_id"] = a["task_id"], a["project_id"]
        return {"task_id": a["task_id"], "task_state": state, "released_attempt_id": a["id"],
                **{"preserved_" + k: v for k, v in worktree_info(root, a["project_id"], a["task_id"], a["id"]).items()}}
    if command == "attempt-reply":
        fields(d, "op_id session_id project_id leader_generation task_id body")
        leader(db, d)
        require(isinstance(d["body"], str) and d["body"].strip() and len(d["body"]) <= 2000, "body must be non-empty text of at most 2000 characters", "INVALID_INPUT", 2)
        a = db.execute("SELECT a.* FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE t.id=? AND t.project_id=? AND a.state IN ('claimed','in_progress')",
                       (d["task_id"], d["project_id"])).fetchone()
        require(a is not None, "task has no active attempt to reply to",
                next_action="attempt-reply answers a worker on a claimed or in_progress attempt. For submitted work use review evidence.")
        saved = json.loads(a["checkpoint_json"])
        # Remember what this reply answered: a later checkpoint repeating the same question/blocker is not a new question.
        saved["leader_reply"] = {"text": d["body"], "at": timestamp, "by_session_id": d["session_id"],
                                 "answered": {"question": saved.get("question"), "blocker": saved.get("blocker")}}
        send_message(db, d["project_id"], d["session_id"], [a["session_id"]], d["body"], d["task_id"], timestamp)
        db.execute("UPDATE attempts SET checkpoint_json=? WHERE id=?", (canonical(saved), a["id"]))
        return {"task_id": d["task_id"], "attempt_id": a["id"], "at": timestamp}
    if command == "activity-log":
        fields(d, "op_id session_id project_id task_id action result")
        member(db, d["session_id"], d["project_id"], {"worker", "reviewer", "leader-candidate", "integrator"})
        require(all(isinstance(d[x], str) and d[x].strip() for x in ("action", "result")), "action and result required", "INVALID_INPUT", 2)
        if d["action"] == "leader-handoff":
            held = db.execute("SELECT session_id,lease_until FROM leader_leases WHERE project_id=?", (d["project_id"],)).fetchone()
            require(held is not None and held["session_id"] == d["session_id"] and lease_view(held["lease_until"], timestamp)["lease_state"] == "valid",
                    "only the current leader can write leader-handoff", next_action=NEXT["leader"])
        if d["task_id"] is not None:
            require(db.execute("SELECT 1 FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone(), "task not in project")
        eid = new_id()
        cursor = db.execute("INSERT INTO events(event_id,at,session_id,project_id,task_id,action,result,payload_json) VALUES(?,?,?,?,?,?,?,?)", (eid, timestamp, d["session_id"], d["project_id"], d["task_id"], d["action"], d["result"], "{}"))
        return {"seq": cursor.lastrowid, "event_id": eid}
    if command == "project-status":
        fields(d, "op_id session_id project_id leader_generation status reason")
        leader(db, d)
        require(d["status"] in ("active", "paused") and isinstance(d["reason"], str) and d["reason"].strip(), "invalid status or reason", "INVALID_INPUT", 2)
        db.execute("UPDATE projects SET status=? WHERE id=?", (d["status"], d["project_id"]))
        return {"project_id": d["project_id"], "status": d["status"]}
    if command == "recover":
        fields(d, "op_id session_id project_id leader_generation task_id reason")
        leader(db, d)
        require(isinstance(d["reason"], str) and d["reason"].strip(), "reason required", "INVALID_INPUT", 2)
        a = db.execute("SELECT a.* FROM attempts a JOIN tasks t ON t.current_attempt_id=a.id WHERE t.id=? AND t.project_id=?", (d["task_id"], d["project_id"])).fetchone()
        require(a is not None and a["state"] in ("claimed", "in_progress") and lease_view(a["lease_until"], timestamp)["lease_state"] == "expired", "attempt not expired or not recoverable")
        d["preserved_checkpoint"] = json.loads(a["checkpoint_json"])
        info = worktree_info(root, d["project_id"], d["task_id"], a["id"])
        d["preserved_worktree"], d["preserved_branch"] = info["worktree"], info["branch"]
        db.execute("UPDATE attempts SET state='abandoned',updated_at=? WHERE id=?", (timestamp, a["id"]))
        db.execute("UPDATE tasks SET state='ready',current_attempt_id=NULL,version=version+1,updated_at=? WHERE id=?", (timestamp, d["task_id"]))
        db.execute("UPDATE sessions SET status='available' WHERE id=? AND status='busy'", (a["session_id"],))
        return {"task_id": d["task_id"], "abandoned_attempt_id": a["id"]}
    if command == "review":
        fields(d, " ".join(["op_id session_id attempt_id outcome evidence"] + [k for k in ("leader_generation", "reviewed_sha") if k in d]))
        require(d["outcome"] in ("accepted", "revision", "rejected", "blocked") and isinstance(d["evidence"], str) and d["evidence"].strip(), "invalid review", "INVALID_INPUT", 2)
        a = db.execute("SELECT a.*,t.project_id,t.state AS task_state,t.current_attempt_id FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (d["attempt_id"],)).fetchone()
        require(a is not None and a["session_id"] != d["session_id"] and a["state"] == "submitted" and a["current_attempt_id"] == a["id"] and a["task_state"] in ("submitted", "in_review"), "attempt not reviewable or self-review")
        if "leader_generation" in d:
            leader(db, {"session_id": d["session_id"], "project_id": a["project_id"], "leader_generation": d["leader_generation"]})
        else:
            member(db, d["session_id"], a["project_id"], {"reviewer"})
            reviewer = db.execute("SELECT * FROM sessions WHERE id=?", (d["session_id"],)).fetchone()
            author = db.execute("SELECT * FROM sessions WHERE id=?", (a["session_id"],)).fetchone()
            # Independence: a reviewer must come from the user's code, or have existed before this work was submitted.
            appointed = db.execute("SELECT 1 FROM appointments WHERE redeemed_by=? AND role='reviewer'", (d["session_id"],)).fetchone()
            require(appointed or reviewer["created_at"] < a["updated_at"], "this reviewer session was registered after the work was submitted",
                    next_action="An independent review needs a reviewer the user appointed (reviewer-join with a code) or the leader.")
            if cfg["review_requires_different_tool"]:
                require(reviewer["tool"] != author["tool"], "review by the same tool is switched off for this project", next_action=BLOCKERS["same_tool_review"][1])
        ag = attempt_git(db, a["id"])
        if ag and ag["head_sha"]:
            require(d.get("reviewed_sha") == ag["head_sha"], "you reviewed a different commit" if "reviewed_sha" in d else "reviewed_sha required",
                    next_action=f"Review commit {ag['head_sha']} (diff: git diff {project_git(db, a['project_id'])['base_branch']}...{ag['head_sha']}) and pass it as reviewed_sha.")
        rid = new_id()
        db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?)", (rid, a["id"], d["session_id"], d["outcome"], d["evidence"], timestamp))
        ast = {"accepted": "accepted", "revision": "revision", "rejected": "rejected", "blocked": "submitted"}[d["outcome"]]
        tst = {"accepted": "completed", "revision": "ready", "rejected": "rejected", "blocked": "blocked"}[d["outcome"]]
        db.execute("UPDATE attempts SET state=?,updated_at=? WHERE id=?", (ast, timestamp, a["id"]))
        db.execute("UPDATE tasks SET state=?,version=version+1,updated_at=? WHERE id=?", (tst, timestamp, a["task_id"]))
        d["task_id"], d["project_id"] = a["task_id"], a["project_id"]
        result = {"review_id": rid, "task_state": tst}
        if d["outcome"] == "accepted":
            branch = worktree_info(root, a["project_id"], a["task_id"], a["id"])["branch"]
            result["branch"] = branch
            if ag and ag["head_sha"]:
                result["head_sha"] = ag["head_sha"]
                result["next_action"] = ("Acceptance is not delivery (R8.1). Integrate now without being asked: task-integrate with \"merge\": true "
                                         "merges exactly the reviewed commit; then verify on the base branch and give that as evidence.")
            else:
                result["next_action"] = (f"Acceptance is not delivery (R8.1). Integrate now without being asked: merge {branch} into the base "
                                         "branch (R8.6), verify in the final location (R8.3), then record task-integrate.")
        return result
    if command == "task-integrate":
        fields(d, " ".join(["op_id session_id project_id task_id evidence"] + [k for k in ("leader_generation", "destination", "merge") if k in d]))
        require("merge" not in d or type(d["merge"]) is bool, "merge must be true or false", "INVALID_INPUT", 2)
        if "leader_generation" in d:
            leader(db, d)
        else:
            member(db, d["session_id"], d["project_id"], {"integrator"})
        pg = project_git(db, d["project_id"])
        require("destination" in d or pg is not None, "destination required", "INVALID_INPUT", 2,
                "Give the existing folder you integrated into as destination (with project-git set it defaults to the repository).")
        path = d.get("destination") or pg["repository"]
        require(isinstance(path, str) and Path(path).is_absolute() and ".." not in Path(path).parts and not path.startswith("\\\\") and Path(path).exists(),
                "destination must be an existing absolute local path without traversal", "INVALID_INPUT", 2,
                "Integrate the files first, verify them in the final location, then record that existing destination path.")
        require(isinstance(d["evidence"], str) and d["evidence"].strip(), "verification evidence required", "INVALID_INPUT", 2,
                "State how the integrated result was verified in its final location (commands run and results).")
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        require(task is not None and task["state"] == "completed", "only an accepted (completed) task can be recorded as integrated",
                next_action="Review and accept the submitted attempt first; integrate only accepted output.")
        accepted = db.execute("SELECT * FROM attempts WHERE id=? AND state='accepted'", (task["current_attempt_id"],)).fetchone()
        require(accepted is not None, "task has no accepted attempt")
        d["integrated_attempt_id"] = accepted["id"]
        d["manifest"] = json.loads(accepted["manifest_json"]) if accepted["manifest_json"] else None
        ag = attempt_git(db, accepted["id"])
        git_info = pre or {}
        require(not (ag and ag["head_sha"]) or git_info.get("head_sha") == ag["head_sha"], "the Git check did not run for this integration; retry with a new op_id")
        for key in ("head_sha", "merge_sha", "extra_commits"):
            if git_info.get(key):
                d[key] = git_info[key]
        db.execute("INSERT OR REPLACE INTO integrations VALUES(?,?,?,?,?,?,?,?)",
                   (task["id"], accepted["id"], git_info.get("head_sha"), git_info.get("merge_sha"), path, d["evidence"], d["session_id"], timestamp))
        result = {"task_id": task["id"], "attempt_id": accepted["id"], "destination": path, "integrated_at": timestamp}
        result.update({k: git_info[k] for k in ("head_sha", "merge_sha") if git_info.get(k)})
        if git_info.get("extra_commits"):
            result["warnings"] = [{"code": "later_commits_not_merged", "commits": git_info["extra_commits"],
                                   "message": "The task branch has commits after the reviewed one; they were not merged."}]
        return result
    if command == "task-unblock":
        fields(d, "op_id session_id project_id leader_generation task_id reason")
        leader(db, d)
        require(isinstance(d["reason"], str) and d["reason"].strip(), "reason required", "INVALID_INPUT", 2)
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        require(task is not None and task["state"] == "blocked", "task not blocked")
        state = "submitted" if task["current_attempt_id"] else "ready"
        db.execute("UPDATE tasks SET state=?,version=version+1,updated_at=? WHERE id=?", (state, timestamp, task["id"]))
        return {"task_id": task["id"], "state": state}
    if command == "task-cancel":
        fields(d, "op_id session_id project_id leader_generation task_id reason")
        leader(db, d)
        require(isinstance(d["reason"], str) and d["reason"].strip(), "reason required", "INVALID_INPUT", 2)
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        require(task is not None, "task not found in project", next_action="Copy the task id exactly from compact status for this project_id.")
        require(task["state"] in ("proposed", "ready", "blocked", "rejected"), f"task in state {task['state']} cannot be cancelled",
                next_action="Only proposed, ready, blocked or rejected tasks can be cancelled. Wait for a live attempt to be submitted, released or recovered.")
        dependents = db.execute("SELECT t.id,t.title,t.state FROM dependencies x JOIN tasks t ON t.id=x.task_id WHERE x.depends_on_id=? AND t.state!='cancelled' ORDER BY t.id", (task["id"],)).fetchall()
        if dependents:
            raise MeshError("CONFLICT", "other tasks depend on this task", 3, {"dependents": [dict(x) for x in dependents],
                            "next_action": "Cancel the dependent tasks first (they could never become claimable), then cancel this one."})
        db.execute("UPDATE tasks SET state='cancelled',reservation_session_id=NULL,reservation_until=NULL,version=version+1,updated_at=? WHERE id=?", (timestamp, task["id"]))
        return {"task_id": task["id"], "state": "cancelled"}
    if command == "task-reopen":
        fields(d, "op_id session_id project_id leader_generation task_id reason")
        leader(db, d)
        require(d["reason"].strip(), "reason required", "INVALID_INPUT", 2)
        task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone()
        require(task is not None and task["state"] == "rejected", "only a rejected task can be reopened",
                next_action="task-reopen turns a rejected task back into proposed; edit it with task-spec, then task-publish.")
        db.execute("UPDATE tasks SET state='proposed',current_attempt_id=NULL,version=version+1,updated_at=? WHERE id=?", (timestamp, task["id"]))
        return {"task_id": task["id"], "state": "proposed", "version": task["version"] + 1}
    if command == "recommend":
        fields(d, "op_id session_id project_id task_id dedupe_key body")
        member(db, d["session_id"], d["project_id"], {"worker", "reviewer", "leader-candidate", "integrator"})
        require(all(isinstance(d[x], str) and d[x].strip() for x in ("dedupe_key", "body")), "invalid recommendation", "INVALID_INPUT", 2)
        if d["task_id"] is not None:
            require(db.execute("SELECT 1 FROM tasks WHERE id=? AND project_id=?", (d["task_id"], d["project_id"])).fetchone(), "task not in project")
        key = d["dedupe_key"].strip().casefold()
        existing = db.execute("SELECT id FROM recommendations WHERE project_id=? AND dedupe_key=?", (d["project_id"], key)).fetchone()
        if existing:
            return {"recommendation_id": existing["id"], "existing": True}
        rid = new_id()
        db.execute("INSERT INTO recommendations VALUES(?,?,?,?,?,?,'proposed',?)", (rid, d["project_id"], d["session_id"], d["task_id"], key, d["body"], timestamp))
        return {"recommendation_id": rid, "existing": False}
    if command == "recommend-decide":
        fields(d, "op_id session_id project_id leader_generation recommendation_id status reason")
        leader(db, d)
        require(d["status"] in ("accepted", "declined", "deferred") and isinstance(d["reason"], str) and d["reason"].strip(), "invalid decision", "INVALID_INPUT", 2)
        cur = db.execute("UPDATE recommendations SET status=? WHERE id=? AND project_id=?", (d["status"], d["recommendation_id"], d["project_id"]))
        require(cur.rowcount == 1, "recommendation not found")
        return {"recommendation_id": d["recommendation_id"], "status": d["status"]}
    raise MeshError("NOT_IMPLEMENTED", "command is not implemented", 2)
