from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "herdr_subagent_panes.py"
SPEC = importlib.util.spec_from_file_location("herdr_subagent_panes", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class FakeHerdr:
    def __init__(self, root: str = "w7:p1") -> None:
        self.live = {root}
        self.split_calls: list[tuple[str, str, str]] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.run_calls: list[tuple[str, str]] = []
        self.close_calls: list[str] = []
        self._next = 2
        self._mutex = threading.Lock()

    def pane_exists(self, pane_id: str) -> bool:
        return pane_id in self.live

    def split(self, pane_id: str, cwd: str, direction: str) -> str:
        with self._mutex:
            new_pane = f"w7:p{self._next}"
            self._next += 1
            self.live.add(new_pane)
            self.split_calls.append((pane_id, cwd, direction))
            return new_pane

    def rename(self, pane_id: str, label: str) -> None:
        self.rename_calls.append((pane_id, label))

    def run(self, pane_id: str, command: str) -> None:
        self.run_calls.append((pane_id, command))

    def close(self, pane_id: str) -> None:
        self.close_calls.append(pane_id)
        self.live.discard(pane_id)


class FailingHerdr(FakeHerdr):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    def rename(self, pane_id: str, label: str) -> None:
        if self.failure == "rename":
            raise plugin.HerdrError("rename failed")
        super().rename(pane_id, label)

    def run(self, pane_id: str, command: str) -> None:
        if self.failure == "run":
            raise plugin.HerdrError("run failed")
        super().run(pane_id, command)


def payload(event: str, agent_id: str, agent_type: str = "worker") -> dict[str, str]:
    return {
        "hook_event_name": event,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": "parent-session",
        "cwd": str(ROOT),
    }


class HookLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w7:p1",
            "HERDR_WORKSPACE_ID": "w7",
            "PLUGIN_DATA": self.temporary.name,
            "CODEX_HOME": str(Path(self.temporary.name) / "codex"),
            "PATH": "/usr/bin",
        }
        self.herdr = FakeHerdr()

    def handle(self, event: str, agent_id: str, agent_type: str = "worker") -> None:
        plugin.handle_hook(
            payload(event, agent_id, agent_type),
            env=self.env,
            client=self.herdr,
            script_path=SCRIPT,
        )

    def state(self) -> dict:
        return json.loads((Path(self.temporary.name) / "state.json").read_text())

    def test_first_agent_splits_parent_to_the_right_without_focus(self) -> None:
        self.handle("SubagentStart", "agent-1", "reviewer")

        self.assertEqual(self.herdr.split_calls, [("w7:p1", str(ROOT), "right")])
        self.assertEqual(self.herdr.rename_calls, [("w7:p2", "subagent: reviewer")])
        self.assertEqual(self.herdr.run_calls[0][0], "w7:p2")
        self.assertIn("--parent-pane-id w7:p1", self.herdr.run_calls[0][1])
        session = next(iter(self.state()["sessions"].values()))
        self.assertEqual(session["agents"]["agent-1"]["pane_id"], "w7:p2")
        self.assertEqual(session["agents"]["agent-1"]["layout_path"], "")

    def test_concurrent_agents_tile_the_right_half_breadth_first(self) -> None:
        for index in range(1, 9):
            self.handle("SubagentStart", f"agent-{index}")

        self.assertEqual(
            self.herdr.split_calls,
            [
                ("w7:p1", str(ROOT), "right"),
                ("w7:p2", str(ROOT), "down"),
                ("w7:p2", str(ROOT), "right"),
                ("w7:p3", str(ROOT), "right"),
                ("w7:p2", str(ROOT), "down"),
                ("w7:p4", str(ROOT), "down"),
                ("w7:p3", str(ROOT), "down"),
                ("w7:p5", str(ROOT), "down"),
            ],
        )
        session = next(iter(self.state()["sessions"].values()))
        self.assertEqual(
            {agent_id: entry["layout_path"] for agent_id, entry in session["agents"].items()},
            {
                "agent-1": "000",
                "agent-5": "001",
                "agent-3": "010",
                "agent-6": "011",
                "agent-2": "100",
                "agent-7": "101",
                "agent-4": "110",
                "agent-8": "111",
            },
        )

    def test_stop_closes_only_the_exact_agent_pane(self) -> None:
        self.handle("SubagentStart", "agent-1")
        self.handle("SubagentStart", "agent-2")

        self.handle("SubagentStop", "agent-1")

        self.assertEqual(self.herdr.close_calls, ["w7:p2"])
        self.assertIn("w7:p1", self.herdr.live)
        session = next(iter(self.state()["sessions"].values()))
        self.assertEqual(set(session["agents"]), {"agent-2"})

    def test_stop_collapses_the_layout_branch_before_the_next_split(self) -> None:
        for index in range(1, 5):
            self.handle("SubagentStart", f"agent-{index}")

        self.handle("SubagentStop", "agent-3")
        session = next(iter(self.state()["sessions"].values()))
        self.assertEqual(
            {agent_id: entry["layout_path"] for agent_id, entry in session["agents"].items()},
            {"agent-1": "0", "agent-2": "10", "agent-4": "11"},
        )

        self.handle("SubagentStart", "agent-5")
        self.assertEqual(self.herdr.split_calls[-1], ("w7:p2", str(ROOT), "right"))

    def test_last_stop_removes_empty_session(self) -> None:
        self.handle("SubagentStart", "agent-1")
        self.handle("SubagentStop", "agent-1")
        self.assertEqual(self.state()["sessions"], {})

    def test_duplicate_start_is_idempotent(self) -> None:
        self.handle("SubagentStart", "agent-1")
        self.handle("SubagentStart", "agent-1")
        self.assertEqual(len(self.herdr.split_calls), 1)

    def test_stale_rightmost_pane_is_pruned_before_next_split(self) -> None:
        self.handle("SubagentStart", "agent-1")
        self.herdr.live.discard("w7:p2")
        self.handle("SubagentStart", "agent-2")
        self.assertEqual(self.herdr.split_calls[-1], ("w7:p1", str(ROOT), "right"))

    def test_non_herdr_surface_is_a_strict_noop(self) -> None:
        for env in ({}, {"HERDR_ENV": "0", "HERDR_PANE_ID": "w7:p1"}):
            plugin.handle_hook(payload("SubagentStart", "agent-1"), env=env, client=self.herdr)
        self.assertEqual(self.herdr.split_calls, [])

    def test_missing_parent_pane_is_a_noop(self) -> None:
        self.herdr.live.clear()
        self.handle("SubagentStart", "agent-1")
        self.assertEqual(self.herdr.split_calls, [])

    def test_incomplete_start_payload_is_a_noop(self) -> None:
        broken = payload("SubagentStart", "agent-1")
        broken.pop("agent_type")
        plugin.handle_hook(broken, env=self.env, client=self.herdr, script_path=SCRIPT)
        self.assertEqual(self.herdr.split_calls, [])

    def test_missing_cwd_falls_back_to_current_directory(self) -> None:
        event = payload("SubagentStart", "agent-1")
        event["cwd"] = "/path/that/does/not/exist"
        with mock.patch.object(plugin.os, "getcwd", return_value="/fallback"):
            plugin.handle_hook(event, env=self.env, client=self.herdr, script_path=SCRIPT)
        self.assertEqual(self.herdr.split_calls, [("w7:p1", "/fallback", "right")])

    def test_rename_failure_does_not_discard_viewer(self) -> None:
        herdr = FailingHerdr("rename")
        plugin.handle_hook(
            payload("SubagentStart", "agent-1"),
            env=self.env,
            client=herdr,
            script_path=SCRIPT,
        )
        self.assertEqual(len(herdr.run_calls), 1)

    def test_run_failure_closes_only_the_new_pane(self) -> None:
        herdr = FailingHerdr("run")
        with self.assertRaisesRegex(plugin.HerdrError, "run failed"):
            plugin.handle_hook(
                payload("SubagentStart", "agent-1"),
                env=self.env,
                client=herdr,
                script_path=SCRIPT,
            )
        self.assertEqual(herdr.close_calls, ["w7:p2"])
        self.assertIn("w7:p1", herdr.live)

    def test_unknown_stop_is_a_noop(self) -> None:
        self.handle("SubagentStop", "not-running")
        self.assertEqual(self.herdr.close_calls, [])

    def test_stop_cleans_state_when_viewer_already_closed(self) -> None:
        self.handle("SubagentStart", "agent-1")
        self.herdr.live.discard("w7:p2")
        self.handle("SubagentStop", "agent-1")
        self.assertEqual(self.herdr.close_calls, [])
        self.assertEqual(self.state()["sessions"], {})

    def test_unknown_event_is_a_noop(self) -> None:
        self.handle("Notification", "agent-1")
        self.assertEqual(self.herdr.split_calls, [])

    def test_parent_pane_is_never_closed_even_if_state_is_corrupt(self) -> None:
        data = {
            "version": 1,
            "next_sequence": 1,
            "sessions": {
                "broken": {
                    "root_pane_id": "w7:p1",
                    "agents": {"agent-1": {"pane_id": "w7:p1", "sequence": 1}},
                }
            },
        }
        (Path(self.temporary.name) / "state.json").write_text(json.dumps(data))
        self.handle("SubagentStop", "agent-1")
        self.assertEqual(self.herdr.close_calls, [])

    def test_session_end_closes_all_viewers_but_not_parent(self) -> None:
        self.handle("SubagentStart", "agent-1")
        self.handle("SubagentStart", "agent-2")
        event = payload("SessionEnd", "unused")
        plugin.handle_hook(event, env=self.env, client=self.herdr, script_path=SCRIPT)
        self.assertEqual(self.herdr.close_calls, ["w7:p2", "w7:p3"])
        self.assertIn("w7:p1", self.herdr.live)
        self.assertEqual(self.state()["sessions"], {})

    def test_new_session_cleans_stale_viewers_in_same_parent_pane(self) -> None:
        self.handle("SubagentStart", "agent-1")
        event = payload("SessionStart", "unused")
        event["session_id"] = "new-parent-session"
        plugin.handle_hook(event, env=self.env, client=self.herdr, script_path=SCRIPT)
        self.assertEqual(self.herdr.close_calls, ["w7:p2"])
        self.assertEqual(self.state()["sessions"], {})


class HerdrClientTests(unittest.TestCase):
    @mock.patch.object(plugin.subprocess, "run")
    def test_split_uses_explicit_anchor_and_no_focus(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"result":{"pane":{"pane_id":"w1:p9"}}}', stderr=""
        )
        pane = plugin.HerdrClient("herdr-test").split("w1:p4", "/work", "down")
        self.assertEqual(pane, "w1:p9")
        self.assertEqual(
            run.call_args.args[0],
            [
                "herdr-test",
                "pane",
                "split",
                "w1:p4",
                "--direction",
                "down",
                "--cwd",
                "/work",
                "--no-focus",
            ],
        )

    @mock.patch.object(plugin.subprocess, "run")
    def test_cli_failure_raises_without_guessing(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gone"
        )
        with self.assertRaisesRegex(plugin.HerdrError, "gone"):
            plugin.HerdrClient().close("w1:p9")

    @mock.patch.object(plugin.subprocess, "run")
    def test_invalid_json_raises(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr=""
        )
        with self.assertRaisesRegex(plugin.HerdrError, "invalid JSON"):
            plugin.HerdrClient().run("w1:p9", "true")

    @mock.patch.object(plugin.subprocess, "run")
    def test_pane_run_accepts_silent_success(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        plugin.HerdrClient().run("w1:p9", "true")

    @mock.patch.object(plugin.subprocess, "run")
    def test_non_object_json_raises(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        with self.assertRaisesRegex(plugin.HerdrError, "unexpected response"):
            plugin.HerdrClient().run("w1:p9", "true")

    @mock.patch.object(plugin.subprocess, "run")
    def test_split_requires_pane_id(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}", stderr=""
        )
        with self.assertRaisesRegex(plugin.HerdrError, "pane id"):
            plugin.HerdrClient().split("w1:p1", "/work", "right")

    @mock.patch.object(plugin.subprocess, "run")
    def test_rename_run_and_close_use_exact_pane(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}", stderr=""
        )
        client = plugin.HerdrClient()
        client.rename("w1:p2", "subagent: worker")
        client.run("w1:p2", "viewer")
        client.close("w1:p2")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ["herdr", "pane", "rename", "w1:p2", "subagent: worker"])
        self.assertEqual(commands[1], ["herdr", "pane", "run", "w1:p2", "viewer"])
        self.assertEqual(commands[2], ["herdr", "pane", "close", "w1:p2"])


class StateTests(unittest.TestCase):
    def test_invalid_or_old_state_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            for contents in ("not json", "[]", '{"version":0}', '{"version":1,"sessions":[]}'):
                state.write_text(contents)
                self.assertEqual(plugin._load_state(state), plugin._default_state())

    def test_invalid_sequence_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            state.write_text('{"version":1,"next_sequence":"bad","sessions":{}}')
            self.assertEqual(plugin._load_state(state)["next_sequence"], 0)

    def test_old_agent_state_gets_balanced_layout_paths(self) -> None:
        agents = {
            "agent-2": {"pane_id": "w7:p3", "sequence": 2},
            "agent-1": {"pane_id": "w7:p2", "sequence": "bad"},
            "agent-3": {"pane_id": "w7:p4", "sequence": 3},
        }
        plugin._ensure_layout_paths(agents)
        self.assertEqual(
            {agent_id: entry["layout_path"] for agent_id, entry in agents.items()},
            {"agent-1": "00", "agent-2": "1", "agent-3": "01"},
        )

    def test_session_end_uses_codex_timeout_limit(self) -> None:
        hooks_path = ROOT / "hooks" / "hooks.json"
        hooks = json.loads(hooks_path.read_text())["hooks"]
        self.assertEqual(hooks["SessionEnd"][0]["hooks"][0]["timeout"], 3)
        for event in ("SessionStart", "SubagentStart", "SubagentStop"):
            self.assertEqual(hooks[event][0]["hooks"][0]["timeout"], 10)

    def test_data_dir_fallback_is_user_scoped(self) -> None:
        with (
            mock.patch.object(plugin.tempfile, "gettempdir", return_value="/tmp/test"),
            mock.patch.object(plugin.os, "getuid", return_value=42),
        ):
            self.assertEqual(plugin._data_dir({}), Path("/tmp/test/herdr-codex-subagents-42"))

    def test_helpers_handle_unexpected_shapes(self) -> None:
        self.assertIsNone(plugin._nested_get([], "result"))
        self.assertIsNone(plugin._find_agent({"sessions": []}, "agent"))
        state = {"sessions": {"bad": [], "ok": {"agents": {}}}}
        self.assertIsNone(plugin._find_agent(state, "agent"))
        session = {"agents": []}
        plugin._prune_session(session, FakeHerdr())
        self.assertEqual(session, {"agents": {}})


class ViewerTests(unittest.TestCase):
    def item(self, item: dict) -> dict:
        return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}

    def test_finds_rollout_by_exact_agent_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "sessions" / "2026" / "09" / "09"
            target.mkdir(parents=True)
            expected = target / "rollout-now-agent-123.jsonl"
            expected.write_text("{}\n")
            self.assertEqual(plugin._find_transcript(Path(temporary), "agent-123"), expected)

    def test_renders_agent_message(self) -> None:
        record = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "content": [{"type": "Text", "text": "Working on it"}],
                },
            },
        }
        self.assertEqual(plugin.render_rollout_event(record), (["\nWorking on it"], False))

    def test_renders_shell_command_without_shell_wrapper(self) -> None:
        record = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "command": ["/bin/bash", "-lc", "pytest -q"],
                    "status": "completed",
                },
            },
        }
        self.assertEqual(
            plugin.render_rollout_event(record), (["\n$ pytest -q [completed]"], False)
        )

    def test_terminal_events_are_detected(self) -> None:
        for event_type in ("task_complete", "turn_aborted", "task_failed"):
            lines, terminal = plugin.render_rollout_event(
                {"type": "event_msg", "payload": {"type": event_type}}
            )
            self.assertTrue(terminal)
            self.assertTrue(lines)

    def test_renders_file_mcp_and_reasoning_events(self) -> None:
        cases = [
            (self.item({"type": "FileChange", "changes": [{}, {}]}), "Δ file changes (2)"),
            (
                self.item({"type": "McpToolCall", "server_name": "github", "tool_name": "get"}),
                "↳ github.get",
            ),
            (self.item({"type": "Reasoning", "summary_text": ["checking"]}), "◇ checking"),
        ]
        for record, expected in cases:
            lines, terminal = plugin.render_rollout_event(record)
            self.assertIn(expected, lines[0])
            self.assertFalse(terminal)

    def test_ignores_unknown_and_malformed_records(self) -> None:
        records = [
            {},
            {"type": "event_msg", "payload": []},
            {"type": "event_msg", "payload": {"type": "token_count"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": []}},
            self.item({"type": "Unknown"}),
        ]
        for record in records:
            self.assertEqual(plugin.render_rollout_event(record), ([], False))

    def test_text_and_command_helpers_cover_fallbacks(self) -> None:
        self.assertEqual(plugin._content_text("bad"), "")
        self.assertEqual(plugin._content_text([{}, {"text": "hello"}]), "hello")
        self.assertEqual(plugin._shorten(" abc ", 5), "abc")
        self.assertEqual(plugin._shorten("abcdef", 5), "abcd…")
        self.assertEqual(plugin._command_text(["git", "status"]), "git status")
        self.assertEqual(plugin._command_text("make test"), "make test")
        self.assertEqual(plugin._command_text(None), "command")

    def test_current_task_snapshot_ignores_invalid_and_non_task_records(self) -> None:
        snapshot = io.StringIO(
            "not-json\n"
            "[]\n"
            '{"type":"event_msg","payload":[]}\n'
            '{"type":"event_msg","payload":{"type":"task_complete"}}\n'
        )
        self.assertEqual(plugin._current_task_snapshot(snapshot), [])

    @mock.patch.object(plugin, "_close_own_pane")
    @mock.patch.object(plugin.time, "sleep")
    def test_viewer_ignores_inherited_completed_tasks(
        self, sleep: mock.Mock, close: mock.Mock
    ) -> None:
        del sleep
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "sessions" / "2026" / "09" / "09"
            target.mkdir(parents=True)
            transcript = target / "rollout-now-agent-123.jsonl"
            records = [
                "not-json",
                json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "AgentMessage",
                                "content": [{"text": "inherited output"}],
                            },
                        },
                    }
                ),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "AgentMessage",
                                "content": [{"text": "current output"}],
                            },
                        },
                    }
                ),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
            ]
            transcript.write_text("\n".join(records) + "\n")
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                plugin.view_subagent("agent-123", "worker", "parent", "w1:p1", Path(temporary), 0)
            self.assertIn("Codex subagent", output.getvalue())
            self.assertNotIn("inherited output", output.getvalue())
            self.assertIn("current output", output.getvalue())
            self.assertEqual(output.getvalue().count("✓ complete"), 1)
            close.assert_called_once_with("w1:p1")

    @mock.patch.object(plugin, "_close_own_pane")
    @mock.patch.object(plugin.time, "sleep")
    @mock.patch.object(plugin, "_current_task_snapshot", return_value=[])
    def test_viewer_follows_new_records_after_snapshot(
        self, snapshot: mock.Mock, sleep: mock.Mock, close: mock.Mock
    ) -> None:
        del sleep
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "sessions" / "2026" / "09" / "09"
            target.mkdir(parents=True)
            transcript = target / "rollout-now-agent-123.jsonl"
            transcript.write_text(
                "not-json\n"
                "[]\n"
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n'
            )
            output = io.StringIO()

            with mock.patch("sys.stdout", output):
                plugin.view_subagent("agent-123", "worker", "parent", "w1:p1", Path(temporary), 0)

            snapshot.assert_called_once()
            self.assertIn("✓ complete", output.getvalue())
            close.assert_called_once_with("w1:p1")

    def test_missing_sessions_directory_has_no_transcript(self) -> None:
        self.assertIsNone(plugin._find_transcript(Path("/missing"), "agent"))

    @mock.patch.dict(plugin.os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p8"}, clear=True)
    def test_viewer_fallback_closes_only_its_own_pane(self) -> None:
        herdr = FakeHerdr(root="w2:p8")
        plugin._close_own_pane("w2:p1", client=herdr)
        self.assertEqual(herdr.close_calls, ["w2:p8"])

    @mock.patch.dict(plugin.os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p1"}, clear=True)
    def test_viewer_fallback_refuses_parent_pane(self) -> None:
        herdr = FakeHerdr(root="w2:p1")
        plugin._close_own_pane("w2:p1", client=herdr)
        self.assertEqual(herdr.close_calls, [])


class EntrypointTests(unittest.TestCase):
    @mock.patch.object(plugin, "handle_hook")
    def test_hook_main_accepts_object_payload(self, handle: mock.Mock) -> None:
        with (
            mock.patch("sys.stdin", io.StringIO('{"hook_event_name":"SubagentStart"}')),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(plugin._hook_main(), 0)
        handle.assert_called_once()
        self.assertEqual(output.getvalue(), "{}\n")

    @mock.patch.object(plugin, "_log")
    def test_hook_main_swallows_invalid_input(self, log: mock.Mock) -> None:
        with (
            mock.patch("sys.stdin", io.StringIO("bad")),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(plugin._hook_main(), 0)
        log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
