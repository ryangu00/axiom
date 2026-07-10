# Contributing to Axiom

Axiom's whole premise is that unverified claims are worthless. Contributions
are held to the same bar.

## Ground rules

1. **Every change ships with a runnable check.** New behavior means a new or
   updated test in `scripts/selftest.py` (hooks) or `scripts/selftest_providers.py`
   (providers). "It works on my machine" is a claim, not evidence.
2. **Both suites must be green** before you open a PR:
   ```
   python3 scripts/selftest.py
   python3 scripts/selftest_providers.py
   ```
   CI runs them on Python 3.10-3.12 across Linux and macOS.
3. **No personal data, ever.** The pre-commit privacy gate
   (`scripts/precommit-privacy-gate.py`) blocks absolute user paths, emails,
   IPs, and configured hostnames in staged additions. Install it:
   ```
   ln -sf ../../scripts/precommit-privacy-gate.py .git/hooks/pre-commit
   ```
   Do not disable it. The release scan (`--scan-all`) checks the whole tree.

## What good looks like

- Hooks fail **open** on their own errors (a broken guard must never wedge a
  user's session) — except the egress gate, which fails closed. Match the
  failure mode to the stakes and say which you chose.
- Prefer narrow, declared behavior over clever inference. A predicate that
  checks exactly what you named beats one that guesses.
- Claims in docs carry a limiting qualifier or a citation. See
  `docs/KNOWN-LIMITATIONS.md` for the tone.

## Scope

v1 is deliberately small (verification hooks + templates). Routing (L1) and
self-evolution (L2) are staged — see the README capability tiers before
proposing work there.
