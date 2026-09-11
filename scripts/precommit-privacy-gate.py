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
import tempfile
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
# Tried against the raw identity bytes when looking for a denylist literal.
# A list, not a guarantee: see _identity_is_canonically_readable for what
# happens when a name is written in something outside it.
CANDIDATE_IDENTITY_ENCODINGS = ("utf-8", "latin-1", "cp1252", "utf-16-le", "utf-16-be")
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
# A ceiling on how much git OUTPUT is read, which is not the same as a ceiling
# on memory: the chunks, the joined copy and the decoded text can all exist at
# once, so peak usage is a multiple of this. It is not a memory limit and is
# not named as one. What it buys is a refusal this gate can still make, rather
# than being killed part-way through and leaving the caller to guess what that
# meant. Incremental scanning is the real fix and is on the roadmap with the
# other unbounded reads.
MAX_GIT_OUTPUT_BYTES = 512 * 1024 * 1024


def _bounded_git_output(
    arguments: list[str], *, cwd: Path, budget: int, stdin_path: Path | None = None
) -> bytes:
    """Run git and read stdout incrementally, refusing once it exceeds a budget.

    Checking the size of an output that has already been captured in full is
    not a limit -- the memory was spent before the comparison ran, so the
    process can be killed before it ever reaches the polite refusal. Reading in
    chunks and stopping at the budget is what makes the refusal real.

    Input comes from a file rather than a pipe so that writing the request and
    reading the response cannot deadlock against each other on a large batch.
    """
    # stderr goes to a file, not a pipe. Only stdout is drained here, so a
    # command that fills the stderr pipe would block writing to it while this
    # loop waits for stdout that is never coming -- a deadlock no budget or
    # timeout in this function can break.
    with tempfile.TemporaryDirectory() as workspace:
        error_path = Path(workspace) / "stderr"
        handle = stdin_path.open("rb") if stdin_path is not None else subprocess.DEVNULL
        try:
            with error_path.open("wb") as error_file:
                process = subprocess.Popen(  # noqa: S603
                    ["git", "--no-replace-objects", *arguments],
                    cwd=cwd,
                    stdin=handle,
                    stdout=subprocess.PIPE,
                    stderr=error_file,
                )
        except FileNotFoundError as error:
            raise GateError("git executable not found") from error
        finally:
            if stdin_path is not None:
                handle.close()

        chunks: list[bytes] = []
        total = 0
        over_budget = False
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > budget:
                    over_budget = True
                    process.kill()
                    break
                chunks.append(chunk)
        finally:
            process.stdout.close()
            process.wait()

        if over_budget:
            raise GateError(
                f"git output exceeded {budget} bytes; refusing to scan a history "
                "this gate cannot hold, rather than being killed part-way through "
                "and leaving the result ambiguous"
            )
        if process.returncode != 0:
            # Only zero. Treating the kill signal as success would also accept a
            # process the OOM killer or anything else ended after it had written
            # well-formed output, which is precisely a partial scan that parses.
            detail = error_path.read_bytes().decode("utf-8", "replace").strip()
            raise GateError(detail or f"git command failed ({process.returncode})")
    return b"".join(chunks)


def _commit_messages(shas: list[str], root: Path) -> dict[str, tuple[str, bytes]]:
    """Read commit messages through a length-delimited channel, or fail.

    A commit message can contain any byte, NUL included -- porcelain refuses
    it, but `hash-object -t commit --literally` writes the object and a ref can
    point at it. So any format string that frames records with a sentinel can
    be broken from inside a message: the records after it misalign, the
    misaligned ones look malformed and get dropped, and the scan reports clean
    on a history it never read.

    `cat-file --batch` states each object's byte length before its contents,
    which ends the argument about which sentinel is safe. Everything this
    function refuses is a variation on the same theme: a partial read must
    never be reported as a clean one.
    """
    if not shas:
        return {}
    # A plain file inside a temporary directory, fully written and closed before
    # anything reopens it. A NamedTemporaryFile held open while a second handle
    # opens the same path is a PermissionError on Windows, which this project
    # supports and tests.
    with tempfile.TemporaryDirectory() as workspace:
        request_path = Path(workspace) / "request"
        request_path.write_bytes("\n".join(shas).encode("ascii") + b"\n")
        data = _bounded_git_output(
            ["cat-file", "--batch"],
            cwd=root,
            budget=MAX_GIT_OUTPUT_BYTES,
            stdin_path=request_path,
        )

    messages: dict[str, tuple[str, bytes]] = {}
    position = 0
    # Responses come back in request order, so each one is checked against the
    # SHA that was asked for. Confirming only that every SHA appears somewhere
    # in the result would accept a reply that answered a different question.
    for expected in shas:
        newline = data.find(b"\n", position)
        if newline == -1:
            raise GateError(
                f"object stream ended before {expected[:9]}; refusing to report "
                "a partial scan as clean"
            )
        header = data[position:newline].decode("utf-8", "replace").split(" ")
        if len(header) != 3:
            raise GateError(
                f"unreadable object header for {expected[:9]}: "
                f"{' '.join(header)[:60]!r}"
            )
        sha, kind, size_text = header
        if sha != expected:
            raise GateError(f"expected object {expected[:9]}, got {sha[:40]!r}")
        if kind != "commit":
            raise GateError(f"object {sha[:9]} is a {kind}, not a commit")
        try:
            size = int(size_text)
        except ValueError as error:
            raise GateError(f"object {sha[:9]} has no readable size") from error

        start = newline + 1
        body = data[start : start + size]
        if len(body) != size:
            raise GateError(
                f"object {sha[:9]} is truncated ({len(body)} of {size} bytes); "
                "refusing to report a partial scan as clean"
            )
        if data[start + size : start + size + 1] != b"\n":
            raise GateError(
                f"object {sha[:9]} is not terminated where its length says it "
                "ends; refusing to report a partial scan as clean"
            )
        position = start + size + 1
        messages[sha] = (_decode_commit_message(body, sha), body)

    if position != len(data):
        raise GateError("trailing data after the last object; refusing to scan")
    return messages


# An ident line is `<kind> Name <address> <timestamp> <zone>`. Embedded objects
# carry their own -- a mergetag holds the tagger who signed the tag that got
# merged -- and those addresses are published with the commit exactly like its
# own, so they get the same structured check rather than a text pattern.
EMBEDDED_IDENT_LINE = re.compile(r"^(?:tagger|author|committer)\s")


# Stricter than CONTROL_BYTE_PATTERN, which spares tab and carriage return
# because those are ordinary in prose. A header is not prose: it is a rigid
# `keyword value` line, and a tab or a carriage return inside one is no more
# something git writes than a unit separator is. Sparing them left
# `tag<TAB>ger` recognised as nothing at all -- the same bypass one byte over.
# Newline is absent because it is the line separator: it cannot occur within a
# line to begin with.
HEADER_CONTROL_BYTES = re.compile(rb"[\x00-\x09\x0b-\x1f\x7f]")


def _unreadable_header_lines(body: bytes) -> list[str]:
    """Header lines carrying bytes git does not write, and so cannot be read.

    A control byte inside a header is not something git produces -- only
    hand-written objects have them -- and it defeats recognition rather than
    detection: a keyword spelled `tag<US>ger` is not matched as an ident line,
    so the line is skipped and its address never checked. Recognising it
    reliably is not possible, so it refuses instead.

    The commit's own author and committer lines are exempt: those fields come
    from git's own extraction in the identity stream, which reads them
    structurally no matter what they contain.
    """
    headers, _, _ = body.partition(b"\n\n")
    unreadable: list[str] = []
    for line in headers.split(b"\n"):
        if line.startswith((b"tree ", b"parent ", b"author ", b"committer ")):
            continue
        if HEADER_CONTROL_BYTES.search(line):
            unreadable.append(
                line.decode("utf-8", "replace")[:80].replace("\ufffd", "?")
            )
    return unreadable


def _embedded_idents(header_text: str) -> tuple[list[str], list[str]]:
    """Pull addresses out of ident lines in embedded objects, or say you cannot.

    Returns the addresses found and the ident-looking lines that could not be
    read. The second list is the point: a line that announces itself as an
    ident and then will not parse must not be skipped, because skipping is
    indistinguishable from finding nothing in it.

    Every bracketed token on the line is returned, not just one. A line like
    `tagger Name <decoy> <real@host> ...` puts the real address in the last
    pair, and a matcher that stops at the first one reports on the decoy --
    which is the same failure as handing the line to a text pattern, one step
    further in.
    """
    addresses: list[str] = []
    unparseable: list[str] = []
    for raw_line in header_text.split("\n"):
        line = raw_line.strip()
        if not EMBEDDED_IDENT_LINE.match(line):
            continue
        found: list[str] = []
        position = 0
        while True:
            opened = line.find("<", position)
            if opened == -1:
                break
            closed = line.find(">", opened + 1)
            if closed == -1:
                unparseable.append(line[:80])
                found = []
                break
            found.append(line[opened + 1 : closed])
            position = closed + 1
        if found:
            addresses.extend(found)
        elif line not in unparseable and "<" not in line:
            # An ident line with no bracketed address at all: cannot be read,
            # and saying nothing about it would read as saying it is clean.
            unparseable.append(line[:80])
    return addresses, unparseable


def _scannable_headers(body: bytes) -> str:
    """Header lines worth scanning, which is all of them but tree and parent.

    A commit object carries more than an ident line and a message. `mergetag`
    embeds an entire tag object -- its tagger identity and its own message --
    and other headers can carry text too. Scanning the message and the idents
    and stopping there leaves that content unread, which is the same shape of
    false green as every other gap in this sequence. `tree` and `parent` are
    object names and hold nothing but hex.
    """
    headers, _, _ = body.partition(b"\n\n")
    keep = [
        line
        for line in headers.split(b"\n")
        if not line.startswith(b"tree ") and not line.startswith(b"parent ")
    ]
    return b"\n".join(keep).decode("utf-8", "surrogateescape")


def _decode_commit_message(body: bytes, sha: str) -> str:
    """Split a commit object and decode its message in the encoding it declares.

    Commit objects may carry an `encoding` header, and decoding a message that
    is not UTF-8 with replacement characters silently rewrites it: a denylist
    literal with a non-ASCII character in it stops matching, which is the one
    outcome a denylist must not have. Decode as declared, or refuse.
    """
    headers, separator, message = body.partition(b"\n\n")
    if not separator:
        # No blank line: treat the whole object as text rather than assume
        # there is no message. Over-reporting is the safe direction here.
        headers, message = b"", body
    encoding = "utf-8"
    declared = [
        line[len(b"encoding ") :].decode("ascii", "replace").strip()
        for line in headers.split(b"\n")
        if line.startswith(b"encoding ")
    ]
    if len(declared) > 1:
        # Which one a reader honours is not something this gate should guess
        # at: if git and this scan resolve the ambiguity differently, the
        # message a person sees is not the message that was scanned.
        raise GateError(
            f"commit {sha[:9]} declares {len(declared)} encodings; refusing to "
            "choose between them"
        )
    if declared:
        if not declared[0]:
            raise GateError(f"commit {sha[:9]} declares an empty encoding")
        encoding = declared[0]
    try:
        return message.decode(encoding)
    except (LookupError, UnicodeDecodeError) as error:
        raise GateError(
            f"commit {sha[:9]} declares encoding {encoding!r} and its message "
            f"does not decode in it ({error}); refusing to scan a message this "
            "gate would have to guess at"
        ) from error


def _literal_in_raw_fields(literal: str, raw_fields: list[bytes]) -> bool:
    """Look for a denylist literal in identity bytes, not only in one decoding.

    An author name is bytes with no declared encoding. Comparing a UTF-8
    literal against a UTF-8 reading of those bytes finds nothing when the name
    was written in something else -- the literal is still there, spelled
    differently, and the scan says clean about metadata that will be published.
    So the literal is encoded into the candidates and matched against the raw
    bytes, in addition to the lossless text form.
    """
    if not raw_fields:
        return False
    joined = b"\x00".join(raw_fields)
    for encoding in CANDIDATE_IDENTITY_ENCODINGS:
        try:
            if literal.encode(encoding) in joined:
                return True
        except (UnicodeEncodeError, LookupError):
            continue
    text = joined.decode("utf-8", "surrogateescape")
    return literal in text


def _identity_is_canonically_readable(raw_fields: list[bytes]) -> bool:
    """Whether these metadata bytes can be compared against a literal at all.

    A commit's ident line has no declared encoding, and a literal comparison
    across a fixed list of candidate encodings is exactly that: a list. A name
    written in something outside it -- CP1252, Shift-JIS, GBK -- can contain a
    denylisted string whose bytes match none of the candidates, and the scan
    would then report clean about metadata that will be published as written.

    So the honest rule is narrow: if the bytes are not valid UTF-8 and nothing
    matched, this function says the comparison was not conclusive, and the
    caller refuses rather than certifying. Repositories with legacy non-UTF-8
    names and no denylist configured are unaffected, because with no literal to
    compare there is nothing that could have been missed.
    """
    for field in raw_fields:
        try:
            field.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


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
    # A shallow clone hides history without saying so. `git log --all` stops at
    # the shallow boundary and succeeds, so the scan would certify a branch
    # whose older commits it never saw -- and those commits are still on the
    # remote, waiting for the next push to expose them. The same goes for a
    # legacy grafts file, which rewrites parentage for local reads only;
    # --no-replace-objects does not cover it.
    #
    # Both refuse rather than report. This scan's whole claim is about the
    # history that will actually be published, and there is no honest way to
    # make that claim about commits it cannot reach.
    if _run_git(["rev-parse", "--is-shallow-repository"], cwd=root).strip() == "true":
        raise GateError(
            "this is a shallow clone, so history older than the shallow boundary "
            "cannot be read -- and it is still on the remote. Run "
            "`git fetch --unshallow` before trusting a history scan"
        )
    # --git-path, not --git-dir joined by hand: in a linked worktree --git-dir
    # is that worktree's private directory, while grafts live in the common
    # directory shared with the main checkout. Resolving it by hand would look
    # in the wrong place and find nothing, which reads as clean.
    grafts = Path(
        _run_git(["rev-parse", "--git-path", "info/grafts"], cwd=root).strip()
    )
    if not grafts.is_absolute():
        grafts = root / grafts
    if grafts.exists():
        raise GateError(
            "this repository has a grafts file, which rewrites parentage for "
            "local reads only; the published history differs from what would be "
            "scanned. Remove it before trusting a history scan"
        )

    identity_stream = _bounded_git_output(
        [
            "log",
            "--all",
            "--source",
            "-z",
            "--format=%H%x00%S%x00%an%x00%ae%x00%cn%x00%ce",
        ],
        cwd=root,
        budget=MAX_GIT_OUTPUT_BYTES,
    )
    # Identity fields are raw bytes and are NOT governed by the message's
    # encoding header, so there is no declared encoding to decode them by.
    # Replacement decoding would rewrite them -- a name spelled in ISO-8859-1
    # becomes a name with a replacement character in it, and a denylist literal
    # that was in it stops matching. surrogateescape round-trips losslessly, so
    # the text form is usable for patterns while the original bytes stay
    # available for the comparison that has to be exact.
    raw_fields = identity_stream.split(b"\x00")
    fields = [value.decode("utf-8", "surrogateescape") for value in raw_fields]
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
    raw_by_sha: dict[str, list[bytes]] = {}
    for index in range(0, len(fields), IDENTITY_FIELDS):
        values = [
            value.strip("\n") for value in fields[index : index + IDENTITY_FIELDS]
        ]
        raw_values = [
            value.strip(b"\n") for value in raw_fields[index : index + IDENTITY_FIELDS]
        ]
        if not re.fullmatch(r"[0-9a-f]{40}", values[0]):
            raise GateError(
                f"commit identity stream is misaligned at {values[0][:40]!r}; "
                "refusing to report a partial scan as clean"
            )
        commits.append(values)
        raw_by_sha[values[0]] = raw_values

    messages = _commit_messages([values[0] for values in commits], root)
    findings: list[tuple[str, int, str]] = []
    for (
        sha,
        source,
        _author_name,
        author_email,
        _committer_name,
        committer_email,
    ) in commits:
        where = f"commit {sha[:9]} (via {source})"
        decoded, raw_body = messages[sha]
        message = CONTROL_BYTE_PATTERN.sub("\n", decoded)
        # Headers are scanned alongside the message for addresses: a mergetag
        # carries its tagger's address, and it is just as published as the
        # commit's own.
        header_source = _scannable_headers(raw_body)
        header_text = CONTROL_BYTE_PATTERN.sub("\n", header_source)

        for address in dict.fromkeys((author_email, committer_email)):
            if not address or RESERVED_EMAIL_DOMAIN_PATTERN.search(address):
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"commit-email ({address})"))
        # Ident lines inside embedded objects get the structured check, the
        # same one the commit's own author and committer get, rather than being
        # left to the text pattern.
        # Parsed from the un-normalised text. Rewriting control bytes to
        # newlines is what makes a trailer visible to a line anchor, but it also
        # splits one ident line into two, and the half without the address then
        # looks like an ident that will not parse.
        unreadable_idents = _unreadable_header_lines(raw_body)
        embedded, unparseable = _embedded_idents(header_source)
        unreadable_idents.extend(unparseable)
        if unreadable_idents:
            raise GateError(
                f"commit {sha[:9]} has a header line that cannot be read "
                f"({unreadable_idents[0]!r}); refusing to certify metadata this "
                "gate cannot parse"
            )
        for address in dict.fromkeys(embedded):
            address = address.strip()
            if not address or RESERVED_EMAIL_DOMAIN_PATTERN.search(address):
                continue
            if address in (author_email, committer_email):
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"commit-email in metadata ({address})"))
        # Defence in depth: an address can also sit in prose, where there is no
        # structure to parse and a pattern is all there is.
        for address in dict.fromkeys(
            EMAIL_PATTERN.findall(message + "\n" + header_text)
        ):
            if address in (author_email, committer_email):
                continue
            if RESERVED_EMAIL_DOMAIN_PATTERN.search(address):
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"commit-email in message ({address})"))

        # Trailers are searched in the headers too. A mergetag's embedded tag
        # message can carry one, and it is published with the commit exactly
        # like a trailer in the commit's own message would be.
        searchable = message + "\n" + header_text
        for pattern in trailers:
            match = pattern.search(searchable)
            if match:
                end_of_line = searchable.find("\n", match.start())
                line = searchable[
                    match.start() : end_of_line if end_of_line != -1 else None
                ]
                findings.append((where, 0, f"commit-trailer ({line.strip()[:60]})"))
        # The denylist compares against the whole object's bytes, so a literal
        # anywhere in it -- a header, a mergetag's embedded message, the commit
        # message itself -- is found, whatever it was encoded in.
        raw_identity = [*raw_by_sha.get(sha, []), raw_body]
        matched_literal = False
        for literal in denylist:
            if literal in message or _literal_in_raw_fields(literal, raw_identity):
                findings.append((where, 0, "denylist-literal"))
                matched_literal = True
        # The header block joins the ident fields under the same rule. The
        # message body does not need it: it is decoded strictly in the encoding
        # the object declares and raises if it will not, so an undecodable
        # message never reaches a comparison at all.
        inconclusive = not _identity_is_canonically_readable(
            [*raw_by_sha.get(sha, []), raw_body.partition(b"\n\n")[0]]
        )
        if denylist and not matched_literal and inconclusive:
            # Nothing matched, and the bytes are not readable in a way that
            # makes "nothing matched" mean anything. Reporting clean here would
            # be a guess presented as a result.
            raise GateError(
                f"commit {sha[:9]} has metadata bytes that are not valid UTF-8, so "
                "a denylist comparison against them is not conclusive; refusing to "
                "certify metadata this gate cannot read canonically"
            )
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
