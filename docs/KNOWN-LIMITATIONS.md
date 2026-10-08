# Known limitations

Axiom's whole premise is that unaudited claims are worthless — so here are
this tool's own, audited by an independent cross-family review before release.
None are correctness defects in the evidence chain (those were fixed before
shipping); they are documented boundaries and next-release hardening targets.

## What this won't catch (threat model)

Axiom catches the **careless** false "done" — the overwhelmingly common one,
where an agent reports success it never checked. It is not a seal against an
agent that is actively trying to get past it. If you need that, you need a
sandbox; this is for the loops you run outside one. Concretely, and verified
against the shipped code:

- **No claim, no check.** If nothing registered a claim for this project, Stop
  records an `unverified_completion` ledger event and lets the turn end. Axiom
  verifies evidence *you declared*; it does not infer claims from the
  transcript. (Automatic claim extraction — claimcheck's method — was weighed
  for exactly this gap and declined; [ROADMAP.md](ROADMAP.md) has the reasons.)
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
- **A goal file in a repository is repository-supplied command execution.**
  SessionStart registers the first `*.goal.md` in the project directory and
  the Stop hook evaluates its predicates *before* it reads the rule mode — so
  a `cmd_succeeds` predicate runs in observe mode too, on the first Stop after
  you open a clone. Keep live goal files out of repositories you did not
  write; this repository keeps its own example under `docs/examples/` with a
  name the glob does not match.

## Heuristics that are not seals

- **Memory injection quarantine is best-effort, not airtight.** The import
  filter matches common ASCII instruction phrasing (`ignore previous`,
  `you must`, …). Unicode homoglyphs (`ignore` with its first letter replaced by U+0456), zero-width characters, or
  novel phrasing can evade it. The load-bearing defense is *not* the filter —
  it is the `[unverified memory]` prefix plus the rule that recalled content
  is always treated as data, never as instructions. The quarantine is one
  defense-in-depth layer on top of that.
- **Quarantine currently scans lesson text, not `source`/`tags`.** If a caller
  renders full lesson metadata verbatim, instruction-shaped content in those
  fields is not filtered. Treat all recalled fields as untrusted. *(next
  release: run the same filter over metadata fields.)*

## Predicate semantics are deliberately narrow

- **`file_changed` means content changed** — the file exists now and its
  content hash differs from the recorded baseline. It intentionally does *not*
  treat a deletion, a permission-only change, or a symlink re-point to
  same-content as "changed." This is a declared narrow definition, not a bug;
  claim what a machine can unambiguously check.

## Which rules can block

`preflight` and `stuck-search` are advisory in both modes: they inject
guidance and record findings, never a block. `write-verify` and
`schema-guard` are observe-only by default and block (Stop decision) or deny
(PreToolUse Write/Edit) once you enable enforce for that rule. Shell targets
receive advisories only, including in enforce mode.

## Advisory rules with incomplete coverage

These rules *warn*, they do not block, so gaps affect hint accuracy, not
safety:

- **`preflight` irreversible-command matching** misses some shapes:
  newline-separated commands, `env rm …` / `command rm …`, absolute paths
  (`/bin/rm`), variable-indirect commands, `truncate -s 0`, `shred`, shell
  redirection overwrite, and `git push origin +ref`. It also flags
  `git push --force-with-lease` (a *safer* operation) via a `--force` prefix
  match. *(next release: broaden the pattern set and special-case
  force-with-lease.)*
- **`stuck-search` clustering** uses set-Jaccard over command tokens
  (threshold 0.4) and does not yet compare error fingerprints, so distinct
  commands sharing root tokens can cluster together, and a single incidental
  success can clear a cluster. This is a tuned tradeoff for v1, locked by
  tests; *(next release: add an error-signature dimension and decay instead
  of hard-clear.)*
- **`stuck-search` guidance: the only data we have does not show it being
  acted on.** The rule never blocks, in any mode. In observe mode it writes a
  ledger event and injects nothing; in enforce mode it adds
  stop-retrying-and-search guidance to the agent's context and the turn
  carries on. (Its observe-mode ledger event is named `would_have_blocked`
  like every other rule's; for this rule read that as "would have advised".)

  What an advisory nudge of this kind actually does was looked at on the
  private hook this rule derives from — one operator's workload, not this
  repository's implementation. Between 2026-07-09 and 2026-09-04 that hook
  fired 13 times. A transcript could still be found for 8 of them; the other
  5, all from 2026-07-09 to 2026-07-14, have none and are not in the
  denominator.

  The first reading of those 8 was wrong, and a draft of this entry repeated
  it. The diagnostic script took the next 15 tool calls from the top-level
  session transcript and found no web search or fetch call among them for any
  of the 8. But in 6 of the 8 the failures, and so the nudge, were in a
  sub-agent, and a sub-agent's transcript is a separate file the script never
  opened, so for those 6 the calls that were graded were not the calls that
  followed the nudge.

  Recounted by the agent that received the nudge, over its own next 15 tool
  calls, with any search or fetch call counting whether or not it had anything
  to do with the failure:

  - **1 of 8 was followed by a fetch inside the window.** It cannot be
    credited to the nudge: agents that had not been nudged were searching at
    the same time.
  - **2 of 8 landed in the top-level session.** Neither searched within the
    window; both searched later in the session.
  - **5 of 8 landed in a sub-agent that made no search or fetch call** in the
    rest of its run.

  For five of the six sub-agent firings the transcript does not record the
  injected text, so the receiving agent is taken to be the one with a failing
  tool result in the seconds before the hook fired. Exactly one sub-agent fits
  in each case, but it is an inference.

  The same recount over the ledger as it stood on 2026-10-03 covers 18
  firings: 10 are counted, with the same single one inside the window (the
  two added firings both landed in sub-agents, and neither searched
  afterwards); 3 have transcripts from unattended worker sessions and are left
  out because whether a search tool was available there was not verified (no
  search follows any of them); 5 have no transcript.

  What this does and does not support:

  - **It is not a compliance rate.** Eight events from five sessions (one
    session contributed three) by one operator are not independent samples.
    The supportable statement is that there is no firing here after which the
    nudge can be shown to have changed what the agent did next — not that the
    rate is zero, and not that it is one in eight.
  - **It does not measure this repository's hook.** The predecessor's wording,
    its thresholds (graded by failure class, with a cooldown) and its trigger
    surface all differ, and the public rule in its default mode injects
    nothing at all.
  - **A search after the nudge is not evidence that the nudge worked.** Any
    search or fetch call counts, relevant or not, and the one case inside the
    window is the one described above. A later search is weaker still: the
    stretch examined runs to the next firing or the end of the session, so an
    unrelated search hours afterwards counts.
  - **The hook's own ledger is not a better source.** It self-reports a search
    after 1 of the 13 firings (2 of 18 by 2026-10-03), counting any
    search-tool call in the session within 30 minutes. Both of those calls
    were made by a sibling sub-agent, not by the agent that was nudged.
  - **The recount is not the original diagnostic.** It was made while this
    entry was under review, with a one-off script over the same ledger and
    transcripts. The diagnostic itself still reads only top-level transcripts.
  - **It does not show that blocking would do better.** No blocking variant is
    built. At this volume — 13 firings over the roughly eleven weeks of
    ledger the first diagnosis covered (all of them in the first eight) — a change of threshold or wording could not be judged
    by live compliance anyway; it would need deterministic replay.

  What the rule does give you, in observe mode, is the record: it writes the
  failure cluster to the ledger, and `/axiom:report` shows that it happened
  and when. In enforce mode it injects the guidance and writes one
  `advice_injected` ledger event, so the report counts the firing. Treat the injected
  guidance as a note the agent may ignore, and do not count on this rule to
  stop a retry storm.

### Open refinement limits (2026-10)

- **Cancelled parallel calls still count as failures:** they are not filtered because the cancellation phrase was observed in recorded tool results, but its failure-event `error` field was checked only synthetically; live capture is still required, and host versions may change the phrase.
- **Failure clusters are not scoped per session:** concurrent sessions still share them because real session relationships have not been verified; whether sub-agents share their parent's session identifier is untested, so session scoping would not establish per-agent isolation.
- **Search-after-advice records can double count:** two search events from the same session arriving at the same moment both read the pending advice before either removes it, so the report can show two `search_after_trigger` records for one piece of advice. This affects the report only; it never changes what is blocked or advised.
- Polling detection is a heuristic over command text. The default recognizes
  loop constructs only; `rules.stuck-search.polling_patterns` replaces those
  expressions. A leading `sleep N &&` exemption is off by default. Opting in
  with a pattern such as `^sleep\s+\d+\s*&&` also exempts a broken command
  after the sleep. No labeled sample resolves that known hole.
- `rules.stuck-search.cooldown_minutes` defaults to 10 minutes per cluster
  in enforce mode. Observe findings still record every failure at or above
  the threshold. Advice counts and `search_after_trigger` counts appear
  separately in the report; neither populates recent observe incidents or
  calibration notices. The effect of cooldown and proposed session scoping
  on existing reports has not been evaluated.
- Search tracking records only the first successful matching tool within
  30 minutes of the latest injection in a session, with a lag in seconds.
  Another injection replaces that pending measurement; a successful Bash
  command does not remove it. `rules.stuck-search.search_tools` replaces the
  default tool-name expressions; an empty list disables tracking. Calls are
  matched to the payload session identifier, or to the shared missing-id
  bucket. Shared parent/sub-agent identifiers can misattribute a search, and
  neither relevance nor causation is established. The same atomic-rename-only
  concurrency limit as the cluster counter also applies to pending searches.
- No refinement has been measured on a replay set against the public hooks.
  Predecessor history supplies the evidence, not public measurement. The
  calibration commitment remains open.
- Shell target extraction is advisory and incomplete: it is not a shell
  evaluator, does not expand variables or execute substitutions, and can
  mistake quoted operators for syntax. Numeric and `&>` descriptor redirects
  are deliberately skipped. Unparsable command strings fail open.
- The built-in scratch-directory exemption follows one host's per-user
  naming convention on POSIX. Other hosts and adapters may need additional
  resolved directories in `rules.schema-guard.exempt_paths`. This does not
  exempt arbitrary temporary directories or similarly named sibling files.

`schema-guard` temp-path detection resolves the configured roots (`/tmp`,
`/var/tmp` by default), `TMPDIR`, the Windows `TEMP`/`TMP` variables, and
`tempfile.gettempdir()`. The `gettempdir()` result is excluded when its
resolved path equals the current working directory or an ancestor of it;
configured roots and explicit environment variables are not subject to that
exclusion. On macOS, libc `confstr(_CS_DARWIN_USER_TEMP_DIR)` also supplies
the per-user temporary directory, even without `TMPDIR`; the macOS test
compares it with `getconf DARWIN_USER_TEMP_DIR`. Discovery errors leave the
other roots intact. `preflight` uses the same resolver. In enforce mode,
`schema-guard` *can* deny a genuinely temporary write whose name matches a
persistent-artifact pattern (see "Post-audit items").

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
- `preflight` and `schema-guard` share one temp-root resolver that recognizes
  the Windows temporary directories (`%TEMP%`, `%TMP%`) and
  `tempfile.gettempdir()` in addition to POSIX paths (`/tmp`, `/var/tmp`,
  `$TMPDIR`).
- The shipped hook manifest (`hooks/hooks.json`) invokes `python3`. Python
  installed from python.org or Chocolatey exposes `python.exe` and `py.exe`
  but no `python3` alias (the Microsoft Store build does); on such installs
  every hook fails to start and the SessionStart health check cannot run
  either. Put a `python3` shim on `PATH` or use a Store build. CI switches to
  `python` on its Windows runner for the same reason.

## Unbounded reads

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
  *(next release: lock the cluster counter too.)*
- **`flock` degrades to no-lock on filesystems that don't support it** (some
  NFS mounts). `_claim_lock` records one process-deduplicated `lock_degraded`
  ledger event and proceeds *without* the lock rather than wedging the session,
  so on those filesystems the
  register/clear critical section loses its mutual exclusion and weakens to
  atomic-rename-only — the same guarantee as the advisory counter above. The
  compare-and-clear token check still prevents deleting a *foreign* claim; only
  register-vs-clear atomicity is lost. `/axiom:report` surfaces the degraded
  mutual-exclusion warning; there is no lockfile fallback yet.

## Scope

- Thresholds were set from one operator's workload — months of daily use
  across four execution lanes, so varied, but n=1 — and are not yet
  calibrated in the sense [CALIBRATION.md](CALIBRATION.md) describes. Observe mode exists
  precisely so you calibrate against *your* loops before enforcing. What has
  and has not been measured for `write-verify` is in
  [CALIBRATION.md](CALIBRATION.md); nothing is published yet for the
  `stuck-search` threshold, and that page says why.

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

  **Reachable object content is scanned too.** A file committed and then
  deleted is gone from the working tree and still in the history, still
  reachable, still published by the next push — a tracked-file scan answers
  what the repository looks like now, not what it hands to whoever clones it.
  `--scan-blobs` reads the content of every blob reachable from any ref;
  `--scan-all` includes it. Note bodies arrive the same way: `git notes`
  stores them as blobs, so the note's commit is metadata the history pass
  reads while its content is only visible here. A blob is skipped as binary
  only when it has a NUL byte near the start, which is git's own test; a text
  file carrying one stray invalid byte is still read.

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
  message, and pushing a tag made with `git tag -a` publishes both — ordinary
  porcelain, which is why this is covered rather than documented as a gap.
  Every ref that points at a tag object, under `refs/tags/` or anywhere else,
  goes through the same checks a commit does, from the same code, because a
  check that exists for commits and not for tags is how the two drift apart.
  A lightweight tag is a ref pointing straight at a commit and carries no
  metadata of its own.

  **Still out of scope: unreachable objects.** After a history rewrite the old
  commits stay fetchable by SHA until the hosting provider garbage-collects,
  and no local gate changes that — removing them needs the provider's help.
  A green gate certifies reachable history, not the absence of orphans.

  The ways this scan reported clean while it was being built — fifteen
  classes, each with its regression test, nine of them with a reproduction in
  ordinary git — are catalogued in
  [HISTORY-SCAN-FALSE-GREENS.md](HISTORY-SCAN-FALSE-GREENS.md).
- **`/axiom:uninstall` deletes within `data_root` and enumerates
  plugin-managed state there.** The opt-in official-memory file
  (`axiom-lessons.md` under the host's memory dir) is intentionally outside
  `data_root`; uninstall does not delete it (it is your memory), and a
  containment guard now refuses to delete anything resolving outside
  `data_root`. *(next release: list the opt-in memory file in the uninstall report so
  you can remove it yourself.)*
- **`schema_guard` in enforce mode can deny a genuinely-temporary write** whose
  filename matches a persistent-artifact pattern (e.g. a real throwaway
  `/tmp/config.json`). It is observe-only by default; enable enforcement per
  rule after reviewing your `/axiom:report`. Writes inside the host-managed
  per-session scratch directory are exempt, because that directory is where
  the host tells the agent to put temporary files. *(next release: narrow the durable-artifact
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
