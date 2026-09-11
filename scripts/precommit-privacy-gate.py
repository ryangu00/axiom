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
        not _is_reserved_documentation_address(address)
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


def _commit_messages(
    shas: list[str], root: Path, *, kind: str = "commit"
) -> dict[str, tuple[str, bytes]]:
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
        sha, object_kind, size_text = header
        if sha != expected:
            raise GateError(f"expected object {expected[:9]}, got {sha[:40]!r}")
        if object_kind != kind:
            raise GateError(f"object {sha[:9]} is a {object_kind}, not a {kind}")
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


# A git object's header block has a grammar: each entry is a keyword, a space
# and a value, and a value may continue onto following lines that begin with a
# space. Nothing else is valid.
HEADER_KEYWORD = re.compile(rb"^[a-z][a-z0-9-]*$")
# Headers that embed a whole object as their value. Its header block has the
# same grammar, so it is read the same way -- recursion, not a second parser.
OBJECT_VALUED_HEADERS = (b"mergetag",)


def _header_entries(header_block: bytes) -> tuple[list[tuple[bytes, bytes]], list[str]]:
    """Split a header block into entries, reporting lines that are not entries.

    Returns (entries, unreadable). This is a whitelist, deliberately. Four
    rounds of review found the same defect four times -- a keyword spelled with
    a separator, a tab, a carriage return inside it -- because each fix named
    one more byte that must not appear. Naming bad bytes is a losing position:
    a line only has to fail *recognition*, and there is always one more way to
    do that. So nothing is recognised leniently here. A line is an entry git
    could have written, or it is refused.
    """
    entries: list[tuple[bytes, list[bytes]]] = []
    unreadable: list[str] = []
    for line in header_block.split(b"\n"):
        if line.startswith(b" "):
            if entries:
                entries[-1][1].append(line[1:])
            else:
                unreadable.append(_readable(line))
            continue
        if not line:
            continue
        keyword, separator, value = line.partition(b" ")
        if not separator or not HEADER_KEYWORD.match(keyword):
            unreadable.append(_readable(line))
            continue
        entries.append((keyword, [value]))
    return [(keyword, b"\n".join(values)) for keyword, values in entries], unreadable


def _readable(line: bytes) -> str:
    return line.decode("utf-8", "replace")[:80].replace("\ufffd", "?")


MAX_EMBEDDED_DEPTH = 8


def _walk_header_blocks(
    header_block: bytes, depth: int = 0
) -> tuple[list[bytes], list[str]]:
    """Every header block reachable from this one, and the lines that are not entries.

    Actually recursive, which the previous version claimed and was not: an
    object-valued header holds an object, and that object's header can hold
    another. Bounded, because a self-referential chain is a way to make this
    function the problem.
    """
    if depth > MAX_EMBEDDED_DEPTH:
        return [], [f"embedded objects nested deeper than {MAX_EMBEDDED_DEPTH}"]
    entries, unreadable = _header_entries(header_block)
    blocks = [header_block]
    for keyword, value in entries:
        if keyword not in OBJECT_VALUED_HEADERS:
            continue
        embedded, _, _ = value.partition(b"\n\n")
        deeper_blocks, deeper_unreadable = _walk_header_blocks(embedded, depth + 1)
        blocks.extend(deeper_blocks)
        unreadable.extend(deeper_unreadable)
    return blocks, unreadable


def _unreadable_header_lines(body: bytes) -> list[str]:
    """Header lines that are not entries git could have written.

    The commit's own author and committer lines are exempt from the refusal:
    those fields reach the checks through git's own structured extraction in
    the identity stream, which reads them whatever they contain.
    """
    headers, _, _ = body.partition(b"\n\n")
    _, unreadable = _walk_header_blocks(headers)
    return [
        line
        for line in unreadable
        if not line.startswith(("tree ", "parent ", "author ", "committer "))
    ]


def _header_addresses(body: bytes) -> tuple[list[str], list[str]]:
    """Every bracketed value in every reachable header, whatever the keyword.

    Checking only `tagger`, `author` and `committer` made the keyword the thing
    that decides whether an address gets looked at -- so a header named
    anything else carried one past the structured check, and past the text
    pattern too if its domain had no dot. The keyword is not evidence about
    the value. Every bracketed token gets the same treatment as a commit's own
    address: whole value, structured comparison.
    """
    headers, _, _ = body.partition(b"\n\n")
    blocks, _ = _walk_header_blocks(headers)
    addresses: list[str] = []
    unreadable: list[str] = []
    for block in blocks:
        for entry_keyword, value in _header_entries(block)[0]:
            if entry_keyword in (b"tree", b"parent"):
                continue
            text = value.decode("utf-8", "surrogateescape")
            # Angle brackets are a convention, not a requirement. An address
            # written bare in a header value is published exactly the same, and
            # a dotless domain makes it invisible to the text pattern too, so
            # every whitespace-or-bracket separated token holding an `@` is
            # checked rather than only the bracketed ones.
            for token in re.split(r"[\s<>]+", text):
                if "@" in token:
                    addresses.append(token)
            position = 0
            while True:
                opened = text.find("<", position)
                if opened == -1:
                    break
                closed = text.find(">", opened + 1)
                if closed == -1:
                    # An opening bracket with nothing closing it. Stopping here
                    # quietly would leave whatever follows unexamined, and what
                    # follows is where the address is.
                    unreadable.append(
                        (entry_keyword.decode("ascii", "replace") + " " + text)[:80]
                    )
                    break
                addresses.append(text[opened + 1 : closed])
                position = closed + 1
    return addresses, unreadable


def _scannable_headers(body: bytes) -> str:
    """Header text worth scanning, which is all of it but tree and parent.

    A commit object carries more than an ident line and a message. `mergetag`
    embeds an entire tag object -- its tagger identity and its own message --
    and other headers can carry text too. `tree` and `parent` are object names
    and hold nothing but hex.
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


def _is_reserved_documentation_address(address: str) -> bool:
    """Whether this is a reserved name -- asked only of something that parses as one.

    Order matters here and was wrong. Testing the reserved pattern first meant
    an address of the shape `mailbox@real-domain` + `@` + `example.com` -- two
    at-signs, a live mailbox in front, a reserved name on the end -- matched
    the exemption and was waved through before anything asked whether it was a
    single address at all. Ordinary porcelain sets exactly that as an author
    address; no hand-written object is needed.

    (Spelled out rather than shown, because this file is scanned by the gate it
    implements and a routable-looking literal in a docstring is a finding --
    correctly so.)
    """
    if address.count("@") != 1:
        return False
    _, _, domain = address.partition("@")
    return bool(RESERVED_EMAIL_DOMAIN_PATTERN.search("@" + domain.strip()))


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


def _annotated_tags(root: Path) -> list[tuple[str, str]]:
    """(ref, object name) for every ref that points at a tag object.

    A lightweight tag is a ref pointing straight at a commit and carries no
    metadata of its own. An annotated tag is an object with a tagger and a
    message, and pushing it publishes both -- through `git tag -a` and `git
    push`, which is as ordinary as git usage gets. Walking commits never
    reaches it.
    """
    listing = _run_git(
        [
            "for-each-ref",
            "--format=%(objecttype) %(objectname) %(refname)",
            "refs/tags",
        ],
        cwd=root,
    )
    tags: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in listing.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3 or parts[0] != "tag":
            continue
        ref, name = parts[2], parts[1]
        # Follow the chain. A tag can point at another tag, and pushing the
        # outer one publishes every object under it -- so a tag with a
        # sensitive tagger can be wrapped in a clean one and have its own ref
        # deleted, leaving it published and named by no ref at all.
        for depth in range(MAX_EMBEDDED_DEPTH + 1):
            if name in seen:
                break
            if depth == MAX_EMBEDDED_DEPTH:
                # The bound is a guard against a cycle, not a licence to stop
                # looking. Returning here would report clean about a tag the
                # scan never opened, which is the thing the bound exists to
                # avoid becoming.
                raise GateError(
                    f"tag chain under {ref} is deeper than {MAX_EMBEDDED_DEPTH}; "
                    "refusing to certify tags this scan did not reach"
                )
            seen.add(name)
            tags.append((ref if depth == 0 else f"{ref} (nested {depth})", name))
            body = _run_git(["cat-file", "-p", name], cwd=root)
            target = ""
            for header in body.split("\n"):
                if header.startswith("object "):
                    target = header[len("object ") :].strip()
                    break
                if not header:
                    break
            if not target:
                break
            if _run_git(["cat-file", "-t", target], cwd=root).strip() != "tag":
                break
            name = target
    return tags


def scan_reachable_blobs(root: Path) -> list[tuple[str, int, str]]:
    """Scan the content of every blob reachable from any ref.

    A file deleted in a later commit is still in the history, still reachable,
    and still published by the next push. Scanning the working tree answers a
    different question: what the repository looks like now, not what it will
    hand to whoever clones it. The classic case -- commit a secret, delete it,
    commit again -- passes a tracked-file scan cleanly.

    Note bodies arrive here too. `git notes` stores them as blobs under
    `refs/notes/`, so the note's commit is metadata the history scan already
    reads while its content is a blob that only this pass sees.
    """
    configuration = load_configuration(root)
    detect_ip = configuration.get("detect_ip", True)
    if not isinstance(detect_ip, bool):
        raise GateError("detect_ip must be true or false")
    hostname_patterns = compile_hostname_patterns(configuration)
    denylist = load_denylist(root)

    listing = _bounded_git_output(
        ["rev-list", "--objects", "--all"], cwd=root, budget=MAX_GIT_OUTPUT_BYTES
    ).decode("utf-8", "surrogateescape")
    names: dict[str, str] = {}
    for line in listing.splitlines():
        name, _, path = line.partition(" ")
        if re.fullmatch(r"[0-9a-f]{40}", name):
            names.setdefault(name, path.strip())
    if not names:
        return []

    with tempfile.TemporaryDirectory() as workspace:
        request = Path(workspace) / "objects"
        request.write_bytes("\n".join(names).encode("ascii") + b"\n")
        types = _bounded_git_output(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=root,
            budget=MAX_GIT_OUTPUT_BYTES,
            stdin_path=request,
        ).decode("ascii", "replace")
    blobs = [
        line.split(" ")[0]
        for line in types.splitlines()
        if line.endswith(" blob") and re.fullmatch(r"[0-9a-f]{40}", line.split(" ")[0])
    ]
    if not blobs:
        return []

    findings: list[tuple[str, int, str]] = []
    # Read in slices so one enormous repository refuses on its own budget
    # rather than on the machine's memory.
    for start in range(0, len(blobs), 512):
        chunk = blobs[start : start + 512]
        with tempfile.TemporaryDirectory() as workspace:
            request = Path(workspace) / "blobs"
            request.write_bytes("\n".join(chunk).encode("ascii") + b"\n")
            data = _bounded_git_output(
                ["cat-file", "--batch"],
                cwd=root,
                budget=MAX_GIT_OUTPUT_BYTES,
                stdin_path=request,
            )
        position = 0
        for expected in chunk:
            newline = data.find(b"\n", position)
            if newline == -1:
                raise GateError(
                    f"object stream ended before {expected[:9]}; refusing to "
                    "report a partial scan as clean"
                )
            header = data[position:newline].decode("utf-8", "replace").split(" ")
            if len(header) != 3 or header[0] != expected:
                raise GateError(
                    f"expected object {expected[:9]}, got {' '.join(header)[:40]!r}"
                )
            size = int(header[2]) if header[2].isdigit() else -1
            if size < 0:
                raise GateError(f"object {expected[:9]} has no readable size")
            body = data[newline + 1 : newline + 1 + size]
            if len(body) != size:
                raise GateError(
                    f"object {expected[:9]} is truncated; refusing to report a "
                    "partial scan as clean"
                )
            position = newline + 1 + size + 1
            # Git's own test for binary is a NUL byte near the start, and that
            # is the one used here. Skipping everything that is not valid UTF-8
            # threw away far more: a text file with one stray byte in it is
            # ordinary, and skipping the whole blob left the legible address on
            # the line above it unread. Anything without a NUL is decoded
            # losslessly and scanned; the invalid bytes survive as surrogates
            # and match nothing, which is the correct outcome for them.
            if b"\x00" in body[:8000]:
                continue
            text = body.decode("utf-8", "surrogateescape")
            where = f"object {expected[:9]} ({names.get(expected) or 'no path'})"
            for number, content in enumerate(text.splitlines(), 1):
                for kind in inspect_line(
                    content,
                    detect_ip=detect_ip,
                    hostname_patterns=hostname_patterns,
                    denylist=denylist,
                ):
                    findings.append((where, number, kind))
    return findings


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

    def check_object(
        where: str,
        label: str,
        raw_body: bytes,
        decoded: str,
        known: tuple[str, ...],
        extra_identity: list[bytes],
    ) -> None:
        """Every metadata check, applied identically to a commit or a tag.

        One body, called twice, because a second copy for tags is how the two
        drift apart -- and a check that exists for commits and not for tags is
        the shape of every finding this file has been through.
        """
        message = CONTROL_BYTE_PATTERN.sub("\n", decoded)
        reported: set[str] = set()
        header_source = _scannable_headers(raw_body)
        header_text = CONTROL_BYTE_PATTERN.sub("\n", header_source)

        for address in dict.fromkeys(known):
            if not address or _is_reserved_documentation_address(address):
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"{label}-email ({address})"))
                reported.add(address)

        unreadable = _unreadable_header_lines(raw_body)
        embedded, unbalanced = _header_addresses(raw_body)
        unreadable.extend(unbalanced)
        for address in dict.fromkeys(embedded):
            address = address.strip()
            if not address or _is_reserved_documentation_address(address):
                continue
            if address in known:
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"{label}-email in metadata ({address})"))
                reported.add(address)
        if unreadable:
            # Raised after the addresses were checked rather than before: a
            # refusal says only that something could not be read, and whatever
            # was legible in the same object is worth naming in the same breath.
            found = [kind for location, _, kind in findings if location == where]
            detail = (
                f". Legible findings in the same object: {'; '.join(found)}"
                if found
                else ""
            )
            raise GateError(
                f"{where} has a header line that cannot be read "
                f"({unreadable[0]!r}); refusing to certify metadata this gate "
                f"cannot parse{detail}"
            )

        searchable = message + "\n" + header_text
        for address in dict.fromkeys(EMAIL_PATTERN.findall(searchable)):
            # Already reported by the structured pass. Saying it twice does not
            # make it truer and buries the second, different finding under a
            # repeat of the first.
            if address in known or address in reported:
                continue
            if _is_reserved_documentation_address(address):
                continue
            if not _address_is_allowed(address, allowlist):
                findings.append((where, 0, f"{label}-email in message ({address})"))

        for pattern in trailers:
            match = pattern.search(searchable)
            if match:
                # The pattern's leading `\s*` can begin the match on the newline
                # before the trailer, so the reported line has to be found from
                # the match's END backwards, not from where the match started.
                line_start = searchable.rfind("\n", 0, match.end()) + 1
                end_of_line = searchable.find("\n", match.end())
                line = searchable[
                    line_start : end_of_line if end_of_line != -1 else None
                ]
                findings.append((where, 0, f"{label}-trailer ({line.strip()[:60]})"))

        raw_identity = [*extra_identity, raw_body]
        matched_literal = False
        for literal in denylist:
            if literal in message or _literal_in_raw_fields(literal, raw_identity):
                findings.append((where, 0, "denylist-literal"))
                matched_literal = True
        inconclusive = not _identity_is_canonically_readable(
            [*extra_identity, raw_body.partition(b"\n\n")[0]]
        )
        if denylist and not matched_literal and inconclusive:
            raise GateError(
                f"{where} has metadata bytes that are not valid UTF-8, so a "
                "denylist comparison against them is not conclusive; refusing to "
                "certify metadata this gate cannot read canonically"
            )

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
        check_object(
            where,
            "commit",
            raw_body,
            decoded,
            (author_email, committer_email),
            raw_by_sha.get(sha, []),
        )

    # Annotated tags are objects in their own right, with their own tagger and
    # message, and `git tag -a` plus `git push` publishes both. Walking commits
    # never reaches them -- the walk sees the commit a tag points at, not the
    # tag.
    tags = _annotated_tags(root)
    if tags:
        tag_bodies = _commit_messages([name for _, name in tags], root, kind="tag")
        for ref, name in tags:
            decoded, raw_body = tag_bodies[name]
            check_object(f"tag {ref}", "tag", raw_body, decoded, (), [])
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
            "scan every tracked file's content, all reachable commit and tag "
            "metadata, AND the content of every reachable object (release/CI "
            "gate), not just staged additions"
        ),
    )
    parser.add_argument(
        "--scan-blobs",
        action="store_true",
        help="scan the content of every blob reachable from any ref, history included",
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
        elif arguments.scan_blobs:
            diff = ""
            findings = scan_reachable_blobs(root)
        elif arguments.scan_all:
            diff = ""
            findings = (
                scan_tracked(root) + scan_history(root) + scan_reachable_blobs(root)
            )
        else:
            diff = staged_diff(root)
            findings = scan(diff, root)
    except GateError as error:
        print(f"Privacy gate error: {error}", file=sys.stderr)
        return 2

    if not findings:
        if arguments.scan_history:
            scope = "commit metadata"
        elif arguments.scan_blobs:
            scope = "reachable object content"
        elif arguments.scan_all:
            scope = "tracked files, commit metadata and reachable history"
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
