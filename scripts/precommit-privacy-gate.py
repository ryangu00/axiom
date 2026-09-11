#!/usr/bin/env python3
"""Reject staged additions that contain likely private identifiers."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^/\s]+(?:/[^\s]*)?")
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    # Every character below is legal in a local part, backtick included, and
    # each one stays reachable: `+tag@` and `_svc@` are ordinary addresses, and
    # so is `` `svc@ ``. The single excluded shape is a local part that is
    # *nothing but* one backtick, which is not an address anyone has -- it is
    # markdown, where `@example.org` in prose was read as an address whose
    # local part is the backtick. A leading dot is excluded because it is
    # invalid in an address to begin with.
    r"(?:[A-Za-z0-9!#$%&'*+/=?^_{|}~-][A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]*"
    r"|`[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+)@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    r"(?![A-Za-z0-9-])"
)
# RFC 2606 and RFC 6761 reserve these names so that documentation, examples and
# tests have addresses that can never route anywhere. Flagging them is a false
# positive by construction: they exist precisely to be written down. Leaving
# them in would train the reader to wave the gate through, which costs more than
# the handful of real addresses it would catch by accident.
RESERVED_EMAIL_DOMAIN_PATTERN = re.compile(
    r"(?i)@(?:[A-Za-z0-9-]+\.)*(?:example\.(?:com|net|org)|example|test|invalid|localhost)$"
)
IP_CANDIDATE_PATTERN = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
DEFAULT_HOSTNAME_PATTERN = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:local|lan|internal|corp|home)\b"
)
HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class GateError(RuntimeError):
    """Raised when the gate cannot safely inspect staged content."""


def _run_git(arguments: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise GateError("git executable not found") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "git command failed"
        raise GateError(detail) from error
    return result.stdout


def repository_root() -> Path:
    return Path(_run_git(["rev-parse", "--show-toplevel"]).strip()).resolve()


def load_configuration(root: Path) -> dict[str, Any]:
    path = root / ".privacy-gate.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"invalid privacy gate configuration: {error}") from error
    if not isinstance(value, dict):
        raise GateError("privacy gate configuration must be a JSON object")
    return value


# Commit metadata is scanned with its own rules, not `inspect_line`'s. Every
# commit carries an author address, so reusing the email pattern there would
# flag the entire history. What actually needs catching is narrower: an address
# outside the allowlist (a personal or employer mailbox that should never have
# been published) and an attribution trailer (this repository's policy is that
# AI involvement is disclosed once in the README, not per commit).
# C0 control bytes other than tab/newline/carriage return are not legitimate
# prose in a commit message, and they are not line breaks to a regex either. A
# trailer placed after one therefore sits mid-"line" and slips past patterns
# anchored with ^, while a human reader and most forge UIs still see it on its
# own line. Normalising them to newlines before matching removes that gap
# without loosening the anchors, which would start matching trailers quoted in
# running prose.
CONTROL_BYTE_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DEFAULT_COMMIT_EMAIL_ALLOWLIST = ["@users.noreply.github.com"]
DEFAULT_COMMIT_TRAILER_PATTERNS = [
    r"^\s*Co-Authored-By:",
    r"^\s*Generated with ",
]


def load_denylist(root: Path) -> list[str]:
    path = root / ".privacy-denylist"
    if not path.exists():
        return []
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    except OSError as error:
        raise GateError(f"cannot read denylist: {error}") from error


def compile_hostname_patterns(configuration: dict[str, Any]) -> list[re.Pattern[str]]:
    if not configuration.get("detect_hostname", False):
        return []
    patterns = [DEFAULT_HOSTNAME_PATTERN]
    configured = configuration.get("hostname_patterns", [])
    if not isinstance(configured, list) or not all(
        isinstance(item, str) for item in configured
    ):
        raise GateError(
            "hostname_patterns must be an array of regular-expression strings"
        )
    for item in configured:
        try:
            patterns.append(re.compile(item))
        except re.error as error:
            raise GateError(f"invalid hostname pattern: {error}") from error
    return patterns


def staged_diff(root: Path) -> str:
    return _run_git(
        [
            "-c",
            "core.quotePath=false",
            "diff",
            "--cached",
            "--no-color",
            "--unified=0",
            "--diff-filter=ACMR",
            "--",
        ],
        cwd=root,
    )


def added_lines(diff: str) -> Iterable[tuple[str, int, str]]:
    """Yield filename, new-file line number, and content for staged additions."""
    current_file: str | None = None
    new_line: int | None = None

    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git "):
            current_file = None
            new_line = None
            continue
        if raw_line.startswith("+++ "):
            name = raw_line[4:]
            current_file = None if name == "/dev/null" else name.removeprefix("b/")
            new_line = None
            continue
        hunk = HUNK_PATTERN.match(raw_line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if current_file is None or new_line is None:
            continue
        if raw_line.startswith("+"):
            yield current_file, new_line, raw_line[1:]
            new_line += 1
        elif raw_line.startswith("-") or raw_line.startswith("\\"):
            continue
        else:
            new_line += 1


def inspect_line(
    content: str,
    *,
    detect_ip: bool,
    hostname_patterns: list[re.Pattern[str]],
    denylist: list[str],
) -> list[str]:
    kinds: list[str] = []
    if USER_PATH_PATTERN.search(content):
        kinds.append("absolute-user-path")
    if any(
        not RESERVED_EMAIL_DOMAIN_PATTERN.search(address)
        for address in EMAIL_PATTERN.findall(content)
    ):
        kinds.append("email")
    if detect_ip:
        for candidate in IP_CANDIDATE_PATTERN.findall(content):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            kinds.append("ip-address")
            break
    if any(pattern.search(content) for pattern in hostname_patterns):
        kinds.append("hostname")
    if any(literal in content for literal in denylist):
        kinds.append("denylist-literal")
    return kinds


def scan(diff: str, root: Path) -> list[tuple[str, int, str]]:
    configuration = load_configuration(root)
    detect_ip = configuration.get("detect_ip", True)
    if not isinstance(detect_ip, bool):
        raise GateError("detect_ip must be true or false")
    hostname_patterns = compile_hostname_patterns(configuration)
    denylist = load_denylist(root)
    findings: list[tuple[str, int, str]] = []

    for filename, line_number, content in added_lines(diff):
        for kind in inspect_line(
            content,
            detect_ip=detect_ip,
            hostname_patterns=hostname_patterns,
            denylist=denylist,
        ):
            findings.append((filename, line_number, kind))
    return findings


def scan_tracked(root: Path) -> list[tuple[str, int, str]]:
    """Scan every tracked file's full content (release/CI gate, not just staged)."""
    configuration = load_configuration(root)
    detect_ip = configuration.get("detect_ip", True)
    if not isinstance(detect_ip, bool):
        raise GateError("detect_ip must be true or false")
    hostname_patterns = compile_hostname_patterns(configuration)
    denylist = load_denylist(root)
    tracked = _run_git(["ls-files", "-z"], cwd=root).split("\x00")
    findings: list[tuple[str, int, str]] = []
    for filename in tracked:
        if not filename:
            continue
        target = root / filename
        try:
            text = target.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: skip
        for line_number, content in enumerate(text.splitlines(), 1):
            for kind in inspect_line(
                content,
                detect_ip=detect_ip,
                hostname_patterns=hostname_patterns,
                denylist=denylist,
            ):
                findings.append((filename, line_number, kind))
    return findings


IDENTITY_FIELDS = 6


def _commit_messages(shas: list[str], root: Path) -> dict[str, str]:
    """Read commit messages through a length-delimited channel.

    A commit message can contain any byte, NUL included, so any format string
    that frames records with a sentinel can be broken from inside a message:
    the records after it misalign, the misaligned ones look malformed and get
    dropped, and the scan reports clean on a history it did not read.

    `cat-file --batch` states the byte length of every object before its
    contents, which removes the question rather than arguing about which
    sentinel is safe. Every requested SHA must come back parsed; one that does
    not is an error, because a missing message would otherwise be scanned as
    an empty string and find nothing.
    """
    if not shas:
        return {}
    try:
        completed = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=root,
            input="\n".join(shas).encode("ascii") + b"\n",
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise GateError("git executable not found") from error
    except subprocess.CalledProcessError as error:
        raise GateError(error.stderr.decode("utf-8", "replace").strip()) from error

    data = completed.stdout
    messages: dict[str, str] = {}
    position = 0
    while position < len(data):
        newline = data.find(b"\n", position)
        if newline == -1:
            break
        header = data[position:newline].decode("utf-8", "replace").split(" ")
        if len(header) != 3:
            raise GateError(f"unreadable object header: {' '.join(header)[:60]!r}")
        sha, _, size_text = header
        try:
            size = int(size_text)
        except ValueError as error:
            raise GateError(f"object {sha[:9]} has no readable size") from error
        body = data[newline + 1 : newline + 1 + size]
        position = newline + 1 + size + 1
        # A commit object is headers, a blank line, then the message.
        _, separator, message = body.partition(b"\n\n")
        messages[sha] = (message if separator else body).decode("utf-8", "replace")

    missing = [sha for sha in shas if sha not in messages]
    if missing:
        raise GateError(
            f"{len(missing)} commit message(s) could not be read (first: "
            f"{missing[0][:9]}); refusing to report a partial scan as clean"
        )
    return messages


def _address_is_allowed(address: str, allowlist: list[str]) -> bool:
    """Match an address against the allowlist on a domain boundary, not a substring.

    `allowed in address` accepts `someone@users.noreply.github.com.evil.example`
    against an allowlist entry of `@users.noreply.github.com`: the permitted
    text occurs inside an address that delivers somewhere else entirely. The
    boundary that matters is the domain, so the domain is taken apart and
    compared as one.
    """
    address = address.strip()
    # Exactly one `@`, both halves present. `rpartition` alone would read
    # `victim@evil.example@allowed.example` as delivering to the allowed
    # domain; it does not, and a malformed address is not a reason to skip the
    # check. Control characters disqualify it too -- they are how a field gets
    # smuggled somewhere it will not be looked at.
    if address.count("@") != 1 or CONTROL_BYTE_PATTERN.search(address):
        return False
    local, _, domain = address.partition("@")
    if not local or not domain:
        return False
    domain = _normalised_domain(domain)
    if not domain:
        return False
    for allowed in allowlist:
        candidate = _normalised_domain(allowed.strip().lstrip("@"))
        if not candidate:
            continue
        if domain == candidate or domain.endswith("." + candidate):
            return True
    return False


def _normalised_domain(value: str) -> str:
    """Lowercase, drop a trailing root dot, and encode to ASCII where possible.

    The allowlist goes through the same function as the address so that a
    Unicode entry and a Unicode address agree; normalising only one side makes
    a configured domain silently stop matching.
    """
    domain = value.strip().rstrip(".").lower()
    if not domain:
        return ""
    labels = domain.split(".")
    if any(not label or len(label) > 63 for label in labels) or len(domain) > 253:
        return ""
    try:
        return domain.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        # A domain that will not encode is not a domain. Returning it as
        # written lets the same malformed string match itself on both sides of
        # the comparison, which is an allowlist entry nobody wrote.
        return ""


def _string_list(
    configuration: dict[str, Any], key: str, default: list[str]
) -> list[str]:
    configured = configuration.get(key, default)
    if not isinstance(configured, list) or not all(
        isinstance(item, str) for item in configured
    ):
        raise GateError(f"{key} must be an array of strings")
    return configured


def scan_history(root: Path) -> list[tuple[str, int, str]]:
    """Scan commit metadata across all reachable history.

    File content and commit metadata are different attack surfaces: a green
    content scan says nothing about who the history says wrote it. This repo
    learned that the hard way — an AI attribution trailer and an employer
    address rode in through commit metadata and were caught by hand, after
    publication, because `--scan-all` reads tracked files and nothing else.

    Unreachable objects are out of scope: after a rewrite the old commits stay
    fetchable by SHA until the host garbage-collects, and no local gate can
    change that. See docs/KNOWN-LIMITATIONS.md.
    """
    configuration = load_configuration(root)
    denylist = load_denylist(root)
    allowlist = _string_list(
        configuration, "commit_email_allowlist", DEFAULT_COMMIT_EMAIL_ALLOWLIST
    )
    raw_trailers = _string_list(
        configuration, "commit_trailer_patterns", DEFAULT_COMMIT_TRAILER_PATTERNS
    )
    trailers = []
    for item in raw_trailers:
        try:
            # Git trailer keys match case-insensitively in practice, so a
            # case-sensitive pattern lets `co-authored-by:` through while every
            # human reader and forge UI still treats it as attribution.
            trailers.append(re.compile(item, re.MULTILINE | re.IGNORECASE))
        except re.error as error:
            raise GateError(f"invalid commit trailer pattern: {error}") from error

    # Identity first. Git will not let an ident line hold a NUL or a newline --
    # it is one line in the commit object, by format -- so these fields can be
    # read as a flat NUL-separated stream chunked into fixed groups, and the
    # whole value of each reaches the checks intact.
    #
    # --source records which ref reached each commit: a bare SHA leaves the
    # reader unable to tell a published commit from one only a stale local
    # remote-tracking ref still holds, which is the first thing they need.
    identity_stream = _run_git(
        [
            "log",
            "--all",
            "--source",
            "-z",
            "--format=%H%x00%S%x00%an%x00%ae%x00%cn%x00%ce",
        ],
        cwd=root,
    )
    fields = identity_stream.split("\x00")
    while fields and fields[-1].strip("\n") == "":
        fields.pop()
    if not fields:
        return []  # a repository with no commits has no history to report on
    if len(fields) % IDENTITY_FIELDS != 0:
        # Fail closed. A stream this scan cannot chunk is a history it did not
        # check, and reporting nothing for it is indistinguishable from
        # reporting it clean.
        raise GateError(
            f"commit identity stream is not a multiple of {IDENTITY_FIELDS} fields "
            f"({len(fields)} read); refusing to report a partial scan as clean"
        )

    commits = []
    for index in range(0, len(fields), IDENTITY_FIELDS):
        values = [
            value.strip("\n") for value in fields[index : index + IDENTITY_FIELDS]
        ]
        if not re.fullmatch(r"[0-9a-f]{40}", values[0]):
            raise GateError(
                f"commit identity stream is misaligned at {values[0][:40]!r}; "
                "refusing to report a partial scan as clean"
            )
        commits.append(values)

    messages = _commit_messages([values[0] for values in commits], root)
    findings: list[tuple[str, int, str]] = []
    for (
        sha,
        source,
        author_name,
        author_email,
        committer_name,
        committer_email,
    ) in commits:
        where = f"commit {sha[:9]} (via {source})"
        message = CONTROL_BYTE_PATTERN.sub("\n", messages[sha])

        for address in dict.fromkeys((author_email, committer_email)):
            if not address or RESERVED_EMAIL_DOMAIN_PATTERN.search(address):
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"commit-email ({address})"))
        # Defence in depth: an address can also sit in the message body.
        for address in dict.fromkeys(EMAIL_PATTERN.findall(message)):
            if address in (author_email, committer_email):
                continue
            if RESERVED_EMAIL_DOMAIN_PATTERN.search(address):
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"commit-email in message ({address})"))

        for pattern in trailers:
            match = pattern.search(message)
            if match:
                end_of_line = message.find("\n", match.start())
                line = message[
                    match.start() : end_of_line if end_of_line != -1 else None
                ]
                findings.append((where, 0, f"commit-trailer ({line.strip()[:60]})"))
        for literal in denylist:
            if any(
                literal in field
                for field in (
                    author_name,
                    author_email,
                    committer_name,
                    committer_email,
                    message,
                )
            ):
                findings.append((where, 0, "denylist-literal"))
    return findings


def log_override(root: Path, diff: str, findings: list[tuple[str, int, str]]) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "staged_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "findings": [
            {"file": filename, "line": line_number, "kind": kind}
            for filename, line_number, kind in findings
        ],
    }
    with (root / ".privacy-gate-log").open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, separators=(",", ":")) + "\n")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-once",
        action="store_true",
        help="allow this invocation and record a local audit entry",
    )
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help=(
            "scan every tracked file's content AND all reachable commit metadata "
            "(release/CI gate), not just staged additions"
        ),
    )
    parser.add_argument(
        "--scan-history",
        action="store_true",
        help="scan reachable commit metadata only (author/committer address, trailers)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    allow_once = (
        arguments.allow_once or os.environ.get("AXIOM_PRIVACY_GATE_ALLOW_ONCE") == "1"
    )
    try:
        root = repository_root()
        if arguments.scan_history:
            diff = ""
            findings = scan_history(root)
        elif arguments.scan_all:
            diff = ""
            findings = scan_tracked(root) + scan_history(root)
        else:
            diff = staged_diff(root)
            findings = scan(diff, root)
    except GateError as error:
        print(f"Privacy gate error: {error}", file=sys.stderr)
        return 2

    if not findings:
        if arguments.scan_history:
            scope = "commit metadata"
        elif arguments.scan_all:
            scope = "tracked files and commit metadata"
        else:
            scope = "staged additions"
        print(f"Privacy gate passed: no sensitive patterns found in {scope}.")
        return 0

    if allow_once:
        try:
            log_override(root, diff, findings)
        except OSError as error:
            print(
                f"Privacy gate error: cannot record override: {error}", file=sys.stderr
            )
            return 2
        print(
            f"Privacy gate allow-once: recorded {len(findings)} finding(s); commit allowed."
        )
        return 0

    print("Privacy gate blocked: sensitive patterns detected.")
    for filename, line_number, kind in findings:
        print(f"  {filename}:{line_number}: {kind}")
    print("Commit rejected. Remove the findings or use an audited one-time override.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
