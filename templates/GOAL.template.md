# GOAL: <short-task-name> (forged <date>)

---
status: pending   # pending | in_progress | done | blocked — update in the same edit as any task change
---

```yaml
goal: <one sentence: what done looks like, not what you will do>
context: >
  <inputs, constraints, links. Keep it short — this file is the north star a
  fresh session reads after context loss; it must stand alone.>
risk: <1-4>        # 3+ means: independent review before execution, human approval before irreversible steps
rollback: <how to undo if this goes wrong; "none possible" is an answer that forces gates>
done_criteria:
  - <executable check, not a vibe — a command, a diff target, a query>
  - <every criterion answers: "what exactly do I run/inspect to call this done?">
tasks:
  - id: t1
    what: <concrete deliverable>
    route: <who executes: self | subagent | external-executor-name>
    accept: <executable acceptance for THIS task>
    status: pending
  - id: t2
    what: ...
    route: ...
    accept: ...
    status: pending
changelog:
  - {ts: <iso>, change: forged, why: <one line>}
  # Every re-plan gets a line HERE in the same edit. A plan change without a
  # changelog line is drift, not adaptation. The file is a continuous state
  # anchor: if the session dies mid-task, the next one resumes from here.
```

## acceptance
<!-- Optional structured section. If present, axiom's write-verify hook
     auto-registers these as evidence predicates at SessionStart: the Stop
     hook will then verify them against the environment (files, git,
     fresh command runs) before accepting "done". Only claim what a
     machine can check. -->
```json
{"predicates": [
  {"type": "file_exists",   "path": "<expected artifact>"},
  {"type": "file_contains", "path": "<file>", "pattern": "<regex>"},
  {"type": "file_changed",  "path": "<file that must differ from baseline>"},
  {"type": "cmd_succeeds",  "cmd": "<test/build command>", "timeout": 120}
], "label": "<short claim label>"}
```

<!--
Discipline notes (delete in real files):
- Forge BEFORE starting work; the act of writing done_criteria is the design review.
- Ask per candidate task: "if I delete this, does a done_criterion fail?" No = it does not enter.
- Three hard stops that escalate to a human instead of continuing: an
  irreversible action, N failed fix attempts on the same target, a collapsed premise.
- On closeout: diff every done_criterion against evidence. Extras you built
  that no criterion asked for get named in the changelog with who asked for them.
-->
