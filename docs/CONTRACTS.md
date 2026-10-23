# Behavior contracts

These six contracts are the arbitration baseline for the pre-0.1.0 refactor. Code,
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

  **Optional `guard_paths` (list[str]).** Paths that must not move between
  registration and evaluation. Registration snapshots them into the same
  `baseline.files` map `file_changed` uses; on evaluation, a command that
  exits 0 while any guarded path was changed, deleted, created, or was never
  baselined does **not** pass. The evidence says which path and how, in a
  `guard_violations` list, and the failure text names the guard rather than
  the exit code. A command that failed reports its own failure and skips the
  guard check — a failing command is already the thing to fix.

  Which paths a predicate needs baselined is decided in one place
  (`predicate_evaluator.baseline_paths`) and consulted by both registration
  and evaluation. A guard that is declared but never snapshotted is the exact
  shape of a check that reads as a pass, so it is a violation, not a skip.

  A `guard_paths` value that is present but not a non-empty list of non-empty
  strings **fails the predicate**. Registration cannot reject it (it has no
  failure channel), so evaluation must: a guard that silently applies to
  nothing, in a predicate written specifically to be guarded, is the one
  outcome worse than declaring no guard at all.

  This exists because an exit code cannot see the most common way a "tests
  pass" claim is satisfied dishonestly: changing the tests. No model is
  involved — it is a SHA-256 comparison against a baseline the claim already
  recorded. It compares registration time against verification time and
  nothing in between, so an edit that is reverted before the turn ends is not
  visible to it; see KNOWN-LIMITATIONS for the full boundary.

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

**Legacy compatibility (dual-read).** Claims written before `claim_id` existed lack
`claim_id`. Readers fall back to `baseline.registered_at` as the comparison
token for exactly these claims. A legacy claim is never auto-cleared on
mismatch and never treated as foreign-clearable. A later release may drop the fallback.

**Lock degradation.** On filesystems without `flock`, locking degrades to
no-lock (atomic-rename writes only). This degradation MUST be observable: a
`lock_degraded` ledger event (deduplicated per process) and surfacing in the
report. No lockfile fallback yet — a stale-lock/crash-recovery protocol
is out of scope, and a half-built one is worse than an honest warning. While
degraded, the corrupt-slot quarantine below shares the lost exclusion: two
registrations that both read a corrupt slot can each quarantine, and the
second may file the first's freshly written, valid claim as corrupt. The
bytes are kept under the quarantine name and the record names it, so the
claim is recoverable, but its `status` describes the bytes that were read,
not the ones that were moved.

**Corrupt slot.** A slot that exists but does not parse as a claim (invalid
JSON, not an object, or unreadable) is `claim_unreadable`. It is **never**
treated as empty: `read_active_claim` raises `ClaimSlotCorrupt` instead of
returning `None`, so the Stop path cannot mistake it for "no claim" — that is
the one state a false "done" needs. The Stop hook records it; in observe mode
the turn ends, in enforce mode it blocks once and honors the re-entry cap like
any failed claim. Registration does not overwrite it: inside the lock the slot
is renamed to `active.json.corrupt-<utc stamp>` (bytes intact), a
`claim_unreadable{action: quarantined, moved_to}` event is appended, and the
new claim takes the empty slot. Quarantined copies are plugin-managed state
(`manifest()` lists them, so uninstall removes them). `clear_active_claim`
never deletes a corrupt slot (its compare fails).

## 3. Config loading

`load_config(path) -> ConfigLoad(status, data, reason)` with
`status ∈ {absent, valid, invalid, unreadable}`:

- `absent` — normal initial state: defaults, **no** degraded event, no noise.
- `valid` — parsed JSON object.
- `invalid` — JSON decode error or JSON that is not an object.
- `unreadable` — `OSError` on read.

`invalid`/`unreadable` fail open to observe-mode defaults AND emit a
structured `config_degraded` ledger event (`status`, `path`, `reason`,
`hook`). A hook records it at most once per session for each broken state of
a path: the status plus a fingerprint of the bytes, kept in
`config-degraded.json` next to the ledger (a fingerprint, never the
content). The marker remembers the 8 most recent broken states of a path and
the 32 most recent sessions of each; a state or session past that is
recorded again. A repaired config is forgotten, so breaking it again is
recorded again. A config that is not UTF-8, nests too deeply for the JSON
parser, or holds an integer over 4,300 digits is `invalid`. Two hooks that load the config in parallel for one tool call can both
record it, and a marker that cannot be written costs a duplicate, never the
record. CLI loads record once per invocation. The report's count is
therefore episodes, not tool calls. `get_report_data()` aggregates these
(latest reason + count) and the CLI report renders them, so "configured observe" and "broken config observing by
accident" are distinguishable. All consumers (`preflight`, `schema_guard`,
`stuck_search`, `write_verify`, CLI `modes`/`enforce`) migrate to the typed
loader; none re-implement parsing.

## 4. Versioning

`SCHEMA_VERSION` stays `v1`; `claim_id` is additive with the dual-read window
above. The provider command-field unification is a pre-publication breaking
change recorded in CHANGELOG. Contract changes after publication require a
version bump note in this file.

## 5. Adapter CLI wire contract (`axiom-adapter-cli/v1`)

The host-neutral primitive every adapter shim calls. Two subcommands of the
CLI: `register` and `verify`. Request: one JSON object on stdin. Response:
one JSON object on stdout — **stdout carries nothing else**; human-readable
text goes to stderr. Every response carries
`"protocol": "axiom-adapter-cli/v1"`.

**The CLI returns facts, never host semantics.** No `block`, `revise`, or
`continue` ever appears in a CLI response; those live in the per-host mapping
below. A response that leaked host semantics would make three shims drift
three ways — the exact failure this section exists to prevent.

### `register`

Request: `{"cwd": "<abs path>"}` (required) plus either
`{"claim": {"label", "predicates": [...]}}` or nothing (goal-file discovery —
`register_goal_claim()` semantics, §2). Response:

```json
{"protocol": "axiom-adapter-cli/v1",
 "outcome": "registered" | "already_active" | "no_goal_found" | "error",
 "registered": true|false, "claim_id": "<uuid>"|null, "reason": "<str>"|null}
```

`already_active` returns the existing claim's id (loser semantics, §2 —
never an exception, never an overwrite).

### `verify`

Request: `{"cwd": "<abs path>"}`. Evaluates the active claim exactly as the
runtime Stop path does: fresh evaluation (§1), compare-and-clear **only on
pass** (§2). A slot that exists but is not a claim (§2) is a `failed` outcome
whose single evidence item has `type: "claim_slot"`, never `no_active_claim`
and never `error`: an error would have every host fail open, which would make
scribbling over the slot cheaper than deleting it on exactly the hosts that
cannot see the Stop hook. Response:

```json
{"protocol": "axiom-adapter-cli/v1",
 "outcome": "passed" | "failed" | "no_active_claim" | "error",
 "claim_id": "<uuid>"|null, "cleared": true|false,
 "enforced": true|false,           // present when outcome is "failed"
 "recorded": true|false,           // present when outcome is "failed": the record for THIS outcome
 "evidence": [<§1 evidence objects>], "reason": "<one-line failure summary>"|null}
```

`reason` is the host-injectable failure text (which predicates failed,
expected vs actual). `no_active_claim` is a normal outcome, not an error.

**Observe-by-default is part of this contract, not a Claude Code feature.**
On `failed` the CLI reads the same `write-verify` rule mode the Stop hook
reads (§3) and returns it as `enforced`. When `enforced` is `false` (the
install default) the CLI has already appended the `would_have_blocked` ledger
event — the same record `/axiom:report` reads — the claim is left active, and
the host MUST NOT act: no block, no continue, no revise. `enforced: true`
requires the operator to have run `/axiom:enforce write-verify on` for that
project. A shim that acts on `failed` without checking `enforced` violates
this contract and turns a zero-risk install into an enforcing one.

Every `verify` outcome leaves the same ledger record the Stop hook leaves
(§6) under `hook: "adapter_cli"`: `unverified_completion` on
`no_active_claim` (with `completion_declaration: null` — the CLI has no
transcript), `verified` on `passed`, `would_have_blocked` on an observed
failure and `blocked` on an enforced one, `claim_unreadable` (`action`
observed or blocked by the same rule mode) on a corrupt slot, and
`error{action: fail_open}` when `register` or `verify` raises (exit 3). The
appends are best-effort and never change the outcome.

Observe mode's value *is* the record, so the CLI reports whether it got one.
`recorded: false` means the append for this outcome raised — the record is **not confirmed**,
which is the honest claim: an append can fail at flush or fsync after writing
part of the line, so absence is not provable, only non-confirmation. The CLI
prints the raised exception on stderr without guessing its cause. The host
still proceeds — a verifier must not wedge its host over a logging failure —
but an unconfirmed record must never be presentable as a clean observe run.

### Exit-code matrix

| exit | meaning | stdout |
|---|---|---|
| 0 | request processed; outcome is one of the non-error enums | full response JSON |
| 2 | malformed request (bad JSON, missing `cwd`) | response with `outcome:"error"`, `error_kind:"malformed_request"` |
| 3 | internal error (evaluator/state failure) | response with `outcome:"error"`, `error_kind:"internal"`; an `error{action: fail_open}` event is recorded when the ledger is reachable |

**Hosts MUST fail open** on any nonzero exit, missing stdout, or unparseable
JSON: the agent proceeds, the adapter logs an observable error. A verifier
that can wedge its host is worse than no verifier.

### Per-host verdict mapping (frozen here, implemented in each shim)

The action column applies **only** to `failed` with `enforced: true`.
`failed` with `enforced: false` behaves like the pass column on every host —
the CLI has already recorded the finding.

| host | `failed` + `enforced` | `failed` + observe / `passed` / `no_active_claim` | `error` |
|---|---|---|---|
| Claude Code (shipped hooks) | Stop `{"decision":"block","reason"}` | pass silently | fail-open (existing behavior) |
| Codex — native hooks (if probe passes) | Stop `{"decision":"block","reason"}` | pass silently | fail-open |
| Codex — exec wrapper (fallback) | do not launch next turn; `resume` with `reason` prepended | proceed | proceed + log |
| hermes-agent | `pre_verify` → `{"action":"continue","message": reason}` | return `None` | return `None` + log |
| OpenClaw | `before_agent_finalize` → `{"action":"revise","reason", "retry":{"instruction": reason, "idempotencyKey": claim_id+attempt, "maxAttempts": 1}}` | `{"action":"finalize"}` / no-op | no-op + log |

### Re-entry cap (every adapter)

A failed verify may drive at most **one** revise/continue/block-reinject
cycle per claim attempt (the Claude Code adapter's `stop_hook_active`
equivalent). After the cap: fail open and write an observable event — the
shipped Claude Code Stop hook writes `escalation` to the ledger (§6; a corrupt
slot writes `claim_unreadable{action: fail_open_after_reentry}`), the shims
log `verify_reentry_capped` on their own channel (stderr, Codex's
`adapter-events.jsonl`), since a shim never writes the ledger. Unbounded
revise loops are a denial of service against the user's own agent.

## 6. Ledger events

The ledger (`<project>/ledger.jsonl`, one JSON object per line, `timestamp`
added on append) is the only record of what the hooks saw and did. Every
event name is listed here; a hook that invents one is wrong.

| event | written by | meaning |
|---|---|---|
| `heartbeat` | SessionStart | the hooks ran this session (coverage, not a finding) |
| `config_degraded` | any hook, CLI | config invalid/unreadable; running on defaults (§3) |
| `lock_degraded` | claim lock | no `flock`; register/clear lost mutual exclusion (§2) |
| `error` + `action: fail_open` | any hook, CLI `register`/`verify` | the hook raised and let the host proceed unchecked |
| `unverified_completion` | Stop, CLI `verify` | a turn ended with no claim registered |
| `claim_unreadable` | Stop, CLI `verify`, registration | the slot exists but is not a claim (§2); `action` ∈ observed, blocked, fail_open_after_reentry, quarantined |
| `would_have_blocked` | any rule in observe mode | what the rule would have done |
| `blocked` | `write-verify` Stop/CLI, `schema-guard` deny | what the rule did, once enforced |
| `asked` | `preflight` in enforce mode | the hook returned an `ask` decision before the command ran; whether a human saw it (a `-p` run denies instead) and what they answered is not visible to hooks |
| `advice_injected` | `stuck-search`, `schema-guard` shell targets, in enforce mode | guidance was injected; nothing was blocked |
| `search_after_trigger` | `stuck-search` | a search followed injected advice |
| `escalation` | Stop | re-entered stop failed open after a block (the one-cycle cap) |
| `verified` | Stop, CLI `verify` | every predicate passed; `cleared` says whether the compare-and-clear removed the evaluated claim (false when a concurrent register replaced it, §2) |
| `write_readback` | PostToolUse Write/Edit | stat-based read-back of a write; never blocks |
| `mode_changed` | CLI `enforce` | an operator decision, with what `--by` asserted as `decided_by` |

**Enforced decisions are recorded.** A rule that only wrote `would_have_blocked`
would go blind the moment it was enforced, and nothing about it could be
measured afterwards. The enforce-mode record (`blocked`, `asked`, `advice_injected`) is
appended **before** the decision is returned and is best-effort: a failed
append is reported on stderr and never converts a block into a pass.

**`finding_id`** (16 hex chars) joins every record about one finding so a
labeling pass can follow it across stops without parsing claim bodies. For
`write-verify` it is `sha256("write-verify" ⟂ claim_id)[:16]` (the legacy
`registered_at` token for claims without an id), so the observe record, the
block, and the `verified` that eventually closes it share one id. For
`schema-guard` it is derived from the resolved path; for `preflight` from the
pattern class and the command text (a command that matches two classes gets
two records, one per class); for `stuck-search` from the moment the
failure cluster opened (`first_at`), so `would_have_blocked`,
`advice_injected` and the `search_after_trigger` that answers the advice share
one id; `claim_unreadable` records use `sha256("write-verify" ⟂ "slot" ⟂
<slot path>)[:16]`, so the Stop record, the CLI record and the quarantine
share one. It hashes identity only, never evidence, so it does not change as
the evidence does. It is a correlation key, not an integrity token: the inputs
are readable by the agent and the ledger is unsigned, so a record carrying a
given id proves nothing about who wrote it (KNOWN-LIMITATIONS: state lives at
the agent's permission level).

**Fail-open is visible on three channels.** A hook that raises prints why to
stderr, appends `error{action: fail_open}` when it can reach the ledger, and
returns `{"systemMessage": ...}` on stdout with no decision field — the host
proceeds exactly as if the hook had said nothing, but the operator is told.
`systemMessage` is the common hook-output field of the pinned Claude Code
hooks reference (ADAPTERS.md); this repository verifies that the hooks emit
it (seam tests), not that a given host renders it, and a host that ignores
the field still proceeds. `/axiom:report` renders the count and the latest
episode.
