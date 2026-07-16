"""Contract tests for the hermes adapter (adapters/hermes/__init__.py).

The adapter module imports no hermes internals, so it loads standalone here and
its hook handlers run against the real CLI and evaluator (no mocks). This
proves the adapter emits the correct hermes control shape
(`{"action":"continue","message"}` on failure, `None` otherwise) and honors the
re-entry cap. That hermes' own `get_pre_verify_continue_message` consumes this
shape is proven separately by the p2 in-process probe.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "scripts" / "axiom_cli.py"
ADAPTER = REPO_ROOT / "adapters" / "hermes" / "__init__.py"


def load_adapter():
    spec = importlib.util.spec_from_file_location("axiom_hermes_adapter", ADAPTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_goal(cwd: Path, artifact: Path) -> None:
    (cwd / "demo.goal.md").write_text(
        "# demo goal\n## acceptance\n```json\n"
        + json.dumps([{"type": "file_exists", "path": str(artifact)}])
        + "\n```\n",
        encoding="utf-8",
    )


class HermesAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = load_adapter()
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.artifact = self.cwd / "artifact.txt"
        # Isolate CLI location, working dir, and state root for this test.
        self._env = {
            "AXIOM_CLI": str(CLI),
            "TERMINAL_CWD": str(self.cwd),
            "CLAUDE_PLUGIN_DATA": str(self.cwd / ".axiom-test-data"),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _register(self) -> None:
        write_goal(self.cwd, self.artifact)
        self.assertIsNone(self.adapter._on_session_start())

    def test_failed_claim_continues_with_reason(self) -> None:
        self._register()  # artifact absent -> claim fails
        result = self.adapter._pre_verify(attempt=0)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["action"], "continue")
        self.assertTrue(result["message"])

    def test_passed_claim_finishes(self) -> None:
        self._register()
        self.artifact.write_text("done", encoding="utf-8")
        self.assertIsNone(self.adapter._pre_verify(attempt=0))

    def test_no_claim_finishes(self) -> None:
        self.assertIsNone(self.adapter._pre_verify(attempt=0))

    def test_reentry_cap(self) -> None:
        self._register()  # a failing claim is active
        self.assertIsNone(self.adapter._pre_verify(attempt=1))  # capped, no nudge

    def test_missing_cli_fails_open(self) -> None:
        self._register()
        os.environ["AXIOM_CLI"] = str(self.cwd / "nonexistent_cli.py")
        self.assertIsNone(self.adapter._pre_verify(attempt=0))

    def test_nonzero_exit_fails_open(self) -> None:
        # CLI exits nonzero with a "failed" body -> must not keep the turn going.
        self._register()
        stub = self.cwd / "bad_cli.py"
        stub.write_text(
            'import sys\nprint(\'{"outcome":"failed","reason":"x"}\')\nsys.exit(3)\n',
            encoding="utf-8",
        )
        os.environ["AXIOM_CLI"] = str(stub)
        self.assertIsNone(self.adapter._pre_verify(attempt=0))

    def test_every_fail_open_path_is_observable(self) -> None:
        # A fail-open nobody can see is indistinguishable from an adapter that
        # was never installed. Each failure mode must say so on stderr.
        import contextlib
        import io

        self._register()
        cases = {
            "nonzero exit": "import sys\nsys.exit(3)\n",
            "bad JSON": "print('not json')\n",
        }
        for label, script in cases.items():
            with self.subTest(case=label):
                stub = self.cwd / "stub_cli.py"
                stub.write_text(script, encoding="utf-8")
                os.environ["AXIOM_CLI"] = str(stub)
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    self.assertIsNone(self.adapter._pre_verify(attempt=0))
                self.assertIn("failed open", buf.getvalue())

    def test_reentry_cap_coerces_attempt(self) -> None:
        # A str/float/None attempt must not bypass the one-strike cap.
        self._register()  # a failing claim is active
        self.assertIsNone(self.adapter._pre_verify(attempt="1"))
        self.assertIsNone(self.adapter._pre_verify(attempt=1.0))
        # attempt=0 (first pass) still runs verify -> continue on the failure.
        self.assertIsInstance(self.adapter._pre_verify(attempt=0), dict)

    def test_reentry_cap_emits_event(self) -> None:
        import contextlib
        import io

        self._register()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertIsNone(self.adapter._pre_verify(attempt=1))
        self.assertIn("verify_reentry_capped", buf.getvalue())

    def test_register_wires_both_hooks(self) -> None:
        registered: dict[str, object] = {}

        class FakeCtx:
            def register_hook(self, name: str, callback: object) -> None:
                registered[name] = callback

        self.adapter.register(FakeCtx())
        self.assertIn("on_session_start", registered)
        self.assertIn("pre_verify", registered)


if __name__ == "__main__":
    unittest.main()
