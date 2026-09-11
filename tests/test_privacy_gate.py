"""Tests for the privacy gate's commit-metadata scan.

A scanner that returns an empty list looks identical whether it is working or
broken, so the central tests here are *positive*: they build a repository whose
history is known to contain exactly the things that have to be caught, and
assert that each one is. A green run against clean history proves nothing on
its own and is only meaningful next to these.
"""

from __future__ import annotations

import importlib.util
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
# Deliberately not an RFC 2606/6761 reserved name: these cases assert that a
# real-looking address IS flagged, which a reserved one no longer would be.
WORK = "someone" + "@" + "employer-corp.co"
OTHER = "dev" + "@" + "another-corp.co"


def _git(repo: Path, *arguments: str, **environment: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "Someone",
        "GIT_AUTHOR_EMAIL": NOREPLY,
        "GIT_COMMITTER_NAME": "Someone",
        "GIT_COMMITTER_EMAIL": NOREPLY,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
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
            with self.subTest(byte=repr(sneaky)):
                with tempfile.TemporaryDirectory() as directory:
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

    def test_malformed_configuration_is_an_error_not_a_silent_pass(self) -> None:
        _commit(self.repo, "x", "bad")
        (self.repo / ".privacy-gate.json").write_text(
            '{"commit_email_allowlist": "not-a-list"}', encoding="utf-8"
        )
        with self.assertRaises(gate.GateError):
            gate.scan_history(self.repo)


if __name__ == "__main__":
    unittest.main()
