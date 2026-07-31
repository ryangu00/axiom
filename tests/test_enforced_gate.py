"""The `enforced` gate must fail safe identically on every shim.

CONTRACTS §5 makes `enforced` the authoritative signal for host action. A
cross-family review pointed out that the three shims only agreed on the
missing-key case *by coincidence* — each used plain truthiness, so the string
`"false"` would have enabled enforcement, and a refactor could silently break
the alignment. These tests pin the contract instead: nothing but the boolean
`True` may let an adapter act on the host.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_SHIM = REPO_ROOT / "adapters" / "codex" / "hooks" / "codex_adapter.py"
HERMES_ADAPTER = REPO_ROOT / "adapters" / "hermes" / "__init__.py"

# Everything that is not the boolean True must read as "not enforced".
NOT_ENFORCED: list[object] = [None, False, "false", "true", 0, 1, "", "yes", [], {}]


def _stub_cli(directory: Path, response: dict) -> Path:
    """A fake CLI that emits one fixed adapter response on stdout."""
    stub = directory / "stub_cli.py"
    stub.write_text(
        "import json, sys\nsys.stdin.read()\nprint(json.dumps("
        + repr(response)
        + "))\n",
        encoding="utf-8",
    )
    return stub


class EnforcedGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _response(self, enforced: object) -> dict:
        payload: dict = {
            "protocol": "axiom-adapter-cli/v1",
            "outcome": "failed",
            "claim_id": "c1",
            "cleared": False,
            "evidence": [],
            "reason": "AXIOM write verification failed: stub",
        }
        if enforced is not None:
            payload["enforced"] = enforced
        return payload

    def _run_codex(self, enforced: object) -> str:
        stub = _stub_cli(self.cwd, self._response(enforced))
        env = dict(os.environ)
        env["AXIOM_CLI"] = str(stub)
        env["CLAUDE_PLUGIN_DATA"] = str(self.cwd / ".axiom-test-data")
        result = subprocess.run(
            [sys.executable, str(CODEX_SHIM), "verify"],
            input=json.dumps({"cwd": str(self.cwd)}),
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _run_hermes(self, enforced: object) -> object:
        stub = _stub_cli(self.cwd, self._response(enforced))
        saved = {
            key: os.environ.get(key)
            for key in ("AXIOM_CLI", "TERMINAL_CWD", "CLAUDE_PLUGIN_DATA")
        }
        os.environ["AXIOM_CLI"] = str(stub)
        os.environ["TERMINAL_CWD"] = str(self.cwd)
        os.environ["CLAUDE_PLUGIN_DATA"] = str(self.cwd / ".axiom-test-data")
        try:
            spec = importlib.util.spec_from_file_location(
                "axiom_hermes_gate", HERMES_ADAPTER
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module._pre_verify(attempt=0)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_codex_acts_only_on_boolean_true(self) -> None:
        for value in NOT_ENFORCED:
            with self.subTest(enforced=value):
                self.assertEqual(
                    self._run_codex(value),
                    "{}",
                    f"enforced={value!r} must not produce a block",
                )
        decision = json.loads(self._run_codex(True))
        self.assertEqual(decision["decision"], "block")

    def test_hermes_acts_only_on_boolean_true(self) -> None:
        for value in NOT_ENFORCED:
            with self.subTest(enforced=value):
                self.assertIsNone(
                    self._run_hermes(value),
                    f"enforced={value!r} must not keep the turn going",
                )
        result = self._run_hermes(True)
        assert isinstance(result, dict)  # narrows for the type checker
        self.assertEqual(result["action"], "continue")


if __name__ == "__main__":
    unittest.main()
