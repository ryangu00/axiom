---
description: Remove axiom-managed state with explicit confirmation
---

Remove the state files this plugin manages. Deletion is irreversible, so every step requires explicit confirmation. Do nothing destructive until the user confirms.

1. Run a **dry-run** first to see exactly what would be deleted:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py uninstall --dry-run
   ```
2. Show the user the **full list** of plugin-managed files that would be deleted.
3. Ask the user for **explicit confirmation** to proceed with deletion. Stop if they decline.
4. Ask **separately** whether to **keep goal files**. Treat that as its own decision, independent of the deletion confirmation.
5. Run the deletion with the confirm flag, adding the keep-goals flag only if the user asked to keep goals:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py uninstall --confirm [--keep-goals]
   ```
6. After deletion, **re-run the dry-run** and show the user that the result is now empty, proving nothing managed by this plugin remains.
7. Tell the user they must run the host uninstall themselves:
   ```
   claude plugin uninstall axiom
   ```
   Note that the host keeps plugin cache copies which **Claude Code manages, not this plugin** — this command only removes the state this plugin wrote.