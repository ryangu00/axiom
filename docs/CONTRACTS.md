# Behavior contracts (v1.1)

These four contracts are the arbitration baseline for the v1.1 refactor. Code,
tests, and docs that disagree with this file are wrong; changing a contract
requires editing this file first, in its own commit, with a changelog entry.

## 1. Predicate evaluation

One evaluator module owns predicate semantics. The runtime Stop hook and the
reference `WriteVerifier` provider both delegate to it — proven structurally
(a delegation test monkeypatches the evaluator and asserts both entry points
observe the patch), not just behaviorally.

**Entry point.** `evaluate_predicate(predicate, *, cwd, baseline) -> Evidence`.
`cwd` is always explicit — relative paths resolve against it (never against
the process cwd). `baseline` is injected by the caller; the evaluator holds no
state.

**Evidence shape.** Every evaluation returns a mapping with at least:
`type`, `passed` (bool), `expected` (str), `actual` (str), plus `path` for
file predicates, `cmd` for command predicates, and
`baseline_sha256`/`current_sha256` for `file_changed`. A predicate that cannot
be evaluated (non-mapping, unknown `type`, missing required field) yields
**failed evidence** — it is never dropped, and it fails the whole claim.

**Per-predicate semantics.**

- `file_exists` — `path` expanduser'd; absolute used as-is, relative joined to
  `cwd` and resolved. Passes iff the path exists.
- `file_contains` — `pattern` must be a non-empty `str`; it is a regex applied
  with `re.search` to the file read as UTF-8. Empty/missing pattern fails (no
  match-all). Invalid regex fails with `invalid pattern`. Missing/unreadable
  file fails.
- `file_changed` — passes iff the file exists now AND (the baseline entry for
  it is absent/non-existent OR the current SHA-256 differs from the baseline
  SHA-256). The runtime supplies `baseline` from the registered claim's
  snapshot; the stateless provider adapts `baseline_hash` from the predicate
  into the same injected shape. Deletion is not "changed".
- `cmd_succeeds` — the command field is `cmd` (canonical): either an argv
  `list[str]`, or a `str` without shell metacharacters (rejected set includes
  `; | & $ ` < > ` and newlines) split with `shlex`. The executable must be on
  the allowlist (`cargo go git make node npm npx pnpm pytest ruby uv yarn` or
  `python*`). Timeout clamps to 1–600 s (default 120). Runs with
  `shell=False`, capture, `check=False`; passes iff exit 0. The provider-side
  `command`/free-executable variants are removed (pre-publication breaking
  change, no external users; recorded in CHANGELOG).

**What this is not.** `cmd_succeeds` is *fresh execution*, not a sandbox: the
child inherits the invoking user's permissions, environment, PATH, network,
and filesystem. The protections (argv-only, allowlist, metacharacter
rejection, timeout) reduce injection surface; they are not a security
boundary. Docs must use this framing wherever the predicate is described.

## 2. Claim lifecycle (single slot)

**Schema.** A claim has `claim_id` (uuid4 string — identity), `registered_at`
(ISO timestamp — audit metadata only, never identity), `label`, `predicates`,
`baseline` (`git_head`, `files{path: {exists, sha256, mtime_ns}}`).

**Registration.** `register_claim_if_absent(...)` is the only write path
(`register_goal_claim` must route through it). Inside one `_claim_lock`
critical section it re-reads for absence and either writes and returns
`(registered=True, claim=own)` or returns `(registered=False, claim=existing)`
— a typed winner/loser outcome, never an exception for the loser. Baseline
capture may run before the lock (wasted work for the loser is acceptable; a
lost or overwritten winner is not).

**Concurrency invariants** (the double-process tests assert exactly these):
1. Two concurrent registers → exactly one winner; the claim on disk equals
   the winner's; the loser observes an explicit loser result.
2. A Stop evaluating claim A concurrent with a register of claim B never
   deletes B — clear is compare-and-clear on `expected_claim_id`.
3. A successful Stop clears only the claim it evaluated.

**Legacy compatibility (dual-read).** Claims written before v1.1 lack
`claim_id`. Readers fall back to `baseline.registered_at` as the comparison
token for exactly these claims. A legacy claim is never auto-cleared on
mismatch and never treated as foreign-clearable. v1.2 may drop the fallback.

**Lock degradation.** On filesystems without `flock`, locking degrades to
no-lock (atomic-rename writes only). This degradation MUST be observable: a
`lock_degraded` ledger event (deduplicated per process) and surfacing in the
report. No lockfile fallback in v1.1 — a stale-lock/crash-recovery protocol
is out of scope, and a half-built one is worse than an honest warning.

## 3. Config loading

`load_config(path) -> ConfigLoad(status, data, reason)` with
`status ∈ {absent, valid, invalid, unreadable}`:

- `absent` — normal initial state: defaults, **no** degraded event, no noise.
- `valid` — parsed JSON object.
- `invalid` — JSON decode error or JSON that is not an object.
- `unreadable` — `OSError` on read.

`invalid`/`unreadable` fail open to observe-mode defaults AND emit a
structured `config_degraded` ledger event (`status`, `path`, `reason`,
`hook`), deduplicated to at most one per hook invocation per path.
`get_report_data()` aggregates these (latest reason + count) and the CLI
report renders them, so "configured observe" and "broken config observing by
accident" are distinguishable. All consumers (`preflight`, `schema_guard`,
`stuck_search`, `write_verify`, CLI `modes`/`enforce`) migrate to the typed
loader; none re-implement parsing.

## 4. Versioning

`SCHEMA_VERSION` stays `v1`; `claim_id` is additive with the dual-read window
above. The provider command-field unification is a pre-publication breaking
change recorded in CHANGELOG. Contract changes after publication require a
version bump note in this file.
