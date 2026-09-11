"""Tests for the privacy gate's commit-metadata scan.

A scanner that returns an empty list looks identical whether it is working or
broken, so the central tests here are *positive*: they build a repository whose
history is known to contain exactly the things that have to be caught, and
assert that each one is. A green run against clean history proves nothing on
its own and is only meaningful next to these.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GATE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "precommit-privacy-gate.py"
)
_spec = importlib.util.spec_from_file_location("axiom_privacy_gate", GATE_PATH)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# Addresses are assembled at runtime so no routable-looking literal appears in
# this file. The gate's own content scan reads tracked files, and a scanner that
# has to be waived through on its own test fixtures teaches people to waive it.
NOREPLY = "1+someone" + "@" + "users.noreply.github.com"
# Keep the host PATH: replacing it with POSIX directories means git is simply
# not found on Windows, which this project supports and its CI matrix covers.
# os.devnull for the same reason -- /dev/null does not exist there.
GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
# Deliberately not an RFC 2606/6761 reserved name: these cases assert that a
# real-looking address IS flagged, which a reserved one no longer would be.
WORK = "someone" + "@" + "employer-corp.co"
OTHER = "dev" + "@" + "another-corp.co"


def _git(repo: Path, *arguments: str, **environment: str) -> None:
    env = {
        **GIT_ENV,
        "GIT_AUTHOR_NAME": "Someone",
        "GIT_AUTHOR_EMAIL": NOREPLY,
        "GIT_COMMITTER_NAME": "Someone",
        "GIT_COMMITTER_EMAIL": NOREPLY,
        **environment,
    }
    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _commit(repo: Path, message: str, marker: str, **environment: str) -> None:
    (repo / f"{marker}.txt").write_text(marker, encoding="utf-8")
    _git(repo, "add", "-A", **environment)
    _git(repo, "commit", "-m", message, **environment)


class CommitMetadataScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self._temporary.name)
        _git(self.repo, "init", "-q", "-b", "main")
        self.addCleanup(self._temporary.cleanup)

    def _kinds(self) -> list[str]:
        return [kind for _, _, kind in gate.scan_history(self.repo)]

    def test_clean_history_yields_nothing(self) -> None:
        _commit(self.repo, "feat: add a thing", "clean")
        self.assertEqual(self._kinds(), [])

    def test_attribution_trailer_is_caught(self) -> None:
        """The exact failure this scan exists for: an AI trailer in the message.

        Tracked-file content was clean in the real incident; the attribution
        rode in through commit metadata, where `--scan-all` never looked.
        """
        _commit(
            self.repo,
            "feat: add a thing\n\nCo-Authored-By: Some Bot <bot" + "@" + "example.com>",
            "trailer",
        )
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-trailer") for kind in kinds),
            f"trailer not detected; got {kinds}",
        )

    def test_generated_with_line_is_caught(self) -> None:
        _commit(
            self.repo, "feat: thing\n\nGenerated with [Some Tool](https://x)", "gen"
        )
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-trailer") for kind in kinds),
            f"generated-with line not detected; got {kinds}",
        )

    def test_address_outside_allowlist_is_caught(self) -> None:
        """An employer or personal mailbox published in the history."""
        _commit(
            self.repo,
            "feat: from a work machine",
            "work",
            GIT_AUTHOR_EMAIL=WORK,
            GIT_COMMITTER_EMAIL=WORK,
        )
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-email") for kind in kinds),
            f"non-allowlisted address not detected; got {kinds}",
        )

    def test_finding_names_the_ref_that_reaches_it(self) -> None:
        """A bare SHA cannot be acted on: published or stale local ref?"""
        _commit(self.repo, "x\n\nCo-Authored-By: Bot <b" + "@" + "example.com>", "ref")
        where = [location for location, _, _ in gate.scan_history(self.repo)]
        self.assertTrue(where and "via " in where[0], f"no source ref in {where}")

    def test_allowlist_is_configurable(self) -> None:
        _commit(
            self.repo,
            "feat: thing",
            "cfg",
            GIT_AUTHOR_EMAIL=OTHER,
            GIT_COMMITTER_EMAIL=OTHER,
        )
        self.assertTrue(self._kinds(), "precondition: should flag before configuring")
        (self.repo / ".privacy-gate.json").write_text(
            '{"commit_email_allowlist": ["@another-corp.co"]}', encoding="utf-8"
        )
        self.assertEqual(self._kinds(), [])

    def test_trailer_patterns_are_configurable(self) -> None:
        _commit(self.repo, "x\n\nCo-Authored-By: A Human <" + NOREPLY + ">", "t2")
        self.assertTrue(self._kinds(), "precondition: should flag before configuring")
        (self.repo / ".privacy-gate.json").write_text(
            '{"commit_trailer_patterns": []}', encoding="utf-8"
        )
        self.assertEqual(self._kinds(), [])

    def test_separator_bytes_in_a_message_cannot_hide_a_trailer(self) -> None:
        """Records are NUL-framed because a message can contain anything else.

        With a printable record separator, a message carrying that byte closes
        its own record early and everything after it -- including the trailer
        -- goes unscanned while the run still reports clean.
        """
        for sneaky in ("\x1e", "\x1f"):
            with (
                self.subTest(byte=repr(sneaky)),
                tempfile.TemporaryDirectory() as directory,
            ):
                repo = Path(directory)
                _git(repo, "init", "-q", "-b", "main")
                _commit(
                    repo,
                    "feat: x"
                    + sneaky
                    + "Co-Authored-By: Bot <bot"
                    + "@"
                    + "example.com>",
                    "sneaky",
                )
                kinds = [kind for _, _, kind in gate.scan_history(repo)]
                # Two separate guarantees: the record survived framing (it
                # was scanned at all), and the trailer was not hidden behind
                # a byte that is invisible to a line anchor.
                self.assertTrue(kinds, f"record vanished behind {sneaky!r}")
                self.assertTrue(
                    any(kind.startswith("commit-trailer") for kind in kinds),
                    f"trailer hidden behind {sneaky!r}; got {kinds}",
                )

    def test_lowercase_trailer_key_does_not_evade(self) -> None:
        """Git matches trailer keys case-insensitively; so must this."""
        _commit(
            self.repo, "x\n\nco-authored-by: Bot <b" + "@" + "example.com>", "lower"
        )
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-trailer") for kind in kinds),
            f"lowercase trailer evaded detection; got {kinds}",
        )

    def test_allowlist_matches_the_domain_not_a_substring(self) -> None:
        """`allowed in address` lets a lookalike domain through."""
        lookalike = "attacker" + "@" + "users.noreply.github.com.evil-domain.co"
        _commit(
            self.repo,
            "feat: x",
            "lookalike",
            GIT_AUTHOR_EMAIL=lookalike,
            GIT_COMMITTER_EMAIL=lookalike,
        )
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-email") for kind in kinds),
            f"lookalike domain accepted as allowlisted; got {kinds}",
        )

    def test_real_subdomain_of_an_allowed_domain_is_accepted(self) -> None:
        """The boundary fix must not turn into a blanket rejection."""
        (self.repo / ".privacy-gate.json").write_text(
            '{"commit_email_allowlist": ["corp-example.co"]}', encoding="utf-8"
        )
        inside = "dev" + "@" + "mail.corp-example.co"
        _commit(
            self.repo,
            "feat: x",
            "sub",
            GIT_AUTHOR_EMAIL=inside,
            GIT_COMMITTER_EMAIL=inside,
        )
        self.assertEqual(self._kinds(), [])

    def test_backticked_domain_in_prose_is_not_an_address(self) -> None:
        """Documentation writes `@domain` constantly; it is not a mailbox."""
        self.assertEqual(
            gate.inspect_line(
                "the default allowlist is `" + "@" + "users.noreply.github.com`",
                detect_ip=True,
                hostname_patterns=[],
                denylist=[],
            ),
            [],
        )

    def test_a_real_address_in_content_is_still_caught(self) -> None:
        """The relaxation must not blind the content scan to real addresses."""
        self.assertIn(
            "email",
            gate.inspect_line(
                "contact dev" + "@" + "some-company.co",
                detect_ip=True,
                hostname_patterns=[],
                denylist=[],
            ),
        )

    def test_separator_in_an_author_name_cannot_move_an_address_out_of_view(
        self,
    ) -> None:
        """Field framing is attackable from inside a field.

        A name carrying the field separator shifts every field after it while
        the record still has enough parts to look well-formed, so the real
        address lands in a slot nothing reads as an address.
        """
        hidden = "real" + "@" + "employer-corp.co"
        _commit(
            self.repo,
            "feat: x",
            "shift",
            GIT_AUTHOR_NAME="A\x1fB",
            GIT_AUTHOR_EMAIL=hidden,
            GIT_COMMITTER_EMAIL=hidden,
        )
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-email") for kind in kinds),
            f"address hidden by field shifting; got {kinds}",
        )

    def test_two_at_signs_do_not_borrow_an_allowed_domain(self) -> None:
        """rpartition alone reads the last domain and waves the address through."""
        smuggled = "victim" + "@" + "evil-domain.co" + "@" + "users.noreply.github.com"
        self.assertFalse(
            gate._address_is_allowed(smuggled, gate.DEFAULT_COMMIT_EMAIL_ALLOWLIST)
        )

    def test_legal_punctuation_local_parts_are_still_caught(self) -> None:
        """Plus-addressing and leading underscores are ordinary, not exotic.

        An earlier fix for a markdown false positive demanded an alphanumeric
        first character and lost both of these from the content scan.
        """
        for local in ("+tag", "_svc", "!weird", "a.b"):
            with self.subTest(local=local):
                self.assertIn(
                    "email",
                    gate.inspect_line(
                        "reach me at " + local + "@" + "some-company.co",
                        detect_ip=True,
                        hostname_patterns=[],
                        denylist=[],
                    ),
                    f"{local} lost from the content scan",
                )

    def test_unusual_but_valid_history_does_not_deny_the_gate(self) -> None:
        """Fail-closed must not become a denial of service against real repos.

        Refusing to run is the safe direction only if nothing legitimate
        triggers it; a gate that a normal history can switch off is worse than
        one that reports.
        """
        _commit(self.repo, "first", "one")
        _git(
            self.repo,
            "commit",
            "-q",
            "--allow-empty",
            "--allow-empty-message",
            "-m",
            "",
        )
        _git(self.repo, "checkout", "-q", "-b", "side")
        _commit(self.repo, "side work", "side")
        _git(self.repo, "checkout", "-q", "main")
        _commit(self.repo, "main work", "mainline")
        _git(self.repo, "merge", "-q", "--no-ff", "side", "-m", "merge branches")
        self.assertEqual(self._kinds(), [])

    def test_two_at_signs_in_a_real_commit_are_caught_end_to_end(self) -> None:
        """The helper being right is not the same as the scan calling it right.

        Extracting address-shaped tokens from text hands over a substring:
        `victim@evil@allowed` yields `evil@allowed`, which IS allowlisted. The
        whole field value has to reach the check.
        """
        smuggled = "victim" + "@" + "evil-domain.co" + "@" + "users.noreply.github.com"
        _commit(
            self.repo,
            "feat: x",
            "twoat",
            GIT_AUTHOR_EMAIL=smuggled,
            GIT_COMMITTER_EMAIL=smuggled,
        )
        kinds = self._kinds()
        # Reporting *something* is not enough and does not discriminate: a text
        # extractor also reports here, because the first substring it pulls out
        # happens to be un-allowlisted too. What must be true is that the whole
        # field value reached the check, so the finding names the whole thing.
        self.assertTrue(
            any(smuggled in kind for kind in kinds),
            f"finding names a substring, not the field value; got {kinds}",
        )

    def test_dotless_domain_in_a_real_commit_is_not_invisible(self) -> None:
        """A domain with no dot produces no token for a text extractor."""
        dotless = "secret" + "@" + "internal-host"
        _commit(
            self.repo,
            "feat: x",
            "dotless",
            GIT_AUTHOR_EMAIL=dotless,
            GIT_COMMITTER_EMAIL=dotless,
        )
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-email") for kind in kinds),
            f"dotless domain never checked; got {kinds}",
        )

    def test_backtick_led_local_part_is_a_real_address(self) -> None:
        """Backtick is legal in a local part; only a lone backtick is markdown."""
        self.assertIn(
            "email",
            gate.inspect_line(
                "reach me at `svc" + "@" + "some-company.co",
                detect_ip=True,
                hostname_patterns=[],
                denylist=[],
            ),
        )
        self.assertEqual(
            gate.inspect_line(
                "the default is `" + "@" + "users.noreply.github.com`",
                detect_ip=True,
                hostname_patterns=[],
                denylist=[],
            ),
            [],
        )

    def test_unencodable_domain_does_not_match_itself(self) -> None:
        """Returning a malformed domain as written lets it be its own allowlist."""
        bad = "a" * 70 + ".co"
        self.assertEqual(gate._normalised_domain(bad), "")
        self.assertFalse(gate._address_is_allowed("x" + "@" + bad, [bad]))

    def test_denylist_literal_in_an_author_name_is_still_found(self) -> None:
        """Names are metadata too, and an earlier revision stopped reading them."""
        (self.repo / ".privacy-denylist").write_text(
            "secret-project\n", encoding="utf-8"
        )
        _commit(self.repo, "feat: x", "name", GIT_AUTHOR_NAME="secret-project")
        self.assertIn("denylist-literal", self._kinds())

    def test_a_repository_with_no_commits_is_not_an_error(self) -> None:
        """Fail-closed must not fire on a history that is legitimately empty."""
        with tempfile.TemporaryDirectory() as directory:
            fresh = Path(directory)
            _git(fresh, "init", "-q", "-b", "main")
            self.assertEqual(gate.scan_history(fresh), [])

    def test_nul_in_a_commit_message_cannot_hide_a_trailer(self) -> None:
        """Any sentinel a message can contain is a sentinel a message can break.

        Porcelain will not build this -- `commit-tree` answers "a NUL byte in
        commit log message not allowed" -- but `hash-object --literally` will,
        so a reachable commit carrying the byte is constructible and the scan
        has to survive it. Message bodies are therefore read through a
        length-delimited channel rather than a framed one.
        """
        _commit(self.repo, "base", "base")
        env = dict(GIT_ENV)
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()
        stamp = "1789000000 +0000"
        raw = (
            f"tree {tree}\n"
            f"author Someone <{NOREPLY}> {stamp}\n"
            f"committer Someone <{NOREPLY}> {stamp}\n"
            "\nsubject\x00\nCo-Authored-By: Bot <bot" + "@" + "example.com>\n"
        ).encode("utf-8")
        made = (
            subprocess.run(
                ["git", "hash-object", "-w", "-t", "commit", "--stdin", "--literally"],
                cwd=self.repo,
                input=raw,
                capture_output=True,
                check=True,
                env=env,
            )
            .stdout.decode()
            .strip()
        )
        _git(self.repo, "update-ref", "refs/heads/crafted", made)
        kinds = self._kinds()
        self.assertTrue(
            any(kind.startswith("commit-trailer") for kind in kinds),
            f"trailer hidden behind a NUL in the message; got {kinds}",
        )

    def test_a_truncated_object_stream_is_an_error_not_a_clean_scan(self) -> None:
        """The central fail-closed promise, tested at the parser.

        A stream that stops mid-object must raise, not scan the fragment and
        record it as that commit's message.
        """
        _commit(self.repo, "x", "trunc")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env=GIT_ENV,
        ).stdout.strip()

        real = gate._bounded_git_output
        for keep in (0.5, 0.0):
            with self.subTest(keep=keep):
                gate._bounded_git_output = lambda *a, _k=keep, **kw: real(*a, **kw)[
                    : int(len(real(*a, **kw)) * _k)
                ]
                try:
                    with self.assertRaises(gate.GateError):
                        gate._commit_messages([sha], self.repo)
                finally:
                    gate._bounded_git_output = real

    def test_trailing_data_after_the_last_object_is_rejected(self) -> None:
        _commit(self.repo, "x", "trail")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env=GIT_ENV,
        ).stdout.strip()
        real = gate._bounded_git_output
        gate._bounded_git_output = lambda *a, **kw: real(*a, **kw) + b"leftover\n"
        try:
            with self.assertRaises(gate.GateError):
                gate._commit_messages([sha], self.repo)
        finally:
            gate._bounded_git_output = real

    def test_two_encoding_headers_are_refused_rather_than_guessed(self) -> None:
        """If git and this scan resolve the ambiguity differently, they disagree
        about what the message even says."""
        body = (
            b"tree x\nauthor a\ncommitter c\n"
            b"encoding ISO-8859-1\nencoding UTF-8\n\nmessage\n"
        )
        with self.assertRaises(gate.GateError):
            gate._decode_commit_message(body, "a" * 40)

    def test_a_non_utf8_message_is_decoded_as_declared_not_mangled(self) -> None:
        """Replacement characters silently rewrite a denylist literal out of range."""
        _commit(self.repo, "base", "enc")
        env = dict(GIT_ENV)
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()
        stamp = "1789000000 +0000"
        secret = "Jos\u00e9-project"
        raw = (
            (
                f"tree {tree}\n"
                f"author Someone <{NOREPLY}> {stamp}\n"
                f"committer Someone <{NOREPLY}> {stamp}\n"
                "encoding ISO-8859-1\n"
                "\n"
            ).encode("ascii")
            + secret.encode("iso-8859-1")
            + b"\n"
        )
        made = (
            subprocess.run(
                ["git", "hash-object", "-w", "-t", "commit", "--stdin", "--literally"],
                cwd=self.repo,
                input=raw,
                capture_output=True,
                check=True,
                env=env,
            )
            .stdout.decode()
            .strip()
        )
        _git(self.repo, "update-ref", "refs/heads/encoded", made)
        (self.repo / ".privacy-denylist").write_text(secret + "\n", encoding="utf-8")
        self.assertIn(
            "denylist-literal",
            self._kinds(),
            "a non-UTF-8 message decoded to replacement characters and the "
            "denylist literal stopped matching",
        )

    def test_a_failing_git_command_is_an_error_not_an_empty_scan(self) -> None:
        """Only a zero return code is success.

        Accepting the kill signal as success also accepts a process that
        something else ended after it had written well-formed output -- a
        partial scan that parses.
        """
        with self.assertRaises(gate.GateError):
            gate._bounded_git_output(
                ["log", "--all", "--format=%H", "no-such-ref-anywhere"],
                cwd=self.repo,
                budget=gate.MAX_GIT_OUTPUT_BYTES,
            )

    def test_the_budget_is_refused_rather_than_ignored(self) -> None:
        """Pins that the budget is honoured at all.

        It does not prove the *timing* -- on an output this small, a reader
        that captured everything first and compared afterwards would raise
        here too. Proving the read stops early needs an output large enough to
        matter, which is not a thing to build into a unit test. The timing
        rests on the reader's structure, which counts per chunk and kills the
        child before appending past the budget.
        """
        _commit(self.repo, "x", "budget")
        with self.assertRaises(gate.GateError) as caught:
            gate._bounded_git_output(
                ["log", "--all", "--format=%H"], cwd=self.repo, budget=5
            )
        self.assertIn("exceeded", str(caught.exception))

    def test_an_empty_encoding_name_is_refused(self) -> None:
        body = b"tree x\nauthor a\ncommitter c\nencoding \n\nmessage\n"
        with self.assertRaises(gate.GateError):
            gate._decode_commit_message(body, "b" * 40)

    def test_a_replaced_object_does_not_hide_the_real_one(self) -> None:
        """The worst shape of failure this gate can have: a false green.

        `git replace <dirty> <clean>` makes every ordinary local read show the
        clean object, but refs/replace/* is not pushed by default -- so the
        remote keeps the original. A scan that honours replacements reports
        clean about a history that was never published, which is worse than
        having no scan at all.
        """
        dirty = "someone" + "@" + "employer-corp.co"
        _commit(
            self.repo,
            "feat: from a work machine",
            "dirty",
            GIT_AUTHOR_EMAIL=dirty,
            GIT_COMMITTER_EMAIL=dirty,
        )
        dirty_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env=GIT_ENV,
        ).stdout.strip()
        _commit(self.repo, "feat: clean", "clean")
        clean_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env=GIT_ENV,
        ).stdout.strip()

        _git(self.repo, "replace", "-f", dirty_sha, clean_sha)
        # Sanity: the replacement really is in effect for an ordinary read.
        shown = subprocess.run(
            ["git", "log", "-1", "--format=%ae", dirty_sha],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env=GIT_ENV,
        ).stdout.strip()
        self.assertNotEqual(shown, dirty, "precondition: replacement not active")

        kinds = self._kinds()
        self.assertTrue(
            any(dirty in kind for kind in kinds),
            f"a replaced object hid the address that will actually be pushed; got {kinds}",
        )

    def test_a_shallow_clone_is_refused_not_certified(self) -> None:
        """History past the shallow boundary is unread, and still on the remote."""
        _commit(self.repo, "old: carries something", "old")
        _commit(self.repo, "new", "new")
        with tempfile.TemporaryDirectory() as directory:
            shallow = Path(directory) / "shallow"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--no-local",
                    self.repo.as_uri(),
                    str(shallow),
                ],
                capture_output=True,
                check=True,
                env=GIT_ENV,
            )
            with self.assertRaises(gate.GateError) as caught:
                gate.scan_history(shallow)
            self.assertIn("shallow", str(caught.exception))

    def test_a_grafts_file_is_refused_not_certified(self) -> None:
        """Grafts rewrite parentage for local reads only."""
        _commit(self.repo, "x", "graft")
        info = self.repo / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "grafts").write_text("\n", encoding="utf-8")
        with self.assertRaises(gate.GateError) as caught:
            gate.scan_history(self.repo)
        self.assertIn("grafts", str(caught.exception))

    def test_malformed_configuration_is_an_error_not_a_silent_pass(self) -> None:
        _commit(self.repo, "x", "bad")
        (self.repo / ".privacy-gate.json").write_text(
            '{"commit_email_allowlist": "not-a-list"}', encoding="utf-8"
        )
        with self.assertRaises(gate.GateError):
            gate.scan_history(self.repo)


if __name__ == "__main__":
    unittest.main()
