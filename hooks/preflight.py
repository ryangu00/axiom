#!/usr/bin/env python3
"""Ask before irreversible Bash command shapes (enforce); record them (observe)."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import axiom_common as common

RULE = "preflight"
# Report order, and the order `detect_pattern` picks its one class from.
CLASSES = (
    "rm_recursive",
    "git_reset_hard",
    "git_clean_force",
    "drop_database",
    "disk_overwrite",
    "force_push",
)
# SQL arrives inside quoted strings and here-documents, which the shell shape
# treats as data, so this class is matched on the raw text.
DROP_DATABASE = re.compile(r"\bDROP\s+(?:TABLE|DATABASE)\b", re.IGNORECASE)
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
# Commands that run a string as a shell script via `-c` (or `--command`).
_SCRIPT_RUNNERS = _SHELLS | {"su", "runuser", "flock"}
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O"})
_SSH_VALUE_OPTIONS = frozenset(
    [
        "-B",
        "-b",
        "-c",
        "-D",
        "-E",
        "-e",
        "-F",
        "-I",
        "-i",
        "-J",
        "-L",
        "-l",
        "-m",
        "-O",
        "-o",
        "-P",
        "-p",
        "-Q",
        "-R",
        "-S",
        "-W",
        "-w",
    ]
)
_EXEC_VALUE_OPTIONS = frozenset(
    {"-e", "-u", "-w", "--env", "--env-file", "--user", "--workdir", "--detach-keys"}
)
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# Classes whose command word is distinctive enough to match at any word
# position: they still fire behind a wrapper this file does not know
# (`retry 3 git push -f`). `rm` is matched in command position only, or
# `git rm -rf --cached dir` would read as a recursive delete.
_ANYWHERE = frozenset({"git", "dd", "shred", "truncate"})
# `bash -c "eval 'sh -c ...'"` nests; three levels is past anything an agent
# writes and keeps a hostile string from recursing without bound.
_MAX_DEPTH = 3

# Text that does not lex (unbalanced quotes) has no known shape, which is not
# the same as a safe one: it falls back to matching the raw text.
RM_COMMAND = re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?rm\s+[^;&|]+", re.IGNORECASE)
FALLBACK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("git_reset_hard", re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE)),
    (
        "git_clean_force",
        re.compile(
            r"\bgit\s+clean\s+(?:-[A-Za-z]*f[A-Za-z]*|--force)\b", re.IGNORECASE
        ),
    ),
    (
        "disk_overwrite",
        re.compile(
            r"(?:\bmkfs(?:\.[A-Za-z0-9_-]+)?\b|\bdd\b[^\n;&|]*\bof\s*=|\bshred\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "force_push",
        re.compile(
            r"\bgit\s+push\b[^\n;&|]*(?:--force(?![\w-])|(?<![\w-])-f\b)",
            re.IGNORECASE,
        ),
    ),
]


def _is_tmp(path_value: str, environ: Mapping[str, str]) -> bool:
    try:
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            return False
        candidate = path.resolve()
    except (OSError, RuntimeError, ValueError):
        # `~nosuchuser`, a NUL byte, a symlink loop: not provably temporary.
        return False
    for temporary in common.temp_roots(environ=environ):
        try:
            candidate.relative_to(temporary)
            return True
        except ValueError:
            continue
    return False


def _split_options(arguments: Sequence[str]) -> tuple[list[str], list[str]]:
    """Options and operands; GNU tools accept options after operands, `--` ends them."""
    options: list[str] = []
    operands: list[str] = []
    ended = False
    for word in arguments:
        if ended or word == "-" or not word.startswith("-"):
            operands.append(word)
        elif word == "--":
            ended = True
        else:
            options.append(word)
    return options, operands


def _short_letters(options: Sequence[str]) -> str:
    """Letters of the short-option bundles: `-rf -v` gives "rfv".

    Long options are excluded: "--force" contains an 'r' but is not recursive.
    """
    return "".join(word[1:] for word in options if not word.startswith("--"))


def _destructive_rm(arguments: Sequence[str], environ: Mapping[str, str]) -> bool:
    options, targets = _split_options(arguments)
    letters = _short_letters(options).lower()
    recursive = "r" in letters or "--recursive" in options
    forced = "f" in letters or "--force" in options
    if not (recursive and forced):
        return False
    # No operand here means the operands arrive some other way (`xargs`,
    # `find -exec`): unknown targets are not temporary ones.
    return not targets or any(not _is_tmp(item, environ) for item in targets)


def _git_class(arguments: Sequence[str]) -> str | None:
    subcommand, rest = common.git_subcommand(arguments)
    options, operands = _split_options(rest)
    letters = _short_letters(options)
    dry_run = "--dry-run" in options
    if subcommand == "reset" and "--hard" in options:
        return "git_reset_hard"
    if (
        subcommand == "clean"
        and ("f" in letters or "--force" in options)
        and not (dry_run or "n" in letters)
    ):
        return "git_clean_force"
    # `--force-with-lease` and `--force-if-includes` refuse to overwrite work
    # the pusher has not seen; only the unconditional forms count.
    if (
        subcommand == "push"
        and not (dry_run or "n" in letters)
        and (
            "f" in letters
            or "--force" in options
            or any(item.startswith("+") for item in operands)
        )
    ):
        return "force_push"
    return None


def _truncates_to_zero(arguments: Sequence[str]) -> bool:
    for index, word in enumerate(arguments):
        if word in ("-s0", "--size=0"):
            return True
        following = arguments[index + 1] if index + 1 < len(arguments) else ""
        if word in ("-s", "--size") and following == "0":
            return True
    return False


def _disk_overwrite(name: str, arguments: Sequence[str]) -> bool:
    # mkfs and shred need an operand: `man shred` and `which mkfs` are lookups.
    operands = _split_options(arguments)[1]
    return (
        ((name.startswith("mkfs") or name == "shred") and bool(operands))
        or (name == "dd" and any(item.startswith("of=") for item in arguments))
        or (name == "truncate" and _truncates_to_zero(arguments))
    )


def _script_argument(arguments: Sequence[str]) -> str | None:
    """The string of `bash -c '...'`, `su -c '...'`, `flock FILE -c '...'`."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in _SHELL_VALUE_OPTIONS:
            index += 2
            continue
        short = word.startswith("-") and not word.startswith("--")
        if word == "--command" or (short and "c" in word[1:]):
            return arguments[index + 1] if index + 1 < len(arguments) else None
        if word.startswith("--command="):
            return word.split("=", 1)[1]
        index += 1
    return None


def _ssh_remote(arguments: Sequence[str]) -> str | None:
    """The remote command of `ssh [options] host command…`, if any."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in _SSH_VALUE_OPTIONS:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            remote = arguments[index + 1 :]
            return " ".join(remote) if remote else None
    return None


def _exec_argv(arguments: Sequence[str]) -> list[str]:
    """The command of `docker|podman exec [options] container cmd…`."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in _EXEC_VALUE_OPTIONS:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            return list(arguments[index + 1 :])
    return []


def _find_exec_commands(arguments: Sequence[str]) -> list[list[str]]:
    """The commands `find -exec` runs, each ended by a `;` or `+` word."""
    commands: list[list[str]] = []
    current: list[str] | None = None
    for word in arguments:
        if current is None:
            if word in _FIND_EXEC:
                current = []
            continue
        if word in (";", "+"):
            commands.append(current)
            current = None
            continue
        current.append(word)
    if current:
        commands.append(current)
    return commands


def _reads_script_from_stdin(name: str, arguments: Sequence[str]) -> bool:
    if name in _SHELLS:
        return _script_argument(arguments) is None
    return name == "ssh" and _ssh_remote(arguments) is None


def _command_classes(
    name: str, arguments: list[str], environ: Mapping[str, str], depth: int
) -> list[str]:
    """Classes of one resolved command, including the commands it runs."""
    found: list[str] = []
    if name == "rm" and _destructive_rm(arguments, environ):
        found.append("rm_recursive")
    elif name == "git":
        git_class = _git_class(arguments)
        if git_class is not None:
            found.append(git_class)
        subcommand, rest = common.git_subcommand(arguments)
        if depth < _MAX_DEPTH and subcommand == "submodule" and rest[:1] == ["foreach"]:
            foreach = [item for item in rest[1:] if not item.startswith("--")]
            found += _text_classes(" ".join(foreach), environ, depth + 1)
    elif _disk_overwrite(name, arguments):
        found.append("disk_overwrite")
    if depth >= _MAX_DEPTH:
        return found
    if name in _SCRIPT_RUNNERS:
        script = _script_argument(arguments)
        if script is not None:
            found += _text_classes(script, environ, depth + 1)
    elif name == "eval" or (name == "watch" and arguments):
        found += _text_classes(" ".join(arguments), environ, depth + 1)
    elif name == "ssh":
        remote = _ssh_remote(arguments)
        if remote is not None:
            found += _text_classes(remote, environ, depth + 1)
    elif name in ("docker", "podman") and arguments[:1] == ["exec"]:
        found += _argv_classes(_exec_argv(arguments[1:]), environ, depth + 1)
    elif name == "kubectl" and "exec" in arguments and "--" in arguments:
        found += _argv_classes(
            arguments[arguments.index("--") + 1 :], environ, depth + 1
        )
    elif name == "find":
        for command in _find_exec_commands(arguments):
            found += _argv_classes(command, environ, depth + 1)
    elif " " in name:
        # A quoted multi-word command (`watch 'git reset --hard'` after the
        # wrapper is skipped) is a script, not an executable name.
        found += _text_classes(" ".join([name, *arguments]), environ, depth + 1)
    return found


def _argv_classes(
    argv: Sequence[str], environ: Mapping[str, str], depth: int
) -> list[str]:
    name, arguments = common.command_name(argv)
    found = _command_classes(name, arguments, environ, depth)
    # Safety net for wrappers `command_name` does not know: the distinctive
    # classes also match where their command word appears later in the argv.
    for index in range(1, len(argv)):
        word = argv[index].replace("\\", "/").rsplit("/", 1)[-1]
        if word in _ANYWHERE or word.startswith("mkfs"):
            found += _command_classes(
                word, list(argv[index + 1 :]), environ, _MAX_DEPTH
            )
    return found


def _fallback_classes(command: str, environ: Mapping[str, str]) -> list[str]:
    found: list[str] = []
    for match in RM_COMMAND.finditer(command):
        try:
            argv = shlex.split(match.group(0).lstrip(";&| "))
        except ValueError:
            argv = match.group(0).lstrip(";&| ").split()
        name, arguments = common.command_name(argv)
        if name == "rm" and _destructive_rm(arguments, environ):
            found.append("rm_recursive")
    found += [name for name, pattern in FALLBACK_PATTERNS if pattern.search(command)]
    return found


def _text_classes(command: str, environ: Mapping[str, str], depth: int) -> list[str]:
    found: list[str] = []
    if DROP_DATABASE.search(command):
        found.append("drop_database")
    shape = common.shell_shape(command)
    if shape is None:
        return found + _fallback_classes(command, environ)
    for argv in shape.commands:
        found += _argv_classes(argv, environ, depth)
    if depth < _MAX_DEPTH:
        for script in shape.substitutions:
            found += _text_classes(script, environ, depth + 1)
        # A here-document is data, except on the stdin of a shell or of ssh
        # with no remote command: `bash <<EOF`, `cat <<EOF | sh`.
        if shape.heredocs and any(
            _reads_script_from_stdin(*common.command_name(argv))
            for argv in shape.commands
        ):
            for body in shape.heredocs:
                found += _text_classes(body, environ, depth + 1)
    return found


def detect_patterns(
    command: str, *, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Every irreversible command class in a Bash command, in `CLASSES` order."""
    environment = os.environ if environ is None else environ
    found = set(_text_classes(command, environment, 0))
    return [name for name in CLASSES if name in found]


def detect_pattern(
    command: str, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """Return the first irreversible command class found in a Bash command."""
    classes = detect_patterns(command, environ=environ)
    return classes[0] if classes else None


def process(
    payload: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Record every detected class; in enforce mode, ask the human first."""
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    classes = detect_patterns(command, environ=environ)
    if not classes:
        return None
    cwd_value = payload.get("cwd")
    cwd = (
        Path(cwd_value).resolve()
        if isinstance(cwd_value, str) and cwd_value
        else Path.cwd()
    )
    paths = common.ensure_layout(root=root, cwd=cwd)
    config = common.hook_config(paths, payload, "preflight").data

    reason = (
        f"AXIOM preflight: detected irreversible command class {', '.join(classes)}; "
        "expected explicit recovery and scope checks, actual command can destroy state. "
        "Confirm before it runs: "
        "Is a current backup or recovery point available? "
        "Is the target and scope exactly verified? "
        "Is there a reversible alternative or dry run? "
        f"Escape hatch: {common.escape_hatch(RULE)}"
    )
    records: list[dict[str, Any]] = [
        {
            "hook": "preflight",
            "rule": RULE,
            "finding_id": common.finding_id(RULE, pattern_class, command),
            "basis": pattern_class,
            "summary": reason,
            "failed": [
                {
                    "type": pattern_class,
                    "expected": "recovery and scope checks",
                    "actual": "irreversible command shape",
                }
            ],
        }
        for pattern_class in classes
    ]
    if common.rule_mode(config, RULE) == "observe":
        for record in records:
            common.append_ledger(
                paths["ledger"], {"event": "would_have_blocked", **record}
            )
        return None
    # Enforce mode puts the questions to the human before the command runs.
    # Context injected from PreToolUse reaches the agent only next to the
    # tool result, after the command has already run. There is no cooldown:
    # a cooldown would let the next irreversible command through unasked.
    for record in records:
        common.record_best_effort(paths["ledger"], {"event": "asked", **record})
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def main() -> int:
    payload: Any = {}
    try:
        payload = json.load(sys.stdin)
        payload = payload if isinstance(payload, Mapping) else {}
        response = process(payload)
        if response:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        # Fail open, but never silently (an invisible fail-open is
        # indistinguishable from a hook that was never installed).
        return common.fail_open_main("preflight", RULE, error, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
