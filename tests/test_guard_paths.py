"""Tests for `cmd_succeeds` guard paths.

An exit code cannot see the most common way a "tests pass" claim is satisfied
dishonestly: changing the tests. These tests pin the behaviour that closes that
gap -- the command passing is necessary but no longer sufficient when the claim
named files that must not move while the work happened.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
_spec = importlib.util.spec_from_file_location(
    "axiom_predicate_evaluator", HOOKS / "predicate_evaluator.py"
)
assert _spec and _spec.loader
evaluator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluator)

sys.path.insert(0, str(HOOKS))
import axiom_common as common  # noqa: E402  (path set above)

PASSING_COMMAND = [sys.executable, "-c", "pass"]
FAILING_COMMAND = [sys.executable, "-c", "raise SystemExit(1)"]


class GuardPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.cwd = Path(self._temporary.name)
        self.guarded = self.cwd / "test_thing.py"
        self.guarded.write_text("assert real_behaviour()\n", encoding="utf-8")
        self.addCleanup(self._temporary.cleanup)

    def _baseline(self) -> dict:
        """The baseline registration would have recorded for this predicate."""
        predicate = self._predicate()
        files = {
            path: evaluator.snapshot(evaluator.resolve_target(self.cwd, path))
            for path in evaluator.baseline_paths(predicate)
        }
        return {"files": files}

    def _predicate(self, command: list[str] | None = None) -> dict:
        return {
            "type": "cmd_succeeds",
            "cmd": command or PASSING_COMMAND,
            "guard_paths": ["test_thing.py"],
        }

    def _evaluate(self, baseline: dict, command: list[str] | None = None) -> dict:
        return evaluator.evaluate_predicate(
            self._predicate(command), cwd=self.cwd, baseline=baseline
        )

    def test_registration_baselines_the_guarded_paths(self) -> None:
        """Wiring: a guard that is never snapshotted can never be checked."""
        self.assertEqual(evaluator.baseline_paths(self._predicate()), ["test_thing.py"])
        self.assertIn("test_thing.py", self._baseline()["files"])

    def test_passes_when_the_guarded_file_did_not_move(self) -> None:
        self.assertTrue(self._evaluate(self._baseline())["passed"])

    def test_rewritten_test_file_is_not_a_clean_pass(self) -> None:
        """The headline case: the command exits 0 because the test was weakened."""
        baseline = self._baseline()
        self.guarded.write_text("assert True\n", encoding="utf-8")
        evidence = self._evaluate(baseline)
        self.assertFalse(evidence["passed"])
        self.assertIn("content changed", evidence["actual"])
        self.assertEqual(len(evidence["guard_violations"]), 1)

    def test_deleted_test_file_is_not_a_clean_pass(self) -> None:
        baseline = self._baseline()
        self.guarded.unlink()
        evidence = self._evaluate(baseline)
        self.assertFalse(evidence["passed"])
        self.assertIn("deleted", evidence["actual"])

    def test_guard_without_a_baseline_cannot_be_proven_and_does_not_pass(self) -> None:
        """Absence of evidence is not evidence of absence, and must not read as one."""
        evidence = self._evaluate({"files": {}})
        self.assertFalse(evidence["passed"])
        self.assertIn("no baseline recorded", evidence["actual"])

    def test_a_failing_command_reports_the_command_not_the_guard(self) -> None:
        """A failing command is already the thing to fix; do not bury it."""
        baseline = self._baseline()
        self.guarded.write_text("assert True\n", encoding="utf-8")
        evidence = self._evaluate(baseline, FAILING_COMMAND)
        self.assertFalse(evidence["passed"])
        self.assertIn("exit 1", evidence["actual"])
        self.assertNotIn("guard_violations", evidence)

    def test_without_guard_paths_behaviour_is_unchanged(self) -> None:
        evidence = evaluator.evaluate_predicate(
            {"type": "cmd_succeeds", "cmd": PASSING_COMMAND},
            cwd=self.cwd,
            baseline={"files": {}},
        )
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["expected"], "fresh command exits 0")

    def test_malformed_guard_paths_fail_closed(self) -> None:
        """A guard that cannot be checked must not read like a guarded run.

        An earlier version of this test asserted the opposite -- that a
        malformed value was simply ignored -- which blessed the exact failure
        the feature exists to prevent: the predicate is written with a guard,
        the guard protects nothing, and the run still reports a clean pass.
        """
        for bad in ("tests/test_x.py", 7, None, [], [1, 2], ["ok", 3]):
            with self.subTest(bad=bad):
                evidence = evaluator.evaluate_predicate(
                    {
                        "type": "cmd_succeeds",
                        "cmd": PASSING_COMMAND,
                        "guard_paths": bad,
                    },
                    cwd=self.cwd,
                    baseline={"files": {}},
                )
                self.assertFalse(
                    evidence["passed"], f"{bad!r} was accepted as a guarded run"
                )
                self.assertIn("guard_paths", evidence["actual"])

    def test_registration_actually_persists_the_guard_snapshot(self) -> None:
        """End-to-end: the guard has to survive the real registration path.

        Building the baseline from ``baseline_paths`` in a test proves the two
        helpers agree with each other, which they would even if registration
        never wrote a snapshot at all.
        """
        claim = {
            "label": "guarded",
            "predicates": [self._predicate()],
        }
        with tempfile.TemporaryDirectory() as state_root:
            outcome = common.register_claim_if_absent(
                claim, root=Path(state_root), cwd=self.cwd
            )
            self.assertTrue(outcome.registered)
            baseline = outcome.claim["baseline"]
            self.assertIn("test_thing.py", baseline["files"])

            # Same claim, weakened test, evaluated against the stored baseline.
            self.guarded.write_text("assert True\n", encoding="utf-8")
            evidence = evaluator.evaluate_predicate(
                self._predicate(), cwd=self.cwd, baseline=baseline
            )
            self.assertFalse(evidence["passed"])
            self.assertIn("content changed", evidence["actual"])


if __name__ == "__main__":
    unittest.main()
