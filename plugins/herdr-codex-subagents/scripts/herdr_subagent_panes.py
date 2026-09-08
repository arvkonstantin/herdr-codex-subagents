#!/usr/bin/env python3
"""Mirror Codex subagent activity into disposable Herdr panes."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

STATE_VERSION = 1
DEFAULT_POLL_INTERVAL = 0.2
DEFAULT_CLOSE_GRACE = 0.35
MAX_RENDERED_TEXT = 2_000


class HerdrError(RuntimeError):
    """Raised when a Herdr CLI operation fails."""


class HerdrClient:
    """Small, JSON-aware wrapper around the Herdr pane CLI."""

    def __init__(self, executable: str = "herdr") -> None:
        self.executable = executable

    def _call(
        self, *args: str, timeout: float = 5.0, allow_empty: bool = False
    ) -> dict[str, Any]:
        completed = subprocess.run(
            [self.executable, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise HerdrError(detail or f"Herdr exited with status {completed.returncode}")
        if allow_empty and not completed.stdout.strip():
            return {}
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise HerdrError("Herdr returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise HerdrError("Herdr returned an unexpected response")
        return payload

    def pane_exists(self, pane_id: str) -> bool:
        try:
            self._call("pane", "get", pane_id)
        except (HerdrError, subprocess.SubprocessError):
            return False
        return True

    def split_right(self, pane_id: str, cwd: str) -> str:
        payload = self._call(
            "pane",
            "split",
            pane_id,
            "--direction",
            "right",
            "--cwd",
            cwd,
            "--no-focus",
        )
        new_pane_id = _nested_get(payload, "result", "pane", "pane_id")
        if not isinstance(new_pane_id, str) or not new_pane_id:
            raise HerdrError("Herdr split response did not include a pane id")
        return new_pane_id

    def rename(self, pane_id: str, label: str) -> None:
        self._call("pane", "rename", pane_id, label)

    def run(self, pane_id: str, command: str) -> None:
        # `herdr pane run` succeeds silently; other pane commands return JSON.
        self._call("pane", "run", pane_id, command, allow_empty=True)

    def close(self, pane_id: str) -> None:
        self._call("pane", "close", pane_id)


def _nested_get(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _data_dir(env: Mapping[str, str]) -> Path:
    configured = env.get("PLUGIN_DATA") or env.get("CLAUDE_PLUGIN_DATA")
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / f"herdr-codex-subagents-{os.getuid()}"


def _default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "next_sequence": 0, "sessions": {}}


def _load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default_state()
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return _default_state()
    if not isinstance(payload.get("sessions"), dict):
        return _default_state()
    if not isinstance(payload.get("next_sequence"), int):
        payload["next_sequence"] = 0
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextlib.contextmanager
def _locked_state(data_dir: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = data_dir / "state.lock"
    state_path = data_dir / "state.json"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        state = _load_state(state_path)
        try:
            yield state_path, state
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _session_key(session_id: str, root_pane_id: str) -> str:
    raw = f"{session_id}\0{root_pane_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _log(data_dir: Path, message: str) -> None:
    try:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with (data_dir / "plugin.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def _prune_session(session: dict[str, Any], client: HerdrClient) -> None:
    agents = session.get("agents")
    if not isinstance(agents, dict):
        session["agents"] = {}
        return
    stale = [
        agent_id
        for agent_id, entry in agents.items()
        if not isinstance(entry, dict)
        or not isinstance(entry.get("pane_id"), str)
        or not client.pane_exists(entry["pane_id"])
    ]
    for agent_id in stale:
        agents.pop(agent_id, None)


def _find_agent(
    state: dict[str, Any], agent_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        return None
    for key, session in sessions.items():
        if not isinstance(session, dict):
            continue
        agents = session.get("agents", {})
        if isinstance(agents, dict) and isinstance(agents.get(agent_id), dict):
            return key, session, agents[agent_id]
    return None


def _viewer_command(
    script_path: Path,
    agent_id: str,
    agent_type: str,
    session_id: str,
    parent_pane_id: str,
    codex_home: Path,
) -> str:
    arguments = [
        sys.executable,
        str(script_path),
        "view",
        "--agent-id",
        agent_id,
        "--agent-type",
        agent_type,
        "--session-id",
        session_id,
        "--parent-pane-id",
        parent_pane_id,
        "--codex-home",
        str(codex_home),
    ]
    return " ".join(shlex.quote(argument) for argument in arguments)


def _handle_start(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    client: HerdrClient,
    script_path: Path,
) -> None:
    agent_id = payload.get("agent_id")
    session_id = payload.get("session_id")
    agent_type = payload.get("agent_type")
    root_pane_id = env.get("HERDR_PANE_ID")
    workspace_id = env.get("HERDR_WORKSPACE_ID")
    cwd = payload.get("cwd")
    if not all(
        isinstance(value, str) and value
        for value in (agent_id, session_id, agent_type, root_pane_id)
    ):
        return
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        cwd = os.getcwd()
    if not client.pane_exists(root_pane_id):
        return

    data_dir = _data_dir(env)
    with _locked_state(data_dir) as (state_path, state):
        existing = _find_agent(state, agent_id)
        if existing is not None and client.pane_exists(existing[2]["pane_id"]):
            return

        key = _session_key(session_id, root_pane_id)
        sessions = state.setdefault("sessions", {})
        session = sessions.setdefault(
            key,
            {
                "session_id": session_id,
                "root_pane_id": root_pane_id,
                "workspace_id": workspace_id,
                "agents": {},
            },
        )
        _prune_session(session, client)
        agents = session["agents"]
        anchor = root_pane_id
        if agents:
            latest = max(agents.values(), key=lambda entry: int(entry.get("sequence", -1)))
            anchor = latest["pane_id"]

        pane_id = client.split_right(anchor, cwd)
        label = f"subagent: {agent_type}"[:80]
        with contextlib.suppress(HerdrError, subprocess.SubprocessError):
            client.rename(pane_id, label)

        codex_home = Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        command = _viewer_command(
            script_path=script_path,
            agent_id=agent_id,
            agent_type=agent_type,
            session_id=session_id,
            parent_pane_id=root_pane_id,
            codex_home=codex_home,
        )
        try:
            client.run(pane_id, command)
        except (HerdrError, subprocess.SubprocessError):
            with contextlib.suppress(HerdrError, subprocess.SubprocessError):
                client.close(pane_id)
            raise

        state["next_sequence"] += 1
        agents[agent_id] = {
            "agent_type": agent_type,
            "pane_id": pane_id,
            "sequence": state["next_sequence"],
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        _save_state(state_path, state)
        _log(data_dir, f"opened agent={agent_id} pane={pane_id} anchor={anchor}")


def _handle_stop(payload: Mapping[str, Any], env: Mapping[str, str], client: HerdrClient) -> None:
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return
    data_dir = _data_dir(env)
    with _locked_state(data_dir) as (state_path, state):
        found = _find_agent(state, agent_id)
        if found is None:
            return
        key, session, entry = found
        pane_id = entry.get("pane_id")
        root_pane_id = session.get("root_pane_id")
        if not isinstance(pane_id, str) or pane_id == root_pane_id:
            _log(data_dir, f"refused unsafe close agent={agent_id} pane={pane_id}")
            return

        pane_exists = client.pane_exists(pane_id)
        if pane_exists:
            client.close(pane_id)
        session.get("agents", {}).pop(agent_id, None)
        if not session.get("agents"):
            state.get("sessions", {}).pop(key, None)
        _save_state(state_path, state)
        _log(data_dir, f"closed agent={agent_id} pane={pane_id} existed={pane_exists}")


def _cleanup_sessions(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    client: HerdrClient,
    *,
    stale_only: bool,
) -> None:
    session_id = payload.get("session_id")
    root_pane_id = env.get("HERDR_PANE_ID")
    if not isinstance(session_id, str) or not session_id or not root_pane_id:
        return
    data_dir = _data_dir(env)
    with _locked_state(data_dir) as (state_path, state):
        sessions = state.get("sessions", {})
        selected: list[str] = []
        for key, session in sessions.items():
            if not isinstance(session, dict) or session.get("root_pane_id") != root_pane_id:
                continue
            same_session = session.get("session_id") == session_id
            if (stale_only and not same_session) or (not stale_only and same_session):
                selected.append(key)

        changed = False
        for key in selected:
            session = sessions[key]
            for entry in session.get("agents", {}).values():
                pane_id = entry.get("pane_id") if isinstance(entry, dict) else None
                if not isinstance(pane_id, str) or pane_id == root_pane_id:
                    continue
                if client.pane_exists(pane_id):
                    with contextlib.suppress(HerdrError, subprocess.SubprocessError):
                        client.close(pane_id)
                _log(data_dir, f"cleanup pane={pane_id} stale_only={stale_only}")
            sessions.pop(key, None)
            changed = True
        if changed:
            _save_state(state_path, state)


def handle_hook(
    payload: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    client: HerdrClient | None = None,
    script_path: Path | None = None,
) -> None:
    """Handle one Codex hook payload without affecting non-Herdr environments."""
    active_env = os.environ if env is None else env
    if active_env.get("HERDR_ENV") != "1" or not active_env.get("HERDR_PANE_ID"):
        return
    if shutil.which("herdr", path=active_env.get("PATH")) is None and client is None:
        return
    active_client = client or HerdrClient()
    active_script = (script_path or Path(__file__)).resolve()
    event_name = payload.get("hook_event_name")
    if event_name == "SessionStart":
        _cleanup_sessions(payload, active_env, active_client, stale_only=True)
    elif event_name == "SessionEnd":
        _cleanup_sessions(payload, active_env, active_client, stale_only=False)
    elif event_name == "SubagentStart":
        _handle_start(payload, active_env, active_client, active_script)
    elif event_name == "SubagentStop":
        _handle_stop(payload, active_env, active_client)


def _find_transcript(codex_home: Path, agent_id: str) -> Path | None:
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    suffix = f"-{agent_id}.jsonl"
    matches: list[Path] = []
    for directory, _, files in os.walk(sessions):
        for filename in files:
            if filename.endswith(suffix):
                matches.append(Path(directory) / filename)
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _shorten(text: str, limit: int = MAX_RENDERED_TEXT) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _command_text(command: Any) -> str:
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        if len(command) >= 3 and command[1] in {"-c", "-lc"}:
            return command[2]
        return shlex.join(command)
    if isinstance(command, str):
        return command
    return "command"


def render_rollout_event(record: Mapping[str, Any]) -> tuple[list[str], bool]:
    """Convert one rollout JSON record to compact viewer lines and terminal status."""
    if record.get("type") != "event_msg":
        return [], False
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return [], False
    event_type = payload.get("type")
    if event_type == "task_started":
        return ["● working"], False
    if event_type == "task_complete":
        return ["✓ complete"], True
    if event_type in {"turn_aborted", "task_failed"}:
        return ["■ interrupted" if event_type == "turn_aborted" else "✗ failed"], True
    if event_type != "item_completed":
        return [], False

    item = payload.get("item")
    if not isinstance(item, dict):
        return [], False
    item_type = item.get("type")
    if item_type == "AgentMessage":
        text = _shorten(_content_text(item.get("content")))
        return ([f"\n{text}"] if text else []), False
    if item_type == "CommandExecution":
        command = _shorten(_command_text(item.get("command")), 800)
        status = item.get("status")
        suffix = f" [{status}]" if isinstance(status, str) and status else ""
        return [f"\n$ {command}{suffix}"], False
    if item_type == "FileChange":
        changes = item.get("changes")
        count = len(changes) if isinstance(changes, list) else None
        suffix = f" ({count})" if count is not None else ""
        return [f"\nΔ file changes{suffix}"], False
    if item_type in {"McpToolCall", "MCPToolCall"}:
        server = item.get("server") or item.get("server_name") or "mcp"
        tool = item.get("tool") or item.get("tool_name") or "tool"
        return [f"\n↳ {server}.{tool}"], False
    if item_type == "Reasoning":
        summary = item.get("summary_text")
        text = _shorten("\n".join(summary), 800) if isinstance(summary, list) else ""
        return ([f"\n◇ {text}"] if text else []), False
    return [], False


def _close_own_pane(parent_pane_id: str, client: HerdrClient | None = None) -> None:
    own_pane_id = os.environ.get("HERDR_PANE_ID")
    if not own_pane_id or own_pane_id == parent_pane_id or os.environ.get("HERDR_ENV") != "1":
        return
    active_client = client or HerdrClient()
    with contextlib.suppress(HerdrError, subprocess.SubprocessError):
        active_client.close(own_pane_id)


def view_subagent(
    agent_id: str,
    agent_type: str,
    session_id: str,
    parent_pane_id: str,
    codex_home: Path,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Follow a subagent rollout and render a compact read-only activity stream."""
    del session_id  # Reserved for future app-server based lookup.
    header = textwrap.dedent(
        f"""\
        Codex subagent
        type  {agent_type}
        id    {agent_id}

        Waiting for activity…
        """
    )
    print(header, flush=True)
    transcript: Path | None = None
    while transcript is None:
        transcript = _find_transcript(codex_home, agent_id)
        if transcript is None:
            time.sleep(poll_interval)

    print(f"transcript  {transcript.name}\n", flush=True)
    with transcript.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline()
            if not line:
                time.sleep(poll_interval)
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            lines, terminal = render_rollout_event(record)
            for rendered in lines:
                print(rendered, flush=True)
            if terminal:
                time.sleep(float(os.environ.get("HERDR_SUBAGENT_CLOSE_GRACE", DEFAULT_CLOSE_GRACE)))
                _close_own_pane(parent_pane_id)
                return


def _hook_main() -> int:
    data_dir = _data_dir(os.environ)
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            handle_hook(payload)
    except Exception as error:  # Hooks must never break the Codex agent lifecycle.
        _log(data_dir, f"error type={type(error).__name__} message={error}")
    print("{}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook", help="Read and handle one Codex hook payload from stdin.")
    viewer = subparsers.add_parser("view", help="Render one Codex subagent rollout.")
    viewer.add_argument("--agent-id", required=True)
    viewer.add_argument("--agent-type", required=True)
    viewer.add_argument("--session-id", required=True)
    viewer.add_argument("--parent-pane-id", required=True)
    viewer.add_argument("--codex-home", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "hook":
        return _hook_main()
    view_subagent(
        agent_id=args.agent_id,
        agent_type=args.agent_type,
        session_id=args.session_id,
        parent_pane_id=args.parent_pane_id,
        codex_home=args.codex_home,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
