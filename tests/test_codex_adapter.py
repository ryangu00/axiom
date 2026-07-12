"""Contract tests for the Codex host shim (adapters/codex/hooks/codex_adapter.py).

The shim is exercised exactly as Codex invokes it: `python3 codex_adapter.py
<verb>` with one JSON payload on stdin. Each test drives the shim against the
real CLI and evaluator (no mocks) so the full chain shim -> CLI -> predicate ->
Codex decision is under test. Per CONTRACTS.md §5 the shim must translate CLI
facts into Codex `decision:block` / `{}` and always fail open.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM = REPO_ROOT / "adapters" / "codex" / "hooks" / "codex_adapter.py"


def run_shim(verb: str, payload: dict, *, cwd: Path, env_extra: dict | None = None):
    import os

    env = dict(os.environ)
    # Isolate state under the test cwd so no test ever writes to the user's
    # real plugin data root (the shim reads CLAUDE_PLUGIN_DATA for both CLI
    # state and its observable event log).
    env["CLAUDE_PLUGIN_DATA"] = str(cwd / ".axiom-test-data")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SHIM), verb],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=140,
    )


def write_goal(cwd: Path, artifact: Path) -> None:
    (cwd / "demo.goal.md").write_text(
        "# demo goal\n## acceptance\n```json\n"
        + json.dumps([{"type": "file_exists", "path": str(artifact)}])
        + "\n```\n",
        encoding="utf-8",
    )


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.artifact = self.cwd / "artifact.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _register(self) -> None:
        write_goal(self.cwd, self.artifact)
        result = run_shim("register", {"cwd": str(self.cwd)}, cwd=self.cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

    def test_verify_failed_blocks(self) -> None:
        self._register()  # artifact absent -> claim fails
        result = run_shim("verify", {"cwd": str(self.cwd)}, cwd=self.cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)
        self.assertEqual(decision.get("decision"), "block")
        self.assertTrue(decision.get("reason"), "block must carry a reason")

    def test_verify_passed_is_silent(self) -> None:
        self._register()
        self.artifact.write_text("done", encoding="utf-8")
        result = run_shim("verify", {"cwd": str(self.cwd)}, cwd=self.cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

    def test_verify_no_claim_is_silent(self) -> None:
        result = run_shim("verify", {"cwd": str(self.cwd)}, cwd=self.cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

    def test_reentry_cap_fails_open_with_event(self) -> None:
        self._register()  # a failing claim is active
        result = run_shim(
            "verify",
            {"cwd": str(self.cwd), "stop_hook_active": True},
            cwd=self.cwd,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})  # does NOT block again
        self.assertIn("verify_reentry_capped", result.stderr)

    def test_reentry_cap_writes_data_root_event(self) -> None:
        self._register()
        data_root = self.cwd / "plugin-data"
        run_shim(
            "verify",
            {"cwd": str(self.cwd), "stop_hook_active": True},
            cwd=self.cwd,
            env_extra={"CLAUDE_PLUGIN_DATA": str(data_root)},
        )
        events = (data_root / "adapter-events.jsonl").read_text(encoding="utf-8")
        self.assertIn("verify_reentry_capped", events)

    def test_missing_cli_fails_open(self) -> None:
        self._register()
        result = run_shim(
            "verify",
            {"cwd": str(self.cwd)},
            cwd=self.cwd,
            env_extra={"AXIOM_CLI": str(self.cwd / "nonexistent_cli.py")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})  # fail open, no block

    def test_malformed_stdin_emits_valid_json(self) -> None:
        import os

        env = dict(os.environ)
        env["CLAUDE_PLUGIN_DATA"] = str(self.cwd / ".axiom-test-data")
        result = subprocess.run(
            [sys.executable, str(SHIM), "verify"],
            input="not json at all",
            capture_output=True,
            text=True,
            cwd=str(self.cwd),
            env=env,
            timeout=140,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        json.loads(result.stdout)  # must still emit parseable hook output

    def test_unknown_verb_fails_open(self) -> None:
        result = run_shim("frobnicate", {"cwd": str(self.cwd)}, cwd=self.cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})


if __name__ == "__main__":
    unittest.main()
