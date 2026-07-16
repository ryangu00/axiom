#!/usr/bin/env bash
# Axiom 30-second demo: watch a false "done" get caught.
#
# Runs entirely in a throwaway directory with its own state root — it touches
# nothing you own and needs no Claude Code session. What it shows is the real
# Stop-hook path: the same hook, the same evaluator, the same decision JSON
# Claude Code acts on.
#
#   ./scripts/demo.sh          # narrated
#   ./scripts/demo.sh --quiet  # just the decisions (for asciinema/CI)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/axiom-demo.XXXXXX")"
STATE="$WORK/.axiom-state"
PROJECT="$WORK/project"
mkdir -p "$PROJECT"
trap 'rm -rf "$WORK"' EXIT

QUIET=false
[[ "${1:-}" == "--quiet" ]] && QUIET=true
say() { [[ "$QUIET" == true ]] || printf '%s\n' "$@"; }
step() { [[ "$QUIET" == true ]] || printf '\n\033[1m%s\033[0m\n' "$1"; }

hook() { # hook <script> <payload-json>
  ( cd "$PROJECT" && printf '%s' "$2" |
    python3 "$REPO/hooks/$1" --data-root "$STATE" )
}
STOP='{"hook_event_name":"Stop","cwd":"'"$PROJECT"'","stop_hook_active":false}'

step "1. You declare the evidence BEFORE the work — a goal file in the project."
cat > "$PROJECT/fix-auth.goal.md" <<EOF
# fix the auth bug

## acceptance
\`\`\`json
[{"type": "file_exists", "path": "src/auth.py"},
 {"type": "cmd_succeeds", "cmd": ["python3", "-m", "unittest", "discover", "-s", "tests"]}]
\`\`\`
EOF
say "$(sed 's/^/    /' "$PROJECT/fix-auth.goal.md")"

step "2. SessionStart registers the claim (baseline snapshotted now, not later)."
hook axiom_common.py '{"hook_event_name":"SessionStart","cwd":"'"$PROJECT"'"}' >/dev/null
say "    claim registered."

# Enforce mode so the demo shows the block. On a real install every rule
# starts in observe mode and records instead of blocking. CLAUDE_PLUGIN_DATA
# keeps this in the throwaway state root — never the reader's own data root.
CLAUDE_PLUGIN_DATA="$STATE" python3 "$REPO/scripts/axiom_cli.py" \
  enforce write-verify on --cwd "$PROJECT" >/dev/null

step "3. The agent does some work and says: \"Done — auth is fixed, tests pass.\""
mkdir -p "$PROJECT/src" "$PROJECT/tests"
cat > "$PROJECT/src/auth.py" <<'EOF'
def authenticate(token):
    return False  # not actually fixed
EOF
cat > "$PROJECT/tests/test_auth.py" <<'EOF'
import unittest, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.auth import authenticate


class T(unittest.TestCase):
    def test_valid_token_authenticates(self):
        self.assertTrue(authenticate("valid-token"))
EOF
say "    src/auth.py written, tests/test_auth.py written."

step "4. The turn tries to end. Axiom re-runs the declared evidence itself:"
DECISION="$(CLAUDE_PLUGIN_DATA="$STATE" hook write_verify.py "$STOP" || true)"
printf '%s\n' "$DECISION" | python3 -c 'import json,sys
raw = sys.stdin.read().strip()
if not raw:
    print("    (no decision — the turn was allowed to end)"); sys.exit()
d = json.loads(raw)
print("    decision:", d.get("decision"))
print("    reason:  ", d.get("reason"))'
say ""
say "    The turn does not end. The agent gets the failure and keeps working."
say "    Note: the file EXISTS and the agent SAID tests pass — Axiom ran them."

step "5. The agent actually fixes it. Same claim, same evidence, re-run:"
cat > "$PROJECT/src/auth.py" <<'EOF'
def authenticate(token):
    return token == "valid-token"
EOF
DECISION="$(CLAUDE_PLUGIN_DATA="$STATE" hook write_verify.py "$STOP" || true)"
if [[ -z "$DECISION" ]]; then
  say "    no decision — the claim passed, the turn ends, the claim is cleared."
else
  say "    still blocked: $DECISION"
fi

step "What you just saw"
say "    - Evidence declared before the work, snapshotted at registration."
say "    - The check ran fresh against the filesystem, not against the chat."
say "    - A false \"done\" was caught by running the test the agent claimed passed."
say "    - Real installs start in observe mode: this would be recorded, not blocked."
