#!/usr/bin/env python3
"""Shared state primitives for Axiom Claude Code hooks."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import predicate_evaluator

SCHEMA_VERSION = "v1"
_lock_degraded_emitted = False

# The one place the operator-facing escape hatch is spelled. Every hook that
# blocks or advises quotes it, and the CLI's `enforce` verb takes exactly this
# argument order (`<rule> on|off`); a test parses the string through the real
# parser so the two cannot drift apart again.
ESCAPE_HATCH_TEMPLATE = "/axiom:enforce {rule} off"


def escape_hatch(rule: str) -> str:
    """Return the slash command that switches `rule` back to observe mode."""
    return ESCAPE_HATCH_TEMPLATE.format(rule=rule)


def temp_roots(
    configured: Any = None, environ: Mapping[str, str] | None = None
) -> list[Path]:
    """Resolve configured, environment, and platform temporary directories.

    Shared by the rules that ask "is this path temporary?" so they agree: the
    configured list (default `/tmp`, `/var/tmp`), then `TMPDIR`, then the
    Windows `TEMP`/`TMP` variables, then `tempfile.gettempdir()` unless it
    resolves to the cwd or an ancestor. On macOS, libc also supplies the
    per-user temp directory even when `TMPDIR` is absent. Discovery errors
    leave the other roots intact; explicit roots are never cwd-filtered.
    """
    environment = os.environ if environ is None else environ
    values: list[str] = (
        [item for item in configured if isinstance(item, str)]
        if isinstance(configured, list)
        else ["/tmp", "/var/tmp"]
    )
    for variable in ("TMPDIR", "TEMP", "TMP"):
        if environment.get(variable):
            values.append(environment[variable])
    with contextlib.suppress(Exception):
        temporary = Path(tempfile.gettempdir()).resolve()
        cwd = Path.cwd().resolve()
        if temporary != cwd and temporary not in cwd.parents:
            values.append(str(temporary))
    if sys.platform == "darwin":
        with contextlib.suppress(Exception):
            import ctypes

            confstr = ctypes.CDLL(None).confstr
            confstr.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_char),
                ctypes.c_size_t,
            ]
            confstr.restype = ctypes.c_size_t
            # _CS_DARWIN_USER_TEMP_DIR from the macOS SDK; includes the NUL.
            size = confstr(65537, None, 0)
            if size:
                buffer = ctypes.create_string_buffer(size)
                written = confstr(65537, buffer, size)
                if 0 < written <= size:
                    values.append(os.fsdecode(buffer.value))
    roots: list[Path] = []
    for value in values:
        if not value:
            continue
        with contextlib.suppress(OSError, RuntimeError):
            resolved = Path(value).expanduser().resolve()
            if resolved not in roots:
                roots.append(resolved)
    return roots


# --- Exclusive file locking -------------------------------------------------
# POSIX uses fcntl advisory locks. Windows has no fcntl, so v1 failed at import
# there (docs/KNOWN-LIMITATIONS.md "Not supported: Windows"). msvcrt.locking is
# the equivalent primitive, so the two branches below give the same guarantee
# the state layer already relied on: one writer at a time.
#
# The POSIX branch is byte-for-byte the previous behaviour. Two differences are
# inherent to the Windows API and are why the branch is not a silent swap:
#
#   * msvcrt locks a byte range at the current file offset rather than the whole
#     file, so we pin byte 0 as the mutex. Callers open the ledger in append
#     mode, where writes go to EOF regardless of the seek position, so moving
#     the offset to take the lock cannot misplace a record.
#   * flock() blocks forever; the Windows branch waits with a deadline instead.
#     Bounded waiting is the better failure for a verifier that must not wedge
#     its host, and both call sites already handle OSError -- append_ledger
#     surfaces it as the contract's `recorded: false`, and _claim_lock degrades
#     to no-lock. The wait is built from LK_NBLCK plus our own retry loop
#     rather than LK_LOCK, whose fixed 10-attempt/10-second ceiling is not a
#     timeout we chose: under real contention (50 concurrent appenders in the
#     test suite, where Windows spawns rather than forks) it expires while the
#     lock is merely busy, turning correct serialisation into EDEADLOCK.
if sys.platform == "win32":  # pragma: no cover - platform-specific
    import msvcrt

    _LOCK_TIMEOUT_S = 60.0
    _LOCK_POLL_S = 0.01

    def _lock_exclusive(handle) -> None:
        handle.seek(0)
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_POLL_S)

    def _lock_release(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_exclusive(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _lock_release(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class ClaimRegistration:
    """Typed result of attempting to occupy the single active-claim slot."""

    registered: bool
    claim: dict[str, Any]


@dataclass(frozen=True)
class ConfigLoad:
    """Typed result of reading an object-shaped project config."""

    status: Literal["absent", "valid", "invalid", "unreadable"]
    data: dict[str, Any]
    reason: str | None


class ClaimSlotCorrupt(Exception):
    """The active-claim slot exists but does not hold a claim.

    Raised instead of reading the slot as empty. A slot that cannot be parsed
    must never be mistaken for "no claim registered": that is exactly the
    state a false "done" needs, and treating it as empty also lets the next
    registration overwrite the only evidence of what happened (CONTRACTS §2).
    """

    def __init__(self, path: Path, status: str, reason: str | None) -> None:
        super().__init__(f"active claim slot is {status}: {path} ({reason})")
        self.path = path
        self.status = status
        self.reason = reason


def finding_id(rule: str, *parts: Any) -> str:
    """Stable id joining every ledger record about one finding (CONTRACTS §6).

    Observe and enforce records of the same claim, and the `verified` that
    eventually closes it, share one id, so a labeling pass can follow a
    finding across stops without parsing claim bodies. The hash covers only
    the rule and identity tokens, never evidence, so the id does not change
    as the evidence does.
    """
    material = "\x1f".join([rule, *(_text(part) for part in parts)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def claim_finding_id(rule: str, claim: Mapping[str, Any]) -> str:
    """A claim's finding id: from `claim_id`, or the legacy timestamp token."""
    token = claim.get("claim_id")
    if not isinstance(token, str) or not token:
        baseline = claim.get("baseline")
        baseline = baseline if isinstance(baseline, Mapping) else {}
        token = baseline.get("registered_at")
    return finding_id(rule, token)


def record_best_effort(ledger: Path | str, record: Mapping[str, Any]) -> bool:
    """Append a record whose failure must not change the decision it describes.

    The enforce-mode records (`blocked`, `advice_injected`) are written right
    before the hook acts. If that append raised into the hook's fail-open
    handler, a full disk would turn a block into a pass -- so the failure is
    reported on stderr and returned, never raised.
    """
    try:
        append_ledger(ledger, record)
        return True
    except Exception as error:
        print(
            f"axiom: {record.get('event', 'ledger')} record NOT confirmed -- "
            f"appending to {ledger} raised {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return False


def fail_open(
    hook: str,
    error: BaseException,
    *,
    ledger: Path | str | None = None,
    rule: str | None = None,
) -> dict[str, str]:
    """Report a hook failure on every channel the host offers, then proceed.

    stderr is what a developer sees, the ledger is what `/axiom:report` sees,
    and the returned `systemMessage` is what the host shows the operator. No
    decision field is set and the exit code stays 0, so the host proceeds
    exactly as if the hook had said nothing -- fail-open -- but never
    silently: a fail-open the operator cannot see is indistinguishable from a
    hook that was never installed. The ledger write is best-effort; the
    message is not.
    """
    message = f"axiom {hook}: fail-open ({type(error).__name__}: {error})"
    print(message, file=sys.stderr)
    if ledger is not None:
        with contextlib.suppress(Exception):
            record: dict[str, Any] = {
                "event": "error",
                "hook": hook,
                "error": f"{type(error).__name__}: {error}",
                "action": "fail_open",
            }
            if rule is not None:
                record["rule"] = rule
            append_ledger(ledger, record)
    return {"systemMessage": message}


def fail_open_main(
    hook: str, rule: str | None, error: BaseException, payload: Any
) -> int:
    """`main()`-level fail-open shared by every hook.

    Derives the ledger from the payload's `cwd` when it can (the hook may have
    died before computing its paths), reports through `fail_open`, prints the
    `systemMessage` line as the hook's only stdout, and exits 0.
    """
    ledger: Path | None = None
    with contextlib.suppress(Exception):
        source = payload if isinstance(payload, Mapping) else {}
        cwd_value = source.get("cwd")
        cwd = (
            Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
        )
        ledger = state_paths(cwd=cwd)["ledger"]
    print(
        json.dumps(
            fail_open(hook, error, ledger=ledger, rule=rule),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


def _canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def data_root(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve state root from argv, environment, then the home fallback."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ

    for index, argument in enumerate(arguments):
        # An empty value means the shell expanded an unset variable (e.g.
        # ${CLAUDE_PLUGIN_DATA} in hooks.json); treat it as unset rather than
        # resolving Path("") to the process cwd and scattering state there.
        if argument == "--data-root" and index + 1 < len(arguments):
            if arguments[index + 1]:
                return _canonical(Path(arguments[index + 1]))
            continue
        if argument.startswith("--data-root="):
            value = argument.split("=", 1)[1]
            if value:
                return _canonical(Path(value))

    configured = environment.get("CLAUDE_PLUGIN_DATA")
    if configured:
        return _canonical(Path(configured))

    resolved_home = Path.home() if home is None else home
    return _canonical(resolved_home / ".axiom")


def _git_value(cwd: Path, argument: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", argument],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def _resolve_git_path(cwd: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return _canonical(candidate)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "worktree"


def project_id(cwd: Path | str | None = None) -> str:
    """Return a stable project id, distinguishing linked Git worktrees."""
    working_directory = _canonical(Path.cwd() if cwd is None else Path(cwd))
    git_dir_value = _git_value(working_directory, "--git-dir")
    common_dir_value = _git_value(working_directory, "--git-common-dir")

    if not git_dir_value or not common_dir_value:
        basis = str(working_directory)
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]

    git_dir = _resolve_git_path(working_directory, git_dir_value)
    common_dir = _resolve_git_path(working_directory, common_dir_value)
    base = hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:12]
    if git_dir == common_dir:
        return base

    top_level_value = _git_value(working_directory, "--show-toplevel")
    worktree_name = (
        _resolve_git_path(working_directory, top_level_value).name
        if top_level_value
        else working_directory.name
    )
    return f"{base}-wt-{_slug(worktree_name)}"


def state_paths(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Path]:
    """Return all versioned state paths for one project."""
    resolved_root = data_root() if root is None else _canonical(Path(root))
    version_root = resolved_root / SCHEMA_VERSION
    project_root = version_root / "projects" / project_id(cwd)
    return {
        "data_root": resolved_root,
        "version_root": version_root,
        "project_root": project_root,
        "ledger": project_root / "ledger.jsonl",
        "config": project_root / "config.json",
        "lessons": project_root / "lessons.md",
        "active_claim": project_root / "claims" / "active.json",
        "claim_lock": project_root / "claims" / "claim.lock",
        "stuck_search": project_root / "stuck-search.json",
        "preflight": project_root / "preflight.json",
        # Which broken config this project already reported, per session
        # (CONTRACTS §3): without it every hook process re-reports it.
        "config_degraded": project_root / "config-degraded.json",
        "global": version_root / "global.json",
        # Adapter shims append their observable events at the data root
        # (not per-project); it must be uninstallable like everything else.
        "adapter_events": resolved_root / "adapter-events.jsonl",
    }


def ensure_layout(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Path]:
    """Create the current schema directories without modifying older versions."""
    paths = state_paths(root=root, cwd=cwd)
    paths["project_root"].mkdir(parents=True, exist_ok=True)
    return paths


def append_ledger(path: Path | str, record: Mapping[str, Any]) -> None:
    """Append one complete JSON object while holding an exclusive file lock."""
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(record)
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"

    with ledger_path.open("a", encoding="utf-8") as ledger:
        _lock_exclusive(ledger)
        try:
            ledger.write(encoded)
            ledger.flush()
            os.fsync(ledger.fileno())
        finally:
            _lock_release(ledger)


# How long a Windows replace may keep retrying past a reader's open handle.
# Long enough to outlast the read-parse-close of a small JSON state file, short
# enough that a genuinely stuck handle surfaces as an error instead of a hang.
_REPLACE_RETRY_SECONDS = 2.0


def _atomic_replace(source: Path, target: Path) -> None:
    """Swap `source` over `target` without a reader ever seeing a partial file.

    os.replace, not os.rename: POSIX rename() silently replaces an existing
    target, but the Win32 call behind os.rename refuses one, so on Windows
    every state file after its first write raised FileExistsError.

    Windows needs the retry as well. Its file locking is mandatory rather than
    advisory, and CPython's open() does not ask for FILE_SHARE_DELETE, so any
    concurrent reader -- even one that only reads and closes -- makes the swap
    fail with a sharing violation while POSIX would have allowed it. Retrying
    keeps the guarantee callers actually depend on (a reader never observes a
    half-written object) without pretending the platforms behave alike.

    Bounded on purpose: a writer that spins forever waiting on someone else's
    handle is a worse failure than one that raises.
    """
    if sys.platform != "win32":
        os.replace(source, target)
        return

    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    delay = 0.001
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def write_config(path: Path | str, config: Mapping[str, Any]) -> None:
    """Atomically replace a JSON config using a same-directory temporary file."""
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(config, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        _atomic_replace(temporary_path, config_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def load_config(path: Path | str) -> ConfigLoad:
    """Read a config while preserving absence, validity, and failure state."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConfigLoad(status="absent", data={}, reason=None)
    except OSError as error:
        reason = f"{type(error).__name__}: {error}"
        return ConfigLoad(status="unreadable", data={}, reason=reason)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        reason = f"{type(error).__name__}: {error}"
        return ConfigLoad(status="invalid", data={}, reason=reason)
    if not isinstance(payload, dict):
        reason = f"expected JSON object, got {type(payload).__name__}"
        return ConfigLoad(status="invalid", data={}, reason=reason)
    return ConfigLoad(status="valid", data=payload, reason=None)


def _degraded_fingerprint(path: Path, result: ConfigLoad) -> str:
    """Name one broken state of a config: its status plus its bytes.

    Unreadable files have no bytes to hash, so their reason stands in. The
    fingerprint only decides whether a state was already reported; it is
    never evidence (CONTRACTS §6).
    """
    try:
        content = path.read_bytes()
    except OSError:
        content = (result.reason or "").encode("utf-8")
    digest = hashlib.sha256(result.status.encode("utf-8") + b"\0" + content)
    return digest.hexdigest()[:16]


def _forget_degraded(marker: Path, config_path: str) -> None:
    """Drop a repaired config from the marker so the next break is reported."""
    if not marker.exists():
        return
    with contextlib.suppress(OSError):
        state = read_json(marker)
        if state.pop(config_path, None) is None:
            return
        if state:
            write_json(marker, state)
        else:
            marker.unlink(missing_ok=True)


def load_hook_config(
    path: Path | str,
    *,
    ledger: Path | str,
    hook: str,
    degraded_paths: set[str] | None = None,
    session_id: str | None = None,
    marker: Path | str | None = None,
) -> ConfigLoad:
    """Load typed config; record a broken one once per session (CONTRACTS §3).

    Without `marker` (the CLI's own loads) the record is per invocation, as
    before. With it, a hook records a broken config once per session for
    each distinct broken state: every hook runs in its own process, so a
    per-process set alone writes one record per tool call for as long as the
    config stays broken. A repaired config is forgotten, so breaking it
    again is reported again.
    """
    result = load_config(path)
    config_path = str(_canonical(Path(path)))
    if result.status not in {"invalid", "unreadable"}:
        if marker is not None:
            _forget_degraded(Path(marker), config_path)
        return result
    display_path = str(Path(path).expanduser())
    seen = degraded_paths if degraded_paths is not None else set()
    if config_path in seen:
        return result
    session = session_id or ""
    fingerprint = ""
    if marker is not None:
        fingerprint = _degraded_fingerprint(Path(path), result)
        reported = read_json(marker).get(config_path)
        if (
            isinstance(reported, Mapping)
            and reported.get("fingerprint") == fingerprint
            and reported.get("session_id") == session
        ):
            seen.add(config_path)
            return result
    append_ledger(
        ledger,
        {
            "event": "config_degraded",
            "status": result.status,
            "path": display_path,
            "reason": result.reason,
            "hook": hook,
        },
    )
    seen.add(config_path)
    if marker is not None:
        # Best-effort: a marker that cannot be written costs a duplicate
        # record next time, never the record itself or the hook.
        with contextlib.suppress(OSError):
            state = read_json(marker)
            state[config_path] = {
                "fingerprint": fingerprint,
                "session_id": session,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            write_json(marker, state)
    return result


def hook_config(
    paths: Mapping[str, Path], payload: Mapping[str, Any], hook: str
) -> ConfigLoad:
    """Load a project's config for a hook, deduplicating degraded records."""
    session = payload.get("session_id")
    return load_hook_config(
        paths["config"],
        ledger=paths["ledger"],
        hook=hook,
        session_id=session if isinstance(session, str) else "",
        marker=paths["config_degraded"],
    )


def read_json(path: Path | str) -> dict[str, Any]:
    """Read an object-shaped JSON state file, returning empty state on failure."""
    return load_config(path).data


def write_json(path: Path | str, payload: Mapping[str, Any]) -> None:
    """Atomically write an object-shaped JSON state file."""
    write_config(path, payload)


def _record_lock_degraded(active_path: Path, error: OSError) -> None:
    """Best-effort, process-deduplicated warning when claim locking is unavailable."""
    global _lock_degraded_emitted
    if _lock_degraded_emitted:
        return
    _lock_degraded_emitted = True
    ledger_path = active_path.parent.parent / "ledger.jsonl"
    entry = {
        "event": "lock_degraded",
        "reason": f"{type(error).__name__}: {error}",
        "lock_path": str(active_path.parent / "claim.lock"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    encoded = (
        json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(ledger_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


@contextlib.contextmanager
def _claim_lock(active_path: Path):
    """Exclusive per-project lock covering register/clear of the active claim so
    a concurrent session cannot slip a new claim between our compare and our
    unlink (closes the compare-and-clear TOCTOU). Degrades to no-lock on
    filesystems without flock support (documented limitation), never wedging."""
    lock_path = active_path.parent / "claim.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        try:
            _lock_exclusive(handle)
        except OSError as error:
            _record_lock_degraded(active_path, error)
        yield
    finally:
        with contextlib.suppress(OSError):
            _lock_release(handle)
        handle.close()


def read_ledger(path: Path | str) -> list[dict[str, Any]]:
    """Read valid object records from a JSONL ledger."""
    try:
        # Records are newline-delimited JSON. `str.splitlines()` also breaks
        # on U+2028/U+2029/NEL and the ASCII separators, which json.dumps
        # leaves unescaped inside strings -- a label or error text carrying
        # one would split its record in two and drop both halves as invalid.
        lines = Path(path).read_text(encoding="utf-8").split("\n")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def register_claim_if_absent(
    claim: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    cwd: Path | str | None = None,
) -> ClaimRegistration:
    """Atomically register a claim only when the single slot is empty."""
    working_directory = _canonical(Path.cwd() if cwd is None else Path(cwd))
    registered = dict(claim)
    predicates = registered.get("predicates", [])
    predicates = predicates if isinstance(predicates, list) else []
    files: dict[str, dict[str, Any]] = {}
    for predicate in predicates:
        # Which paths a predicate needs baselined is the evaluator's decision,
        # not this function's -- a second copy of that rule here is how a guard
        # ends up declared but never snapshotted, which reads as a pass.
        for path_value in predicate_evaluator.baseline_paths(predicate):
            # Baseline and verification MUST resolve paths identically, so the
            # registration snapshot goes through the evaluator's own resolution.
            target = predicate_evaluator.resolve_target(working_directory, path_value)
            files[path_value] = predicate_evaluator.snapshot(target)
    registered["predicates"] = [
        dict(item) if isinstance(item, Mapping) else item for item in predicates
    ]
    registered["claim_id"] = str(uuid.uuid4())
    registered["registered_at"] = datetime.now(timezone.utc).isoformat()
    registered["baseline"] = {
        "git_head": _git_value(working_directory, "HEAD"),
        "files": files,
    }
    paths = ensure_layout(root=root, cwd=working_directory)
    with _claim_lock(paths["active_claim"]):
        slot = load_config(paths["active_claim"])
        if slot.status in ("invalid", "unreadable"):
            _quarantine_claim_slot(paths, slot)
        elif slot.data:
            return ClaimRegistration(registered=False, claim=slot.data)
        write_json(paths["active_claim"], registered)
        return ClaimRegistration(registered=True, claim=registered)


def _quarantine_claim_slot(paths: Mapping[str, Path], slot: ConfigLoad) -> Path:
    """Move an unparsable slot aside, bytes intact, and record that it happened.

    Overwriting it would destroy the only evidence of what happened to the
    claim; refusing to register would leave the project unverified for good.
    So the slot is renamed next to itself with a timestamp, a
    `claim_unreadable` event names the new path, and registration proceeds
    into an empty slot. A rename that fails propagates to the caller's
    fail-open handler -- the slot is then still corrupt and still reported.
    """
    active_path = paths["active_claim"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    quarantined = active_path.with_name(f"{active_path.name}.corrupt-{stamp}")
    # The same replace the state writer uses: on Windows a reader holding the
    # slot open makes a bare os.replace fail, and that path retries.
    _atomic_replace(active_path, quarantined)
    with contextlib.suppress(Exception):
        append_ledger(
            paths["ledger"],
            {
                "event": "claim_unreadable",
                "hook": "register_claim",
                "rule": "write-verify",
                "status": slot.status,
                "reason": slot.reason,
                "path": str(active_path),
                "action": "quarantined",
                "moved_to": str(quarantined),
                "finding_id": finding_id("write-verify", "slot", str(active_path)),
            },
        )
    return quarantined


def read_active_claim(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Any] | None:
    """Read the active claim for the current project or worktree.

    Returns None only for an absent or empty slot. A slot that exists but
    does not parse raises `ClaimSlotCorrupt`; it is never reported as "no
    claim" (CONTRACTS §2).
    """
    active_path = state_paths(root=root, cwd=cwd)["active_claim"]
    slot = load_config(active_path)
    if slot.status in ("invalid", "unreadable"):
        raise ClaimSlotCorrupt(active_path, slot.status, slot.reason)
    return slot.data or None


def clear_active_claim(
    *,
    root: Path | str | None = None,
    cwd: Path | str | None = None,
    expected_claim_id: str | None = None,
    expected_legacy_registered_at: str | None = None,
) -> bool:
    """Remove the active claim after all declared predicates verify.

    New claims compare only by ``claim_id``. Legacy claims without a claim id
    compare only by their old ``baseline.registered_at`` token. Cross-format
    and mismatched comparisons always preserve the current claim.
    """
    active_path = state_paths(root=root, cwd=cwd)["active_claim"]
    with _claim_lock(active_path):
        current = read_json(active_path) or {}
        current_claim_id = current.get("claim_id")
        if isinstance(current_claim_id, str) and current_claim_id:
            if current_claim_id != expected_claim_id:
                return False
        else:
            baseline = current.get("baseline")
            baseline = baseline if isinstance(baseline, Mapping) else {}
            current_legacy_token = baseline.get("registered_at")
            if (
                not isinstance(current_legacy_token, str)
                or current_legacy_token != expected_legacy_registered_at
            ):
                return False
        # An OSError here propagates to the caller's fail-open handler on
        # purpose: a clear we cannot prove must not report success.
        active_path.unlink(missing_ok=True)
        return True


def register_goal_claim(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> ClaimRegistration | None:
    """Register predicates from the first local goal acceptance JSON block."""
    working_directory = _canonical(Path.cwd() if cwd is None else Path(cwd))
    for goal_path in sorted(working_directory.glob("*.goal.md")):
        try:
            text = goal_path.read_text(encoding="utf-8")
        except OSError:
            continue
        section = re.search(
            r"^##\s+acceptance\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
            text,
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if section is None:
            continue
        body = section.group("body")
        fenced = re.search(r"```(?:json)?\s*(?P<json>.*?)\s*```", body, re.DOTALL)
        candidates = [fenced.group("json")] if fenced else []
        candidates.extend(
            line.lstrip()[2:].strip()
            for line in body.splitlines()
            if line.lstrip().startswith("- {")
        )
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, list):
                claim: dict[str, Any] = {"predicates": parsed}
            elif isinstance(parsed, dict):
                claim = parsed
            else:
                continue
            predicates = claim.get("predicates")
            if not isinstance(predicates, list) or not predicates:
                continue
            claim.setdefault("label", goal_path.name.removesuffix(".goal.md"))
            return register_claim_if_absent(claim, root=root, cwd=working_directory)
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def parse_payload(payload: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalize documented and missing hook payload fields without exceptions."""
    source = payload if isinstance(payload, Mapping) else {}
    event = _text(source.get("hook_event_name"))
    response = source.get("tool_response", {})
    response = response if isinstance(response, Mapping) else {}
    error = source.get("error") if event == "PostToolUseFailure" else ""
    return {
        "event": event,
        "error": _text(error),
        "stdout": _text(response.get("stdout")),
        "stderr": _text(response.get("stderr")),
    }


# Two hooks need the shape of a Bash command rather than its text: preflight
# (which simple commands run, with which arguments) and stuck-search (whether
# an exit status of 0 says anything about the command that matters). This is
# a tokenizer, not a shell: no expansion, aliases or functions. `None` means
# the text did not tokenize (unbalanced quotes); callers treat that as
# "shape unknown", never as "safe".
_SHELL_PUNCTUATION = "();<>|&\n"
_HEREDOC_START = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Commands that run the rest of their argument list as a command. The value
# sets name the options that consume the following word, so `sudo -u root rm`
# resolves to `rm`, not `root`.
_COMMAND_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "nice": frozenset({"-n", "--adjustment"}),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    # xargs runs its command on words read from stdin, so the arguments seen
    # here are only part of the real argument list.
    "xargs": frozenset(
        {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter"}
    ),
}
# git options that consume the following word when written apart from it.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


@dataclass(frozen=True)
class ShellShape:
    """Simple commands (argv, redirections removed) and the operators between.

    `operators` lists every list or pipeline operator in source order,
    trailing ones included (`a &` ends in "&"): one of "|", "||", "&&", ";"
    (newlines count as ";") or "&".
    """

    commands: list[list[str]]
    operators: list[str]


def _strip_heredoc_bodies(command: str) -> str:
    """Drop here-document bodies: they are data fed to a command, not commands."""
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            delimiter, tabs = pending[0]
            if (line.lstrip("\t") if tabs else line) == delimiter:
                pending.pop(0)
            continue
        kept.append(line)
        pending.extend(
            (match.group(3), match.group(1) == "-")
            for match in _HEREDOC_START.finditer(line)
        )
    return "\n".join(kept)


def _shell_operator(token: str) -> str | None:
    if "||" in token:
        return "||"
    if "&&" in token:
        return "&&"
    if "|" in token:
        return "|"
    if ";" in token or "\n" in token:
        return ";"
    if "&" in token:
        return "&"
    return None  # parentheses: a command boundary, not a list operator


def shell_shape(command: str) -> ShellShape | None:
    """Split Bash text into simple commands and the operators joining them."""
    lexer = shlex.shlex(
        _strip_heredoc_bodies(command),
        posix=True,
        punctuation_chars=_SHELL_PUNCTUATION,
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[list[str]] = [[]]
    operators: list[str] = []
    skip_target = False
    for token in tokens:
        if skip_target:
            skip_target = False
            continue
        if token and all(char in _SHELL_PUNCTUATION for char in token):
            if "<" in token or ">" in token:
                # A redirection: drop it, its target, and a file-descriptor
                # number written against it ("2>").
                current = commands[-1]
                if current and current[-1].isdigit():
                    current.pop()
                skip_target = True
                continue
            operator = _shell_operator(token)
            if operator is not None:
                operators.append(operator)
            if commands[-1]:
                commands.append([])
            continue
        commands[-1].append(token)
    return ShellShape([argv for argv in commands if argv], operators)


def command_name(argv: Sequence[str]) -> tuple[str, list[str]]:
    """Return the executable's base name and arguments past wrappers.

    Leading assignments (`FOO=1`), group/negation words (`{`, `!`) and the
    wrappers in `_COMMAND_WRAPPERS` are skipped, so `sudo env -i /bin/rm -rf x`
    names `rm`. An argv that is only wrappers names "".
    """
    index = 0
    while index < len(argv):
        word = argv[index]
        if word in {"{", "}", "!"} or _ASSIGNMENT.match(word):
            index += 1
            continue
        name = word.replace("\\", "/").rsplit("/", 1)[-1]
        options = _COMMAND_WRAPPERS.get(name)
        if options is None:
            return name, list(argv[index + 1 :])
        index += 1
        while index < len(argv):
            flag = argv[index]
            if flag == "--":
                index += 1
                break
            if not flag.startswith("-") or flag == "-":
                if name == "env" and _ASSIGNMENT.match(flag):
                    index += 1
                    continue
                break
            index += 2 if flag in options else 1
    return "", []


def git_subcommand(arguments: Sequence[str]) -> tuple[str, list[str]]:
    """Split git's arguments into its subcommand and that subcommand's own.

    `git -C repo push -f` is ("push", ["-f"]); no subcommand is ("", []).
    """
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in _GIT_VALUE_OPTIONS:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            return word, list(arguments[index + 1 :])
    return "", []


def rule_mode(config: Mapping[str, Any] | None, rule: str) -> str:
    """Return a rule's mode, defaulting invalid or missing values to observe."""
    source = config if isinstance(config, Mapping) else {}
    rules = source.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    rule_config = rules.get(rule, {})
    rule_config = rule_config if isinstance(rule_config, Mapping) else {}
    mode = rule_config.get("mode", "observe")
    return mode if mode in {"observe", "enforce"} else "observe"


def record_heartbeat(ledger: Path | str) -> None:
    """Record one SessionStart heartbeat for coverage reporting."""
    append_ledger(ledger, {"event": "heartbeat", "hook": "session_start"})


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_report_data(ledger: Path | str) -> dict[str, Any]:
    """Return rule findings, recent scenes, and hook coverage data."""
    records = read_ledger(ledger)
    rules: dict[str, dict[str, Any]] = {}
    hook_last_active: dict[str, str] = {}
    heartbeats: list[datetime] = []
    config_degraded: dict[str, Any] = {"count": 0}
    lock_degraded: dict[str, Any] = {"count": 0}
    fail_open_summary: dict[str, Any] = {"count": 0}
    for record in records:
        timestamp = _timestamp(record.get("timestamp"))
        hook = record.get("hook")
        if isinstance(hook, str) and timestamp is not None:
            previous = _timestamp(hook_last_active.get(hook))
            if previous is None or timestamp > previous:
                hook_last_active[hook] = timestamp.isoformat()
        if record.get("event") == "heartbeat" and timestamp is not None:
            heartbeats.append(timestamp)
        if record.get("event") == "config_degraded":
            config_degraded = {
                "count": int(config_degraded["count"]) + 1,
                "latest_status": _text(record.get("status")),
                "latest_path": _text(record.get("path")),
                "latest_reason": _text(record.get("reason")),
                "latest_hook": _text(record.get("hook")),
                "latest_timestamp": _text(record.get("timestamp")),
            }
        if record.get("event") == "lock_degraded":
            lock_degraded = {
                "count": int(lock_degraded["count"]) + 1,
                "latest_reason": _text(record.get("reason")),
                "latest_lock_path": _text(record.get("lock_path")),
                "latest_timestamp": _text(record.get("timestamp")),
            }
        if record.get("event") == "error" and record.get("action") == "fail_open":
            # A hook that failed open did not check anything. The report has
            # to say so, or a run of fail-opens reads as a clean observe phase.
            fail_open_summary = {
                "count": int(fail_open_summary["count"]) + 1,
                "latest_hook": _text(record.get("hook")),
                "latest_error": _text(record.get("error")),
                "latest_timestamp": _text(record.get("timestamp")),
            }
        rule = record.get("rule")
        event = record.get("event")
        if not isinstance(rule, str) or not isinstance(event, str):
            continue
        group = rules.setdefault(rule, {"recent": []})
        group[event] = int(group.get(event, 0)) + 1
        if event in ("would_have_blocked", "blocked"):
            claim = record.get("claim", {})
            claim = claim if isinstance(claim, Mapping) else {}
            failed = record.get("failed", [])
            failed = failed if isinstance(failed, list) else []
            group["recent"].append(
                {
                    "timestamp": record.get("timestamp", ""),
                    "event": event,
                    "claim_label": _text(claim.get("label")),
                    "failed": failed,
                }
            )
            group["recent"] = group["recent"][-3:]
    heartbeat_days = 0
    if heartbeats:
        heartbeat_days = (max(heartbeats).date() - min(heartbeats).date()).days
    return {
        "rules": rules,
        "config_degraded": config_degraded,
        "lock_degraded": lock_degraded,
        "fail_open": fail_open_summary,
        "coverage": {
            "heartbeat_days": heartbeat_days,
            "event_count": len(records),
            "hook_last_active": hook_last_active,
        },
    }


def calibration_notice(report: Mapping[str, Any]) -> str:
    """Return the observe-phase conclusion prompt when calibration is mature."""
    rules = report.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    finding_count = 0
    threshold_met = False
    for group in rules.values():
        if not isinstance(group, Mapping):
            continue
        count = group.get("would_have_blocked", 0)
        count = count if isinstance(count, int) else 0
        finding_count += count
        threshold_met = threshold_met or count >= 3
    coverage = report.get("coverage", {})
    coverage = coverage if isinstance(coverage, Mapping) else {}
    days = coverage.get("heartbeat_days", 0)
    days = days if isinstance(days, int) else 0
    if not threshold_met and days < 7:
        return ""
    return (
        f"Axiom's observe phase has accumulated {finding_count} finding(s). "
        "Run /axiom:report to review them, then /axiom:enforce <rule> on "
        "to turn on blocking for a rule."
    )


def manifest(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Any]:
    """Enumerate every runtime path managed by this schema for safe uninstall."""
    paths = state_paths(root=root, cwd=cwd)
    managed = [
        paths["global"],
        paths["ledger"],
        paths["config"],
        paths["lessons"],
        paths["active_claim"],
        paths["claim_lock"],
        paths["stuck_search"],
        # preflight keeps no state since its cooldown was removed; the path
        # stays listed so uninstall still removes the file older versions wrote.
        paths["preflight"],
        paths["config_degraded"],
        paths["adapter_events"],
    ]
    # Quarantined claim slots (`active.json.corrupt-<stamp>`) are ours too;
    # an uninstall that left them behind would not be the zero-residue one
    # the README promises.
    active_path = paths["active_claim"]
    with contextlib.suppress(OSError):
        managed.extend(sorted(active_path.parent.glob(f"{active_path.name}.corrupt-*")))
    return {
        "schema_version": SCHEMA_VERSION,
        "managed_paths": [str(path) for path in managed],
    }


def session_start_main() -> int:
    """Record a heartbeat and emit a calibration notice when one is due."""
    try:
        payload = json.load(sys.stdin)
        payload = payload if isinstance(payload, Mapping) else {}
    except (json.JSONDecodeError, OSError):
        payload = {}
    cwd_value = payload.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
    try:
        paths = ensure_layout(cwd=cwd)
        register_goal_claim(cwd=cwd)
        record_heartbeat(paths["ledger"])
        notice = calibration_notice(get_report_data(paths["ledger"]))
        if notice:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": notice,
                        }
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    except Exception as error:
        return fail_open_main("session_start", None, error, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(session_start_main())
