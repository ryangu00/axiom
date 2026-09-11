# Known limitations

Axiom's whole premise is that unaudited claims are worthless — so here are
this tool's own, audited by an independent cross-family review before release.
None are correctness defects in the evidence chain (those were fixed before
shipping); they are documented boundaries and v1.1 hardening targets.

## What this won't catch (threat model)

Axiom catches the **careless** false "done" — the overwhelmingly common one,
where an agent reports success it never checked. It is not a seal against an
agent that is actively trying to get past it. If you need that, you need a
sandbox; this is for the loops you run outside one. Concretely, and verified
against the shipped code:

- **No claim, no check.** If nothing registered a claim for this project, Stop
  records an `unverified_completion` ledger event and lets the turn end. Axiom
  verifies evidence *you declared*; it does not infer claims from the
  transcript. (Automatic claim extraction — claimcheck's method — is credited
  on the v1.2 roadmap for exactly this gap.)
- **A predicate is a letter, not a spirit.** `file_exists` passes on an empty
  file; `cmd_succeeds` passes on a test that asserts nothing. An agent that
  writes a stub satisfies a weak predicate honestly. Your predicates are the
  specification — Axiom does not divine correctness you didn't declare.

  One shape of this is partly catchable: a suite that passes because the suite
  was changed. Give `cmd_succeeds` a `guard_paths` list and the run stops
  counting as a clean pass if those files are not, at verification time, the
  files that were there when the claim was registered.

  Read that sentence literally, because the limits are in it.

  - **It compares two points in time, not the interval between them.** An
    agent that copies a guarded test aside, weakens it, runs the suite, and
    restores the original bytes ends with a matching hash and a clean pass.
    Catching that needs the command to run against an immutable copy, or the
    guarded paths watched for the duration — neither is built. What
    `guard_paths` actually catches is the careless version: the tests were
    edited and left edited, which is the common one and the whole point of
    the tool, but it is not a seal.
  - **It catches *that* a file moved, never *whether* the change was honest.**
    A legitimate edit to a guarded test reports identically to a dishonest one.
  - **It cannot see a test that was weak from the start.** Nothing changed, so
    nothing is flagged.

  Held-out tests the agent never sees are the stronger answer, and a tool
  running on your machine, at your agent's permission level, on the same side
  of the work, cannot manufacture them.
- **The state is on your filesystem, at your agent's permission level.** The
  active claim lives at `<data-root>/v1/projects/<id>/claims/active.json`. An
  agent with write access to that path can remove it, and an agent can edit a
  `*.goal.md` before it is registered. Axiom raises the cost of a false "done"
  from *free* to *deliberate*; it does not make it impossible.
- **One block per stop cycle, on purpose.** After a block, the *re-entered*
  stop (`stop_hook_active: true`) fails open and writes an `escalation` event,
  so a wrong claim costs one extra cycle rather than looping forever — a
  verifier that can wedge your agent is worse than no verifier. The cap is per
  re-entry, **not** per claim: the claim stays active, and a later turn whose
  evidence still fails is blocked again. Enforcement never escalates on its
  own.
- **Observe mode blocks nothing.** That is the default and the point: you
  calibrate on your own loops first. Nothing is enforced until you enable it
  per rule.
- **Hooks run at the host's discretion.** Axiom sits on the official hook API;
  if a host changes when or whether hooks fire, Axiom's checks change with it.
  Verified host versions are pinned in [ADAPTERS.md](ADAPTERS.md); adapters
  fail open on anything they don't understand.
- **`cmd_succeeds` is fresh execution, not a sandbox.** The child inherits your
  permissions, environment, `PATH`, network, and filesystem. Argv-only
  execution, the executable allowlist, metacharacter rejection, and the timeout
  reduce injection surface; they are not a security boundary.

## Heuristics that are not seals

- **Memory injection quarantine is best-effort, not airtight.** The import
  filter matches common ASCII instruction phrasing (`ignore previous`,
  `you must`, …). Unicode homoglyphs (`іgnore`), zero-width characters, or
  novel phrasing can evade it. The load-bearing defense is *not* the filter —
  it is the `[unverified memory]` prefix plus the rule that recalled content
  is always treated as data, never as instructions. The quarantine is one
  defense-in-depth layer on top of that.
- **Quarantine currently scans lesson text, not `source`/`tags`.** If a caller
  renders full lesson metadata verbatim, instruction-shaped content in those
  fields is not filtered. Treat all recalled fields as untrusted. *(v1.1: run
  the same filter over metadata fields.)*

## Predicate semantics are deliberately narrow

- **`file_changed` means content changed** — the file exists now and its
  content hash differs from the recorded baseline. It intentionally does *not*
  treat a deletion, a permission-only change, or a symlink re-point to
  same-content as "changed." This is a declared narrow definition, not a bug;
  claim what a machine can unambiguously check.

## Advisory rules (non-blocking) with incomplete coverage

These rules *warn*, they do not block, so gaps affect hint accuracy, not
safety:

- **`preflight` irreversible-command matching** misses some shapes:
  newline-separated commands, `env rm …` / `command rm …`, absolute paths
  (`/bin/rm`), variable-indirect commands, `truncate -s 0`, `shred`, shell
  redirection overwrite, and `git push origin +ref`. It also flags
  `git push --force-with-lease` (a *safer* operation) via a `--force` prefix
  match. *(v1.1: broaden the pattern set and special-case force-with-lease.)*
- **`schema_guard` temp-path detection** recognizes `/tmp` and `/var/tmp`
  always, but macOS `$TMPDIR` (`/var/folders/.../T/`) only when that variable
  is present in the hook environment. *(v1.1: resolve platform temp roots
  independent of env.)*
- **`stuck-search` clustering** uses set-Jaccard over command tokens
  (threshold 0.4) and does not yet compare error fingerprints, so distinct
  commands sharing root tokens can cluster together, and a single incidental
  success can clear a cluster. This is a tuned tradeoff for v1, locked by
  tests; *(v1.1: add an error-signature dimension and decay instead of
  hard-clear.)*

## One claim per project, and only the first goal file

- **A project has exactly one active claim slot** (CONTRACTS §2). Registration
  is a compare-and-set: whoever gets there first owns the slot, and a second
  registration returns `already_active` rather than replacing it. That is
  deliberate — silently overwriting a live claim would let a later, easier
  claim erase an earlier, harder one. The consequence to be aware of: two
  concurrent sessions in the same project **share** that claim, so whichever
  one stops first is the one that gets verified against it.
- **Only the first `*.goal.md` (lexicographic) is registered.** If a project
  holds `a.goal.md` and `z.goal.md`, `z` is never registered while `a` holds
  the slot — it is not queued and there is no warning. Keep one active goal
  file per project, or register claims explicitly through the CLI.
- Neither is a multi-goal work queue, and v1 does not pretend to be one.

## Platform support

Axiom runs on Linux, macOS, and Windows. The state layer uses platform-appropriate
locking primitives (`fcntl` on POSIX, `msvcrt.locking` on Windows) and command
parsing (`shlex` on POSIX, `CommandLineToArgvW` on Windows). CI covers all three
platforms (Ubuntu, macOS, Windows) with Python 3.10-3.12.

Windows-specific notes:
- File locking is mandatory rather than advisory; concurrent readers may cause
  transient sharing violations during atomic file swaps. The implementation
  retries for up to 2 seconds to handle this gracefully.
- POSIX permission-based test fixtures (chmod 0o500) are skipped on Windows,
  as Windows does not enforce Unix-style permission bits against the file owner.
- The `preflight` rule recognizes Windows temporary directories (`%TEMP%`,
  `%TMP%`) in addition to POSIX paths (`/tmp`, `/var/tmp`, `$TMPDIR`).

## Unbounded reads (v1.2 targets)

The transcript scan is bounded (last 8 KiB only), but three paths are not:
`file_contains` and `file_changed` read the whole target file into memory,
`cmd_succeeds` captures unbounded stdout/stderr, and `SessionStart` parses the
entire ledger to build the report. A multi-gigabyte artifact, a very chatty
command, or a years-old ledger can make a hook slow. Nothing corrupts; it
degrades.

## Concurrency

`cmd_succeeds` runs a fresh child process with the invoking user's permissions,
environment, `PATH`, network, and filesystem. Its argv-only execution,
executable allowlist, metacharacter rejection, and timeout reduce injection
surface; they are not a security boundary.

- **Cross-session state uses atomic-rename writes.** The completion-claim path
  additionally takes an exclusive lock and does compare-and-clear (it only
  clears the claim it evaluated), so concurrent sessions cannot delete each
  other's claims. The advisory `stuck-search` cluster counter is
  atomic-rename only (no lock): under heavy concurrent failure across
  sessions, a failure increment can be lost. Advisory, not evidence-chain.
  *(v1.1: lock the cluster counter too.)*
- **`flock` degrades to no-lock on filesystems that don't support it** (some
  NFS mounts). `_claim_lock` records one process-deduplicated `lock_degraded`
  ledger event and proceeds *without* the lock rather than wedging the session,
  so on those filesystems the
  register/clear critical section loses its mutual exclusion and weakens to
  atomic-rename-only — the same guarantee as the advisory counter above. The
  compare-and-clear token check still prevents deleting a *foreign* claim; only
  register-vs-clear atomicity is lost. `/axiom:report` surfaces the degraded
  mutual-exclusion warning; there is no lockfile fallback in v1.1.

## Scope

- Thresholds are calibrated on one operator's workload — months of daily use
  across four execution lanes, so varied, but n=1. Observe mode exists
  precisely so you calibrate against *your* loops before enforcing.

## Post-audit items (independent dual-track review)

An independent cross-family audit ran before release. The two HIGH defects it
found (a non-atomic compare-and-clear window; malformed predicates dropped at
registration) were fixed and locked with regression tests. The remaining
findings are documented boundaries, not fixed in v1:

- **The privacy gate now scans commit metadata as well as tracked file
  content.** `--scan-all` covers both; `--scan-history` covers metadata alone.
  Across all reachable commits it checks the author and committer addresses
  against an allowlist (default: addresses on `users.noreply.github.com`) and
  the message
  against attribution-trailer patterns (default: `Co-Authored-By:` and
  `Generated with `), both configurable in `.privacy-gate.json`. Each finding
  names the ref that reaches the commit, because a bare SHA does not tell you
  whether the thing is published or only held by a stale local remote-tracking
  ref. Commit metadata gets its own rules rather than the file-content ones:
  every commit carries an author address, so reusing the email pattern there
  would flag the entire history.

  This gap was not theoretical. An AI attribution trailer and an employer
  address reached the published history of this repository through commit
  metadata and were found by hand, after publication, because the gate read
  tracked files and nothing else.

  Replacement objects are disabled for both reads. `git replace <dirty>
  <clean>` makes every ordinary local command show the clean object, but
  `refs/replace/*` is not pushed by default, so the remote keeps the original
  — a scan that honoured replacements would report clean about a history that
  was never published. A false green is worse than no scan.

  The whole commit object is read, not only the ident lines and the message: a
  `mergetag` header embeds an entire tag object, tagger identity included, and
  it is as published as the commit around it. `tree` and `parent` are skipped
  because they hold nothing but object names.

  Ident lines and headers have no declared encoding. Denylist literals are
  matched against those raw bytes in several candidate encodings — a list, not
  a guarantee — so where the comparison is inconclusive the scan refuses rather
  than reporting clean: nothing matched, the bytes are not valid UTF-8, and a
  denylist is configured. All three, because with no denylist there is no
  literal to miss and a legacy non-UTF-8 name is then not an obstacle. The
  message body is exempt from the rule for a different reason: it is decoded
  strictly in the encoding the object declares and raises if it will not, so it
  never reaches a comparison that could quietly conclude nothing.

  A shallow clone and a legacy grafts file both make the scan refuse rather
  than report. Each hides history from a local read while leaving it on the
  remote, and there is no honest way to certify commits the scan cannot reach.
  `git fetch --unshallow` first.

  Ident lines inside embedded objects — a `mergetag`'s tagger — are parsed as
  ident lines and get the same allowlist check as the commit's own author and
  committer, rather than being left to the text pattern. An address that a
  pattern cannot see, or can only see part of, is exactly what the structured
  check exists for.

  **Out of scope: hand-written objects.** The scan refuses anything it cannot
  read — a header that is not an entry git would have written, a message that
  will not decode as declared, an object whose length does not match — so a
  malformed object produces a refusal rather than a clean verdict. What it does
  not claim is complete *detection* inside one. `git hash-object -t commit
  --literally` writes objects that porcelain refuses to create, and an
  adversary willing to do that is the one this tool has always said it is not a
  seal against. Every gap found by ordinary git usage is closed; the raw-object
  surface is bounded by failing closed, not by promising to catch everything
  in it.

  **Annotated tags are scanned.** A tag object has its own tagger identity and
  message, and `git tag -a` followed by `git push` publishes both — ordinary
  porcelain, which is why this is covered rather than documented as a gap.
  Every ref under `refs/tags/` that points at a tag object goes through the
  same checks a commit does, from the same code, because a check that exists
  for commits and not for tags is how the two drift apart. A lightweight tag
  is a ref pointing straight at a commit and carries no metadata of its own.

  **Still out of scope: unreachable objects.** After a history rewrite the old
  commits stay fetchable by SHA until the hosting provider garbage-collects,
  and no local gate changes that — removing them needs the provider's help.
  A green gate certifies reachable history, not the absence of orphans.
- **`/axiom:uninstall` deletes within `data_root` and enumerates
  plugin-managed state there.** The opt-in official-memory file
  (`axiom-lessons.md` under the host's memory dir) is intentionally outside
  `data_root`; uninstall does not delete it (it is your memory), and a
  containment guard now refuses to delete anything resolving outside
  `data_root`. *(v1.1: list the opt-in memory file in the uninstall report so
  you can remove it yourself.)*
- **`schema_guard` in enforce mode can deny a genuinely-temporary write** whose
  filename matches a persistent-artifact pattern (e.g. a real throwaway
  `/tmp/config.json`). It is observe-only by default; enable enforcement per
  rule after reviewing your `/axiom:report`. *(v1.1: narrow the durable-artifact
  heuristic.)*
- The provider injection quarantine and the advisory `stuck-search` /
  `preflight` heuristics have the coverage boundaries already listed above; the
  audit confirmed them as best-effort, matching how they are documented.

## Provider layer & concurrency (from the code-quality review)

- **The provider layer is an extension interface, not wired into the runtime
  hooks by default.** The built-in verification path is the runtime hook;
  `providers/` gives you a reference `WriteVerifier` / `MemoryProvider` to point
  at your own backend. Runtime and provider predicate semantics now delegate to
  one canonical evaluator. The provider adapts its stateless `baseline_hash`
  input into the same injected baseline shape used by the runtime; all four
  predicates are parity-tested. The provider remains an opt-in extension rather
  than a runtime backend.
- **Claim registration is atomic when locking is available.**
  `register_goal_claim()` delegates to `register_claim_if_absent()`, which
  performs the absence check and write inside one locked critical section and
  returns a typed winner/loser result. New claims use `claim_id` for locked
  compare-and-clear; legacy claims without it are dual-read through their
  registration timestamp. The no-`flock` degradation described above remains.
- **Config load failures are observable and fail open.** `load_config()` returns
  a typed `ConfigLoad` distinguishing absent, valid, invalid, and unreadable
  state. Invalid or unreadable config falls back to observe mode and emits a
  deduplicated `config_degraded` ledger event surfaced by `/axiom:report`;
  ordinary absence uses defaults without a degraded warning.
