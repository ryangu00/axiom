#!/usr/bin/env python3
"""Compatibility entry point for the hooks test suite."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

test_hooks = importlib.import_module("tests.test_hooks")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_hooks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
