# Design note: a fail-closed egress gate for confidential content (v2 preview)

<!-- Status: design document only. A working implementation runs in the
     author's environment but is coupled to a private knowledge base and is
     NOT shipped in v1. This note documents the architecture because several
     of its decisions were hard-won and generalize. A provider-based,
     shippable implementation is on the v2 roadmap. -->

## Problem

Once your agent can *read* from a knowledge base, a new exfiltration path
opens: confidential content retrieved locally can be handed to a cloud model
(a cheaper-tier dispatch, an external agent CLI, a `curl` to some API) with no
mechanical stop between "retrieved" and "sent." Routing discipline alone does
not close this — discipline is what fails at 2 a.m. in an unattended loop.

## The gate

A `PreToolUse` hook on the dispatch/network surface (Bash, Agent prompts,
writes to auto-pushed directories). Before content can leave, two independent
predicates run:

1. **Content match** — regexes that recognize your confidential shapes.
2. **Label reference** — the retrieved item is tagged confidential and its id
   is being quoted into an outbound payload.

Either one fires → the action is blocked with a one-line remediation.

## Four decisions that generalize

- **Criteria are single-sourced, never copied into the hook.** The gate loads
  its patterns at runtime from the one place they already live. A second copy
  of a confidential-pattern list is itself a second confidential artifact and
  a drift source. The hook file ships with **zero** literal patterns in it.
- **A canary self-test on every run.** Before judging real content, the gate
  runs a harmless constructed string that *must* match. If it doesn't, the
  criteria failed to load — and the gate treats that as failure, not as "all
  clear."
- **Fail-closed on the decision path, fail-open only off it.** If pattern
  loading, regex compilation, or the label query throws, the gate **denies**
  (with a fix hint). Only non-decision work — writing the audit log — is
  allowed to fail silently. (This is the opposite of the completion-verifier's
  posture, where a false block is worse than a missed check. Match the failure
  mode to the stakes.)
- **Override is a single-use, logged token, not an env var.** An env variable
  set inside the command text is invisible to the hook process anyway (it
  inherits the host's environment, not the command's). So an override is a
  one-shot file token the hook consumes and records — auditable, not ambient.

## Why it's staged, not shipped

The pattern source and the label store are, in the reference deployment, a
private knowledge base. A shippable version abstracts both behind the same
provider interface v1 already uses for memory: a `CriteriaProvider` (you
supply your own pattern file) and an optional `LabelProvider`. Until that
interface is proven against a second real deployment, shipping the coupled
version would violate this project's own honesty rule — so it waits.
