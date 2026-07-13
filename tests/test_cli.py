"""Process-boundary tests for Axiom CLI failure semantics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "scripts" / "axiom_cli.py"


class CliProjectScopeTests(unittest.TestCase):
    """Project-scoped subcommands must honor --cwd, not the ambient shell cwd."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.project = base / "project"
        self.elsewhere = base / "elsewhere"
        self.data_root = base / "state"
        self.project.mkdir()
        self.elsewhere.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cli(self, *argv: str) -> subprocess.CompletedProcess[str]:
        # Always run from the WRONG directory to prove --cwd wins.
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = str(self.data_root)
        return subprocess.run(
            [sys.executable, str(CLI_PATH), *argv],
            text=True,
            capture_output=True,
            check=False,
            cwd=str(self.elsewhere),
            env=env,
        )

    def test_uninstall_targets_explicit_cwd_and_names_the_project(self) -> None:
        # Seed real state for the project via enforce (writes config.json).
        seeded = self._cli("enforce", "write-verify", "on", "--cwd", str(self.project))
        self.assertEqual(seeded.returncode, 0, seeded.stderr)

        # Dry-run against the right project, run from the wrong directory:
        # must see the seeded state and must name the resolved project.
        dry = self._cli("uninstall", "--dry-run", "--cwd", str(self.project))
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("axiom project:", dry.stdout)
        self.assertIn(str(self.project.resolve()), dry.stdout)
        self.assertNotIn("(none present)", dry.stdout)

        # Without --cwd the ambient directory is targeted — allowed for
        # compatibility, but the resolved project must be visible so a
        # wrong-directory run cannot read as a clean uninstall.
        ambient = self._cli("uninstall", "--dry-run")
        self.assertIn(str(self.elsewhere.resolve()), ambient.stdout)

        # Real uninstall with --cwd removes the project's state.
        real = self._cli("uninstall", "--confirm", "--cwd", str(self.project))
        self.assertEqual(real.returncode, 0, real.stdout + real.stderr)
        after = self._cli("uninstall", "--dry-run", "--cwd", str(self.project))
        self.assertIn("(none present)", after.stdout)

    def test_persist_lessons_targets_explicit_cwd(self) -> None:
        lessons_json = self.elsewhere / "lessons.json"
        lessons_json.write_text(
            json.dumps(
                [
                    {
                        "timestamp": "2026-07-12T00:00:00Z",
                        "source": "test",
                        "text": "explicit cwd lesson",
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = self._cli(
            "persist-lessons", str(lessons_json), "--cwd", str(self.project)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lessons_files = list(self.data_root.rglob("lessons.md"))
        self.assertEqual(len(lessons_files), 1)
        self.assertIn(
            "explicit cwd lesson", lessons_files[0].read_text(encoding="utf-8")
        )

    def test_enforce_warns_on_unknown_rule_but_stays_fail_open(self) -> None:
        typo = self._cli("enforce", "write-verfy", "on", "--cwd", str(self.project))
        self.assertEqual(typo.returncode, 0)
        self.assertIn("not a known rule", typo.stderr)
        known = self._cli("enforce", "write-verify", "on", "--cwd", str(self.project))
        self.assertEqual(known.returncode, 0)
        self.assertNotIn("not a known rule", known.stderr)


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
