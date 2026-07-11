"""Process-boundary tests for Axiom CLI failure semantics."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "scripts" / "axiom_cli.py"


class CliExitCodeTests(unittest.TestCase):
    def _run_with_broken_common(self, command: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            hooks = root / "hooks"
            scripts.mkdir()
            hooks.mkdir()
            copied_cli = scripts / "axiom_cli.py"
            shutil.copyfile(CLI_PATH, copied_cli)
            (hooks / "axiom_common.py").write_text(
                'raise RuntimeError("injected import failure")\n', encoding="utf-8"
            )
            return subprocess.run(
                [sys.executable, str(copied_cli), command],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_report_and_modes_fail_closed_when_common_import_fails(self) -> None:
        for command in ("report", "modes"):
            with self.subTest(command=command):
                result = self._run_with_broken_common(command)
                combined = (result.stdout + result.stderr).lower()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("could not import axiom_common", result.stderr)
                for success_wording in (
                    "current rule modes",
                    "findings by rule",
                    "coverage",
                    "persisted",
                    "success",
                ):
                    self.assertNotIn(success_wording, combined)

    def test_no_arguments_keeps_argparse_exit_code(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI_PATH)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("the following arguments are required: command", result.stderr)
