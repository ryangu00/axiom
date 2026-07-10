# Known limitations

Axiom's whole premise is that unaudited claims are worthless — so here are
this tool's own, audited by an independent cross-family review before release.
None are correctness defects in the evidence chain (those were fixed before
shipping); they are documented boundaries and v1.1 hardening targets.

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

## Concurrency

- **Cross-session state uses atomic-rename writes.** The completion-claim path
  additionally takes an exclusive lock and does compare-and-clear (it only
  clears the claim it evaluated), so concurrent sessions cannot delete each
  other's claims. The advisory `stuck-search` cluster counter is
  atomic-rename only (no lock): under heavy concurrent failure across
  sessions, a failure increment can be lost. Advisory, not evidence-chain.
  *(v1.1: lock the cluster counter too.)*
- **`flock` degrades to no-lock on filesystems that don't support it** (some
  NFS mounts). `_claim_lock` suppresses the `OSError` and proceeds *without* the
  lock rather than wedging the session, so on those filesystems the
  register/clear critical section loses its mutual exclusion and weakens to
  atomic-rename-only — the same guarantee as the advisory counter above. The
  compare-and-clear token check still prevents deleting a *foreign* claim; only
  register-vs-clear atomicity is lost. *(v1.1: lockfile fallback or an explicit
  health warning when flock is unavailable.)*

## Scope

- Thresholds are calibrated on one operator's workload — months of daily use
  across four execution lanes, so varied, but n=1. Observe mode exists
  precisely so you calibrate against *your* loops before enforcing.

## Post-audit items (independent dual-track review)

An independent cross-family audit ran before release. The two HIGH defects it
found (a non-atomic compare-and-clear window; malformed predicates dropped at
registration) were fixed and locked with regression tests. The remaining
findings are documented boundaries, not fixed in v1:

- **The `--scan-all` privacy gate scans tracked *file content* only.** It does
  not scan commit metadata (author/email) or unreachable history blobs. Treat
  history/identity sanitization as a separate manual pre-publish step, not
  something a green gate certifies. *(v1.1: extend the gate to metadata + full
  reachable history.)*
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
  at your own backend. The stateless predicates (`file_exists`, `file_contains`
  with a regex `pattern`) now give identical results in the runtime and the
  reference provider (parity-tested). `file_changed` intentionally differs: the
  runtime compares against a baseline captured at claim registration, while the
  stateless provider compares against a `baseline_hash` supplied in the
  predicate. *(v1.1: wire the provider into the runtime so there is a single
  implementation.)*
- **`register_goal_claim()` has a check-then-register TOCTOU.** The
  active-claim *clear* is atomic (locked compare-and-clear), but the
  "no claim exists yet → register from the goal file" path checks and writes
  across two steps; two sessions starting in the same project at the same instant
  could both register. Low real-world probability (one operator, one project at a
  time). *(v1.1: make check-absence + register a single locked critical section.)*
- **Config error handling is coarse.** Missing / invalid / unreadable config all
  fall back to observe mode; the hook fails open (correct for availability) but
  does not surface a distinct "degraded" state. *(v1.1: distinguish the cases and
  show degraded status in `/axiom:report`.)*
