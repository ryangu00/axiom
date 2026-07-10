"""Filesystem and command write-verification predicates."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import VerifyResult, WriteVerifier

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import predicate_evaluator  # noqa: E402


class FsGitWriteVerifier(WriteVerifier):
    """Verify filesystem state and freshly re-run commands."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})

    def verify(self, predicate: dict[str, Any]) -> VerifyResult:
        cwd = Path(self.config.get("cwd", Path.cwd())).resolve()
        baseline: dict[str, Any] = {}
        if predicate.get("type") == "file_changed":
            path = predicate.get("path")
            if isinstance(path, str) and "baseline_hash" in predicate:
                baseline = {
                    "files": {
                        path: {
                            "exists": True,
                            "sha256": predicate.get("baseline_hash"),
                        }
                    }
                }
        evidence = predicate_evaluator.evaluate_predicate(
            predicate, cwd=cwd, baseline=baseline
        )
        return VerifyResult(
            evidence["passed"], evidence["expected"], evidence["actual"]
        )
