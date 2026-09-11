# Roadmap

What is deliberately not built yet, and why. Written down so the same
evaluation does not get redone from scratch — a negative result costs as much
to reach as a positive one and is thrown away far more often.

Ordering is by value against cost, with one veto: **nothing ships that puts a
model in the verification path.** That property is the whole reason to prefer
this over asking a judge, and a feature that trades it away is not a better
version of Axiom, it is a different tool.

## Shipped since the first release

- **Commit-metadata privacy scan.** `--scan-all` covers reachable commit
  metadata; `--scan-history` runs it alone. Was promised and unbuilt, and the
  gap reached published history before it was closed.
- **`cmd_succeeds` guard paths.** A suite that passes while a guarded file is
  no longer the file that was there at registration is not a clean pass. It
  compares two points in time, so an edit reverted before the turn ends is
  invisible to it — the boundary is spelled out in KNOWN-LIMITATIONS.

## Not doing

### Automatic claim extraction from the transcript

The largest single gap: nothing registered a claim means nothing is checked.
[`claimcheck`](https://github.com/ojuschugh1/claimcheck) (MIT) does exactly
this — parses a session transcript, extracts concrete claims, verifies each
against the filesystem, git, and lockfiles, with no model involved.

**Not doing it, for two reasons that are worth stating plainly:**

Extraction by regex has a ceiling its own author documents: unusual phrasing is
missed, function and line counts can never be derived, and a bug-fix claim with
no file path in it is unverifiable. Raising that ceiling means putting a model
in the extraction step — and a verdict is only as deterministic as the weakest
step that produced it. A model choosing *what gets checked* decides the outcome
as surely as a model choosing the verdict.

And the premise underneath is unevidenced. The argument for extraction is that
declaring predicates is the adoption barrier. `claimcheck` removes that barrier
entirely and has roughly the same usage this project does, which is the only
data point available and it points the other way.

Revisit if: someone reports abandoning Axiom *specifically* because of the
declaration step, or extraction stops needing a model to be good.

### Bounded reads

`file_contains` and `file_changed` read whole files into memory, `cmd_succeeds`
captures unbounded output, and `SessionStart` parses the whole ledger. A
multi-gigabyte artifact, a chatty command, or an old ledger makes a hook slow.

The commit-metadata scan joins them: it reads the identity stream and every
commit body into memory at once. That one has an interim answer rather than a
fix — it refuses out loud above a ceiling, because a gate killed part-way
through is a gate that was not enforcing, and an explicit refusal is at least
legible. Streaming is the real answer for all of them.

Real, and cheap to fix — streaming hashes, a capture cap, a ledger tail. Not
first because nothing corrupts: it degrades, visibly, on inputs nobody has
reported having. Ships when someone hits it or when something else opens the
same files.

### Listing the opt-in memory file in the uninstall report

`axiom-lessons.md` lives outside `data_root` by design, so uninstall does not
delete it — correct, but the report should say it exists so you can decide.
Small, uncontroversial, waiting on a reason to touch that command.

### Narrowing the durable-artifact heuristic in `schema_guard`

In enforce mode it can deny a genuinely temporary write whose name looks
persistent. Mitigated by being observe-only by default. Narrowing it needs a
corpus of real writes to calibrate against; guessing at a better heuristic
without one is how the first one got this wide.

### Dropping the legacy-claim dual read

Claims written before `claim_id` fall back to a timestamp. Harmless, small,
and there is no evidence anyone still has such a claim. Removal is a cleanup
with a nonzero chance of breaking a quiet user — the trade only makes sense
bundled with something else in that code.

### Review and Evolve stations

Both are labelled `roadmap` in the README and neither is written. They are the
two stations where the honest check is "did an independent party look at this"
and "did a human approve this rule change" — process claims, not filesystem
ones. A predicate cannot verify them, so whatever ships there will be a
different kind of thing, and calling it a hook would be the first lie.

### Wiring the provider layer into the runtime hooks

`providers/` is a reference extension interface. Nobody has asked for a
second implementation. Wiring it in ahead of that demand means maintaining an
abstraction with one implementation.

### Shallow clones make the history scan unusable, not wrong

`--scan-history` refuses in a shallow clone, which is the correct security
answer — the commits past the boundary are still on the remote and there is no
honest way to certify them — but it means any CI that clones shallow by
default cannot run the scan without being reconfigured (`fetch-depth: 0` on
GitHub Actions, which this repository already sets).

Recorded rather than fixed because the two available fixes are both worse:
degrading to a partial scan reintroduces the false green this whole guard
exists to prevent, and fetching the full history from inside the gate makes a
read-only check do network I/O. A clearer error, or an opt-in that records the
partial coverage in the output rather than hiding it, is the likely shape.

*Raised by the cross-family review of the commit-metadata scan, classified by
that reviewer as usability rather than a security defect.*

## Not a roadmap item: held-out tests

`guard_paths` catches *that* declared files moved between registration and
verification. It cannot tell an honest edit from a dishonest one, it cannot
see a test that was weak to begin with, and it cannot see an edit that was
reverted before the turn ended — it compares two points in time, not the
interval between them. Closing that last one means running the command against
an immutable copy or watching the paths for its duration; both are real
options and neither is built. The literature's stronger answer is held-out tests the agent never sees
— which a tool running on the same machine, with the same permissions, on your
side of the work, cannot manufacture. Stated here so the boundary does not get
quietly re-litigated as a feature request.
