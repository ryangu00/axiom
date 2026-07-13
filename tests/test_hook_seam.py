"""Host-seam contract tests: run every hook exactly as Claude Code does.

Each hook is invoked as a subprocess — ``python3 hooks/<hook>.py --data-root
<tmp>`` with one JSON payload on stdin — because every ``main()`` is fail-open
(``except Exception: return 0``): a bug inside the seam keeps in-process tests
green while the product is silently disabled. These tests pin the seam itself,
mirroring the treatment the adapter lanes already get.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
CLI_PATH = REPO_ROOT / "scripts" / "axiom_cli.py"


class HookSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.cwd = base / "project"
        self.data_root = base / "state"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _hook(self, script: str, payload: dict) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_DATA", None)  # argv --data-root must win alone
        result = subprocess.run(
            [
                sys.executable,
                str(HOOKS_DIR / script),
                "--data-root",
                str(self.data_root),
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            cwd=str(self.cwd),
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _ledger_events(self) -> list[dict]:
        events: list[dict] = []
        for ledger in self.data_root.rglob("ledger.jsonl"):
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    events.append(record)
        return events

    def _active_claims(self) -> list[Path]:
        return [p for p in self.data_root.rglob("active.json") if p.stat().st_size]

    def test_goal_file_to_stop_full_chain_through_the_host_seam(self) -> None:
        """SessionStart registers the goal claim; Stop verifies it — all via
        subprocess, covering register -> observe -> enforce-block -> re-entry."""
        (self.cwd / "demo.goal.md").write_text(
            "# demo\n## acceptance\n```json\n"
            + json.dumps([{"type": "file_exists", "path": "artifact.txt"}])
            + "\n```\n",
            encoding="utf-8",
        )

        # SessionStart (axiom_common.py IS the registered hook entry).
        self._hook(
            "axiom_common.py", {"hook_event_name": "SessionStart", "cwd": str(self.cwd)}
        )
        self.assertEqual(len(self._active_claims()), 1)
        self.assertIn("heartbeat", {e.get("event") for e in self._ledger_events()})

        # Stop under the observe DEFAULT: silent stdout, claim survives.
        stop_payload = {
            "hook_event_name": "Stop",
            "cwd": str(self.cwd),
            "stop_hook_active": False,
        }
        observed = self._hook("write_verify.py", stop_payload)
        self.assertEqual(observed.stdout, "")
        self.assertIn(
            "would_have_blocked", {e.get("event") for e in self._ledger_events()}
        )
        self.assertEqual(len(self._active_claims()), 1)

        # Flip to enforce through the real CLI, still against the same state.
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = str(self.data_root)
        enforced = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "enforce",
                "write-verify",
                "on",
                "--cwd",
                str(self.cwd),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(enforced.returncode, 0, enforced.stderr)

        # Enforced failing claim blocks the stop with a reason.
        blocked = self._hook("write_verify.py", stop_payload)
        decision = json.loads(blocked.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("artifact.txt", decision["reason"])

        # Re-entry fails open and records the escalation.
        reentry = self._hook(
            "write_verify.py", {**stop_payload, "stop_hook_active": True}
        )
        self.assertEqual(reentry.stdout, "")
        self.assertIn("escalation", {e.get("event") for e in self._ledger_events()})

        # Fix the artifact: the enforced stop now passes silently and clears.
        (self.cwd / "artifact.txt").write_text("made\n", encoding="utf-8")
        passed = self._hook("write_verify.py", stop_payload)
        self.assertEqual(passed.stdout, "")
        self.assertIn("verified", {e.get("event") for e in self._ledger_events()})
        self.assertEqual(self._active_claims(), [])

    def test_schema_guard_seam_records_tmp_persistence(self) -> None:
        result = self._hook(
            "schema_guard.py",
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "cwd": str(self.cwd),
                "tool_input": {"file_path": "/tmp/axiom-seam-ledger.jsonl"},
            },
        )
        self.assertEqual(result.stdout, "")  # observe default: no deny
        events = [e for e in self._ledger_events() if e.get("hook") == "schema_guard"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "would_have_blocked")

    def test_preflight_seam_records_irreversible_command_class(self) -> None:
        result = self._hook(
            "preflight.py",
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "cwd": str(self.cwd),
                # The target must NOT be under a tmp root (the test cwd is),
                # or preflight's deliberate tmp carve-out skips it.
                "tool_input": {"command": "rm -rf /opt/data/junk"},
            },
        )
        self.assertEqual(result.stdout, "")  # observe default: advisory only
        events = [e for e in self._ledger_events() if e.get("hook") == "preflight"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "would_have_blocked")

    def test_stuck_search_seam_tracks_failure_clusters(self) -> None:
        self._hook(
            "stuck_search.py",
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "cwd": str(self.cwd),
                "tool_input": {"command": "make build"},
                "error": "boom",
            },
        )
        states = list(self.data_root.rglob("stuck-search.json"))
        self.assertEqual(len(states), 1)
        clusters = json.loads(states[0].read_text(encoding="utf-8"))["clusters"]
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["count"], 1)

    def test_health_check_seam_is_silent_when_healthy(self) -> None:
        result = self._hook(
            "health_check.py",
            {"hook_event_name": "SessionStart", "cwd": str(self.cwd)},
        )
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
