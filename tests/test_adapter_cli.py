#!/usr/bin/env python3
"""Subprocess contract tests for the host-neutral adapter CLI verbs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "scripts" / "axiom_cli.py"
PROTOCOL = "axiom-adapter-cli/v1"


class AdapterCliContractTests(unittest.TestCase):
    def _run(
        self,
        verb: str,
        request: dict[str, Any] | str,
        *,
        data_root: Path,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        payload = request if isinstance(request, str) else json.dumps(request)
        environment = os.environ.copy()
        environment["CLAUDE_PLUGIN_DATA"] = str(data_root)
        result = subprocess.run(
            [sys.executable, str(CLI_PATH), verb],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        response = json.loads(result.stdout)
        self.assertIsInstance(response, dict)
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertEqual(response["protocol"], PROTOCOL)
        for host_word in ("block", "revise", "continue"):
            self.assertNotIn(host_word, result.stdout)
        return result, response

    def test_register_returns_registered_then_already_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            cwd.mkdir()
            request = {
                "cwd": str(cwd),
                "claim": {
                    "label": "adapter claim",
                    "predicates": [{"type": "file_exists", "path": "done.txt"}],
                },
            }

            winner, registered = self._run(
                "register", request, data_root=base / "state"
            )
            loser, active = self._run("register", request, data_root=base / "state")

            self.assertEqual(winner.returncode, 0)
            self.assertEqual(registered["outcome"], "registered")
            self.assertTrue(registered["registered"])
            self.assertIsInstance(registered["claim_id"], str)
            self.assertIsNone(registered["reason"])
            self.assertEqual(loser.returncode, 0)
            self.assertEqual(active["outcome"], "already_active")
            self.assertFalse(active["registered"])
            self.assertEqual(active["claim_id"], registered["claim_id"])

    def test_register_discovers_goal_and_reports_no_goal_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            empty = base / "empty"
            empty.mkdir()
            no_goal_result, no_goal = self._run(
                "register", {"cwd": str(empty)}, data_root=base / "empty-state"
            )
            self.assertEqual(no_goal_result.returncode, 0)
            self.assertEqual(no_goal["outcome"], "no_goal_found")
            self.assertFalse(no_goal["registered"])
            self.assertIsNone(no_goal["claim_id"])

            project = base / "project"
            project.mkdir()
            (project / "ship.goal.md").write_text(
                "## acceptance\n"
                "```json\n"
                '[{"type":"file_exists","path":"release.txt"}]\n'
                "```\n",
                encoding="utf-8",
            )
            result, response = self._run(
                "register", {"cwd": str(project)}, data_root=base / "goal-state"
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(response["outcome"], "registered")
            self.assertTrue(response["registered"])

    def test_verify_passed_clears_claim_and_exit_codes_differ_from_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            cwd.mkdir()
            (cwd / "done.txt").write_text("ready\n", encoding="utf-8")
            data_root = base / "state"
            register_result, registered = self._run(
                "register",
                {
                    "cwd": str(cwd),
                    "claim": {
                        "label": "satisfiable",
                        "predicates": [
                            {
                                "type": "file_contains",
                                "path": "done.txt",
                                "pattern": "ready",
                            }
                        ],
                    },
                },
                data_root=data_root,
            )
            self.assertEqual(register_result.returncode, 0)
            active_claim = next(data_root.rglob("active.json"))
            self.assertTrue(active_claim.is_file())

            passed_result, passed = self._run(
                "verify", {"cwd": str(cwd)}, data_root=data_root
            )
            missing_result, missing = self._run("verify", {}, data_root=data_root)

            self.assertEqual(passed_result.returncode, 0)
            self.assertEqual(passed["outcome"], "passed")
            self.assertEqual(passed["claim_id"], registered["claim_id"])
            self.assertTrue(passed["cleared"])
            self.assertTrue(passed["evidence"][0]["passed"])
            self.assertIsNone(passed["reason"])
            self.assertFalse(active_claim.exists())
            self.assertEqual(missing_result.returncode, 2)
            self.assertNotEqual(passed_result.returncode, missing_result.returncode)
            self.assertEqual(missing["error_kind"], "malformed_request")

            no_claim_result, no_claim = self._run(
                "verify", {"cwd": str(cwd)}, data_root=data_root
            )
            self.assertEqual(no_claim_result.returncode, 0)
            self.assertEqual(no_claim["outcome"], "no_active_claim")
            self.assertIsNone(no_claim["claim_id"])
            self.assertFalse(no_claim["cleared"])
            self.assertEqual(no_claim["evidence"], [])

    def test_verify_failed_keeps_claim_and_names_failed_predicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            cwd.mkdir()
            data_root = base / "state"
            _, registered = self._run(
                "register",
                {
                    "cwd": str(cwd),
                    "claim": {
                        "label": "failing",
                        "predicates": [{"type": "file_exists", "path": "missing.txt"}],
                    },
                },
                data_root=data_root,
            )
            active_claim = next(data_root.rglob("active.json"))

            result, failed = self._run("verify", {"cwd": str(cwd)}, data_root=data_root)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(failed["outcome"], "failed")
            self.assertEqual(failed["claim_id"], registered["claim_id"])
            self.assertFalse(failed["cleared"])
            self.assertFalse(failed["evidence"][0]["passed"])
            self.assertIn("file_exists", failed["reason"])
            self.assertIn("missing.txt", failed["reason"])
            self.assertTrue(active_claim.is_file())

            # Observe-by-default (§5): no config was ever written, so the
            # failure must report enforced=false AND land in the ledger as
            # would_have_blocked — the record /axiom:report reads.
            self.assertIs(failed["enforced"], False)
            ledger = next(data_root.rglob("ledger.jsonl"))
            events = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]
            observed = [e for e in events if e.get("event") == "would_have_blocked"]
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["rule"], "write-verify")
            self.assertEqual(observed[0]["hook"], "adapter_cli")
            self.assertTrue(observed[0]["failed"])
            _, still_active = self._run(
                "register",
                {
                    "cwd": str(cwd),
                    "claim": {"label": "replacement", "predicates": []},
                },
                data_root=data_root,
            )
            self.assertEqual(still_active["outcome"], "already_active")
            self.assertEqual(still_active["claim_id"], registered["claim_id"])

    @unittest.skipIf(os.geteuid() == 0, "root can write to a chmod 444 file")
    def test_unrecordable_observe_finding_is_never_silent(self) -> None:
        # Observe mode's value is the record. If the ledger cannot be written,
        # the host must still proceed, but the caller must be told the finding
        # was lost — otherwise a missed finding reads as a clean observe run.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            cwd.mkdir()
            data_root = base / "state"
            self._run(
                "register",
                {
                    "cwd": str(cwd),
                    "claim": {
                        "label": "failing",
                        "predicates": [{"type": "file_exists", "path": "missing.txt"}],
                    },
                },
                data_root=data_root,
            )
            # A healthy run records it.
            _, healthy = self._run("verify", {"cwd": str(cwd)}, data_root=data_root)
            self.assertIs(healthy["recorded"], True)

            ledger = next(data_root.rglob("ledger.jsonl"))
            ledger.chmod(0o444)
            try:
                environment = os.environ.copy()
                environment["CLAUDE_PLUGIN_DATA"] = str(data_root)
                result = subprocess.run(
                    [sys.executable, str(CLI_PATH), "verify"],
                    input=json.dumps({"cwd": str(cwd)}),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                response = json.loads(result.stdout)
            finally:
                ledger.chmod(0o644)

            self.assertEqual(result.returncode, 0)  # still fails open
            self.assertEqual(response["outcome"], "failed")
            self.assertIs(response["enforced"], False)
            self.assertIs(response["recorded"], False)  # and admits the loss
            self.assertIn("NOT recorded", result.stderr)

    def test_verify_failed_reports_enforced_after_operator_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            cwd.mkdir()
            data_root = base / "state"
            self._run(
                "register",
                {
                    "cwd": str(cwd),
                    "claim": {
                        "label": "failing",
                        "predicates": [{"type": "file_exists", "path": "missing.txt"}],
                    },
                },
                data_root=data_root,
            )
            env = os.environ.copy()
            env["CLAUDE_PLUGIN_DATA"] = str(data_root)
            enforced_run = subprocess.run(
                [
                    sys.executable,
                    str(CLI_PATH),
                    "enforce",
                    "write-verify",
                    "on",
                    "--cwd",
                    str(cwd),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(enforced_run.returncode, 0, enforced_run.stderr)

            _, failed = self._run("verify", {"cwd": str(cwd)}, data_root=data_root)
            self.assertEqual(failed["outcome"], "failed")
            self.assertIs(failed["enforced"], True)
            # An enforced failure is the host's to act on — it must NOT also be
            # double-counted as an observe finding.
            ledger = next(data_root.rglob("ledger.jsonl"))
            self.assertNotIn("would_have_blocked", ledger.read_text(encoding="utf-8"))

    def test_each_verb_wraps_malformed_json_and_missing_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for verb in ("register", "verify"):
                for request in ("{not-json", {}, {"cwd": "relative/path"}):
                    with self.subTest(verb=verb, request=request):
                        result, response = self._run(
                            verb, request, data_root=base / f"state-{verb}"
                        )
                        self.assertEqual(result.returncode, 2)
                        self.assertEqual(response["outcome"], "error")
                        self.assertEqual(response["error_kind"], "malformed_request")
                        self.assertIn("reason", response)

    def test_each_verb_wraps_internal_errors_and_keeps_stdout_pure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            cwd.mkdir()
            unusable_root = base / "not-a-directory"
            unusable_root.write_text("occupied", encoding="utf-8")
            result, response = self._run(
                "register",
                {
                    "cwd": str(cwd),
                    "claim": {"label": "claim", "predicates": []},
                },
                data_root=unusable_root,
            )
            self.assertEqual(result.returncode, 3)
            self.assertEqual(response["outcome"], "error")
            self.assertEqual(response["error_kind"], "internal")
            self.assertNotEqual(result.stderr, "")

            verify_root = base / "verify-state"
            self._run(
                "register",
                {
                    "cwd": str(cwd),
                    "claim": {
                        "label": "passing",
                        "predicates": [{"type": "file_exists", "path": "."}],
                    },
                },
                data_root=verify_root,
            )
            active_claim = next(verify_root.rglob("active.json"))
            claims_directory = active_claim.parent
            claims_directory.chmod(0o500)
            try:
                result, response = self._run(
                    "verify", {"cwd": str(cwd)}, data_root=verify_root
                )
            finally:
                claims_directory.chmod(0o700)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(response["outcome"], "error")
            self.assertEqual(response["error_kind"], "internal")
            self.assertNotEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
