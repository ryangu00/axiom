---
description: Show axiom findings, coverage, and per-hook health
---

Run the axiom report and present the results to the user.

1. Run the CLI:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py report
   ```
2. Present the output grouped by rule. For every rule that appears, show:
   - the **would-have-blocked** count for that rule, and
   - the **last 3 incidents**, each on one line as: `<label> | <failed predicate> | <time>`.
3. Present the **coverage** section:
   - heartbeat days,
   - events total, and
   - per-hook last-active time.
4. If the report shows **zero events AND zero heartbeats**, warn the user that the hooks may not be loaded — the report layer has nothing to summarize.
5. At the end, for any rule with **3 or more findings**, suggest running `/axiom:enforce` to switch that rule from observe to enforce.

Do not edit any state from this command. It is read-only.