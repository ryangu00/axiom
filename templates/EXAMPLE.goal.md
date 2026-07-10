# GOAL: add-ci-pipeline (forged 2026-07-01)

<!-- Fully fictional example: adding CI to an open-source Python library. -->

---
status: done   # closed 2026-07-03 after all criteria diffed green
---

```yaml
goal: every push to main runs lint + tests on 3 Python versions, and a red build blocks merge
context: >
  Repo "acme-parser" (pytest suite exists, no CI). GitHub Actions chosen —
  repo already lives on GitHub, zero new vendors. Budget: free tier only.
risk: 2
rollback: delete .github/workflows/ci.yml; branch protection toggle reverts in settings
done_criteria:
  - .github/workflows/ci.yml exists and a push to a scratch branch shows a green run for 3.10/3.11/3.12
  - a deliberately broken test on a scratch branch produces a red run
  - branch protection on main requires the CI check (screenshot or gh api output)
tasks:
  - id: t1
    what: workflow file (lint via ruff, tests via pytest, matrix 3 versions)
    route: self
    accept: green run link on scratch branch
    status: done   # run 4417 green on all 3 versions
  - id: t2
    what: prove red path — commit a failing assert on scratch branch
    route: self
    accept: red run link, then revert commit
    status: done   # run 4418 red as expected; reverted
  - id: t3
    what: branch protection requiring the check
    route: self
    accept: gh api repos/:owner/:repo/branches/main/protection shows required check "ci"
    status: done
changelog:
  - {ts: 2026-07-01T10:00, change: forged, why: two regressions shipped last month; tests exist but nobody runs them}
  - {ts: 2026-07-02T09:14, change: dropped planned 3.9 from matrix, why: library setup.py already requires >=3.10 — testing 3.9 verifies nothing}
  - {ts: 2026-07-03T16:40, change: all tasks done, criteria diffed, closed, why: three green evidence links recorded above}
```

## acceptance
```json
{"predicates": [
  {"type": "file_exists",   "path": ".github/workflows/ci.yml"},
  {"type": "file_contains", "path": ".github/workflows/ci.yml", "pattern": "3\\.1[012]"},
  {"type": "cmd_succeeds",  "cmd": "python -m pytest -q", "timeout": 300}
], "label": "ci pipeline live"}
```
