---
description: Remove axiom-managed state with explicit confirmation
---

Remove the state files this plugin manages. Deletion is irreversible, so every step requires explicit confirmation. Do nothing destructive until the user confirms.

1. Run a **dry-run** first to see exactly what would be deleted:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py uninstall --dry-run
   ```
2. Show the user the **full list** of plugin-managed files that would be deleted. Their `*.goal.md` files live in their own project and are **never touched** by this command — say so.
3. Ask the user for **explicit confirmation** to proceed with deletion. Stop if they decline.
4. Run the deletion with the confirm flag:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py uninstall --confirm
   ```
5. After deletion, **re-run the dry-run** and show the user that the result is now empty, proving nothing managed by this plugin remains.
6. Tell the user they must run the host uninstall themselves:
   ```
   claude plugin uninstall axiom
   ```
   Note that the host keeps plugin cache copies which **Claude Code manages, not this plugin** — this command only removes the state this plugin wrote.