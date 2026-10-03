# History scan: the false greens

A false green is the worst answer a gate can give: it reports clean about
content that is published, or is about to be. This page lists the classes of
false green found while the privacy gate was extended from tracked files to
all reachable history (`--scan-history`, `--scan-blobs`, `--scan-all`): what
each one is, the regression test that holds it shut, and, for nine of the
fifteen, a minimal reproduction in ordinary git.

It is a catalogue of our own misses, not a claim of completeness. The scope
statement is in [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md): everything
reachable through ordinary git is meant to be covered; hand-written objects are
bounded by refusing what cannot be read, not by a promise to detect everything
inside one; unreachable objects are out of scope.

## How these were found

The history scan went through 26 rounds of adversarial review by a model from
a different family than the one that wrote it. No round came back empty. That
is checkable against the repository rather than against this page: each round
has its own commit, and the message says which round it answers and what was
found. (Round 1 reviewed the guard-paths work in the same pass, and at least
two of its six findings belong there rather than to the scan.)

Counted from those messages:

- **About 50 findings** across the 26 rounds. Twenty of the messages state a
  count or speak of "the finding" in the singular, and those add up to 41.
  Six (rounds 5, 7, 16, 21, 22 and 23) state none; the rest of the estimate
  is read off what those commits fixed. One round's message records a finding
  that turned out not to be real. So this is a reconstruction from commit
  messages, not a ledger.
- **Fifteen classes**, below. The grouping is ours. Counted as individual
  findings instead, the same material is roughly 25: the commit messages keep
  a running count for one class — "content that is published but never read"
  — and reach "thirteenth" by round 19, with more after it.
- **Three were regressions introduced by the previous round's fix**, each
  admitted in the commit that repaired it: requiring an address's local part
  to start with an alphanumeric (to silence a markdown false positive) blinded
  the content scan to `+tag@` and `_svc@` addresses; extracting address-shaped
  tokens from joined text made an address at a dotless host invisible; and
  trimming a format string dropped author and committer names, and with them
  the denylist check over names.

A sequence that never reached zero is evidence that the next round would have
found something too. Read the list with that in mind.

## Reproducing

Exit codes: `0` clean, `1` findings, `2` the scan refused to certify.

The setup below is shared by every reproduction on this page. Addresses are
assembled from variables so that this page passes the gate it documents; `BAD`
stands in for any domain that must not be published.

```sh
GATE=/path/to/axiom/scripts/precommit-privacy-gate.py
WORK=$(mktemp -d)
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
NOREPLY=users.noreply.github.com   # the default allowlist
BAD=employer-corp.co
export GIT_AUTHOR_NAME=Someone GIT_COMMITTER_NAME=Someone
export GIT_AUTHOR_EMAIL="1+someone@$NOREPLY" GIT_COMMITTER_EMAIL="1+someone@$NOREPLY"
c() { echo "$2" > "$2.txt"; git add -A; git commit -q -m "$1"; }   # c <message> <marker>
new() { cd "$WORK" && mkdir "$1" && cd "$1" && git init -q -b main; }
```

Nine classes have a shell reproduction below (1, 2, 3, 5, 6, 7, 13, 14, 15).
Those commands were run as written under `sh`, `bash` and `zsh` with git
2.54.0 and Python 3.14 on macOS; class 7 also needs `ssh-keygen`.

For classes 5, 6 and 7 the regression tests build their objects with
`git hash-object --literally`, but ordinary git writes the same shapes, and
the reproductions take that route. Each of the three was also run once
against the gate as it stood just before its fix, and that version exits 0.
Classes 8–12 are exercised by tests that write objects by hand or call the
parser directly, and class 4 can only be simulated at the parser. For those,
the reproduction is the named test:

```sh
python3 -m unittest tests.test_privacy_gate -k <test name>
```

## The fifteen classes

### 1. Replacement objects

`git replace <dirty> <clean>` makes every ordinary local read show the clean
object, but `refs/replace/*` is not pushed by default, so the remote keeps the
original; a scan that honours replacements reports clean about a history that
was never published.

- Closed by: every read runs with replacement objects disabled.
- Test: `test_a_replaced_object_does_not_hide_the_real_one`

```sh
new replace
(GIT_AUTHOR_EMAIL="someone@$BAD"; GIT_COMMITTER_EMAIL="someone@$BAD"; c "feat: from a work machine" dirty)
DIRTY=$(git rev-parse HEAD)
c "feat: clean" clean
git replace -f "$DIRTY" "$(git rev-parse HEAD)"
git log -1 --format=%ae "$DIRTY"     # prints the clean address
python3 "$GATE" --scan-history       # exit 1: reports the dirty one
```

### 2. Shallow clones

`git log --all` in a shallow clone stops at the shallow boundary and exits
successfully; a scan that sees only the commits inside the boundary reports
clean while the older ancestors are still on the remote.

- Closed by: refusing to run in a shallow repository. The cost is real: CI
  that clones shallow by default cannot run the history scan until it fetches
  full depth. That trade is recorded in [ROADMAP.md](ROADMAP.md).
- Test: `test_a_shallow_clone_is_refused_not_certified`

```sh
new deep
c "old: carries something" old; c new new
git clone -q --depth 1 --no-local "file://$PWD" ../shallow && cd ../shallow
python3 "$GATE" --scan-history       # exit 2: shallow clone, fetch --unshallow first
```

### 3. Grafts, including from a linked worktree

A grafts file rewrites parentage for local reads only, and disabling
replacement objects does not cover it; in a linked worktree, building the path
from the worktree's own git directory looks in the wrong place (grafts live in
the common directory), so a grafted history was scanned as it appeared locally
and reported clean.

- Closed by: refusing when a grafts file exists, and asking git where that
  file is instead of joining a path.
- Tests: `test_a_grafts_file_is_refused_not_certified`,
  `test_grafts_are_found_from_inside_a_linked_worktree`

```sh
new grafted
c x wt
mkdir -p .git/info; printf '\n' > .git/info/grafts
git worktree add -q ../linked -b wt-branch && cd ../linked
python3 "$GATE" --scan-history       # exit 2: grafts file
```

Git marks grafts as deprecated and prints a hint; a future git may remove them.

### 4. Truncated object stream

The reader that takes objects by their declared length did not actually hold
the stream to that length: when it ended mid-object, the fragment was scanned
as that commit's message and recorded as clean.

- Closed by: validating object name order, type, length, terminator and the
  absence of trailing data; a stream truncated to nothing is refused too.
- Tests: `test_a_truncated_object_stream_is_an_error_not_a_clean_scan`,
  `test_trailing_data_after_the_last_object_is_rejected`
- Reproduction: none at the git command level. The tests cut the reader's
  input to half, cut it to nothing, or append to it, and assert a refusal.

### 5. Identity decoding

Author and committer names are raw bytes with no declared encoding; decoding
them as UTF-8 with replacement characters rewrites a non-UTF-8 name, the
denylist literal no longer matches it, and the scan reports clean.

- Closed by: comparing denylist literals against the raw bytes in several
  candidate encodings — and, because that list is finite, refusing when
  nothing matched, the bytes are not valid UTF-8, and a denylist is
  configured.
- Tests: `test_a_latin1_author_name_still_matches_the_denylist`,
  `test_an_unreadable_identity_is_refused_when_a_denylist_exists`,
  `test_an_unreadable_identity_is_fine_with_no_denylist`

```sh
new latin1
git config i18n.commitEncoding ISO-8859-1
printf 'Jos\303\251-project\n' > .privacy-denylist   # the literal, in UTF-8
NAME=$(printf 'Jos\351-project')                     # the same name, in ISO-8859-1
(GIT_AUTHOR_NAME="$NAME"; GIT_COMMITTER_NAME="$NAME"; c "feat: x" latin)
python3 "$GATE" --scan-history       # exit 1: denylist-literal
```

The tests write this commit by hand, with no `encoding` header. Ordinary git
puts the same raw bytes in the ident lines once `i18n.commitEncoding` is set;
without that setting it converts the name to UTF-8 itself and prints a
warning.

### 6. Message encoding

Two cases under one heading. The message body was force-decoded as UTF-8 with
replacement, so a non-ASCII denylist literal in an ISO-8859-1 message was
rewritten and missed. And a commit object declaring two different `encoding`
headers was read with the last one, silently, so the scan could be reading a
different message than another reader sees.

- Closed by: decoding strictly in the encoding the object declares and
  refusing if it will not decode; two `encoding` headers, or an empty encoding
  name, are refused rather than guessed.
- Tests: `test_a_non_utf8_message_is_decoded_as_declared_not_mangled`,
  `test_two_encoding_headers_are_refused_rather_than_guessed`,
  `test_an_empty_encoding_name_is_refused`

The first case, in ordinary git (the second needs a hand-written object; the
tests cover it):

```sh
new latin1msg
git config i18n.commitEncoding ISO-8859-1
printf 'Jos\303\251-project\n' > .privacy-denylist
c "$(printf 'feat: for Jos\351-project')" msg
python3 "$GATE" --scan-history       # exit 1: denylist-literal
```

This is the loosest of the fifteen: our own grouping named it without defining
it, so both cases the fixes cover are listed.

### 7. Headers that were never scanned

A commit object has headers beyond the ident lines and the message; a
`mergetag` header embeds an entire tag object — tagger identity and tag
message — and it is published with the commit, while the scan read only the
author, the committer and the body.

- Closed by: checking the header block for addresses, matching denylist
  literals against the whole object, applying the same "unreadable means
  refuse" rule to headers, and searching them for attribution trailers.
- Tests: `test_a_mergetag_header_is_scanned_too`,
  `test_an_unreadable_header_block_is_also_refused`,
  `test_a_trailer_inside_a_mergetag_is_found`

The tests write the `mergetag` commit by hand. Ordinary git writes one when
it merges a signed tag, and the header stays in the merge commit after the
tag itself is deleted:

```sh
new mergetag
ssh-keygen -q -t ed25519 -N '' -f "$WORK/key"        # throwaway signing key
c base mtbase; git checkout -q -b side; c "side work" side
GIT_COMMITTER_EMAIL="tagger@$BAD" git -c gpg.format=ssh \
  -c user.signingkey="$WORK/key" tag -s v1 -m release
git checkout -q main; c "main work" mainwork
git merge -q --no-edit v1            # cannot verify the signature; merges anyway
git tag -d v1                        # the tag object stays, inside the merge commit
python3 "$GATE" --scan-history       # exit 1: commit ... commit-email in metadata
```

### 8. Embedded ident lines matched as text

Ident lines inside an embedded object (a `mergetag`'s tagger) were handed to
the text pattern instead of the structural check, so bypasses already closed
for the commit's own ident lines reappeared one level down: a dotless domain
the pattern cannot see, two `@` signs borrowing an allowlisted domain, a decoy
bracket pair ahead of the real one, and an unclosed bracket that was skipped
without a word.

- Closed by: parsing embedded ident lines as ident lines; every bracketed
  token is checked, and a line that will not parse is refused.
- Tests: `test_a_dotless_tagger_address_in_a_mergetag_is_caught`,
  `test_a_two_at_tagger_address_does_not_borrow_an_allowed_domain`,
  `test_a_decoy_bracket_does_not_hide_the_real_tagger_address`,
  `test_an_unclosed_bracket_is_refused_not_skipped`
- Reproduction: the tests (hand-written objects). Two of the four shapes do
  not need one. Put a dotless or a two-`@` address in `GIT_COMMITTER_EMAIL`
  in the class 7 reproduction and the tagger line inside the `mergetag`
  carries it; each was checked once that way and exits 1. The decoy and
  unclosed-bracket shapes do need a hand-written object, because git strips
  angle brackets out of a name.

### 9. A control byte inside the keyword

A control byte inside an ident keyword (`tag<US>ger`) stopped the line being
recognised as an ident line, so the whole line — and the address on it — was
skipped.

- Closed by: refusing header lines that carry bytes git does not write.
- Test: `test_a_control_byte_inside_an_ident_keyword_is_refused`
- Reproduction: the test (hand-written object); the scan exits 2.

### 10. Tab and carriage return

The fix for class 9 spared tab and carriage return because they are ordinary
in prose, so `tag<TAB>ger` and `tag<CR>ger` still went unrecognised and the
line was still skipped.

- Closed by: validating headers against git's header grammar (keyword, space,
  value; continuation lines begin with a space) instead of listing bad bytes,
  which also refuses high bytes outside the C0 range.
- Test: `test_any_control_byte_inside_an_ident_keyword_is_refused`
  (parameterised over unit separator, tab, carriage return and a high byte)
- Reproduction: the test (hand-written objects).

### 11. The keyword whitelist

Grammar validation constrains the shape of a line; which lines had their
addresses checked was still a whitelist of three keywords, so a well-formed
header such as `x-identity Name <address>`, with a real address in the
brackets, is legal, is not an
ident line, and passed with its address unread.

- Closed by: every bracketed token in every reachable header goes through the
  structural comparison, recursing through embedded objects under a depth
  bound.
- Test: `test_an_unknown_keyword_carrying_an_address_is_still_checked`
- Reproduction: the test (direct parser call).

### 12. Bare addresses

Angle brackets are a convention of ident lines, not a requirement anywhere
else; a bare address in a header value is published just the same, and a
dotless domain keeps it away from the text pattern, so extracting only
bracketed tokens missed it.

- Closed by: checking every token that contains an `@`, delimited by
  whitespace or brackets.
- Test: `test_a_bare_address_in_a_header_is_checked`
- Reproduction: the test (direct parser call). Ordinary git can be made to
  write a bare address into a header — a nonsense `i18n.commitEncoding` value
  is copied into the `encoding` header as given — but the scan refuses that
  object (exit 2, checked once) because the message will not decode as
  declared, so the bare-address check itself is reached only in the test.

### 13. The reserved-domain exemption ran first

The exemption for reserved documentation domains (`example.com` and the like)
ran before the structural check, so a value with a live mailbox in front and a
reserved name on the end matched the exemption and never reached the question
of whether it was one address. Ordinary porcelain will set such a value as the
author address.

- Closed by: asking about the exemption only for a value that parses as a
  single address, and only about that address's one domain.
- Tests:
  `test_a_reserved_domain_on_the_end_does_not_exempt_a_multi_at_address`,
  `test_a_genuine_reserved_address_is_still_exempt`

```sh
new reserved
(GIT_AUTHOR_EMAIL="secret@$BAD@example.com"; GIT_COMMITTER_EMAIL="secret@$BAD@example.com"; c "feat: x" reserved)
python3 "$GATE" --scan-history       # exit 1: the finding names the whole field value
```

### 14. Annotated tags

Walking commits never arrives at a tag object, only at the commit it points
to; pushing a tag made with `git tag -a` publishes the tagger address and the
tag message. Later rounds found the rest of the family: a tag pointing at a
tag whose inner ref was then deleted, a tag chain longer than the depth bound
where the walk stopped without saying so, and a tag object held by a ref
outside `refs/tags/`.

- Closed by: tags go through the same function as commits; tag chains are
  followed; a chain past the bound is refused rather than truncated; every ref
  is examined, not only `refs/tags/`.
- Tests: `test_an_annotated_tag_is_scanned`,
  `test_a_lightweight_tag_adds_nothing_to_scan`,
  `test_a_nested_tag_is_followed`,
  `test_a_tag_chain_past_the_bound_is_refused_not_truncated`,
  `test_a_tag_outside_refs_tags_is_still_scanned`

```sh
new tagged
c "clean commit" tagbase
GIT_COMMITTER_EMAIL="tagger@$BAD" git tag -a v1 -m release
python3 "$GATE" --scan-history       # exit 1: tag refs/tags/v1 ... tag-email
git update-ref refs/archive/release refs/tags/v1; git tag -d v1
python3 "$GATE" --scan-history       # exit 1: tag refs/archive/release ... tag-email
```

### 15. Reachable blob content

Commit a file holding a secret, delete it, commit again: the working tree is
clean, the metadata is clean, and the blob is still reachable and still
published by the next push — while the scan read only tracked files and
metadata. The same family: a `git notes` body is a blob; a text file with one
invalid byte was skipped whole as binary; and in a repository created with
`--object-format=sha256` object names are 64 hex characters, a pattern that
knew only 40 matched nothing, the object list came back empty, and empty read
as clean.

- Closed by: `--scan-blobs` reads every blob reachable from any ref; binary
  now means a NUL byte near the start, not "does not decode"; object names of
  both lengths are recognised.
- Tests: `test_a_deleted_file_is_still_in_the_history`,
  `test_a_note_body_is_scanned`,
  `test_a_stray_byte_does_not_make_a_text_file_unscannable`,
  `test_a_real_binary_blob_is_skipped`,
  `test_a_sha256_repository_is_not_read_as_empty`

```sh
new blobs      # sha256 variant: replace this line with
               #   cd "$WORK" && mkdir b256 && cd b256 && git init -q -b main --object-format=sha256
c base blobbase
echo "SECRET=leaked@$BAD" > s.env; git add -A; git commit -q -m "add secret"
git rm -q s.env; git commit -q -m "remove secret"
python3 "$GATE" --scan-history       # exit 0: the metadata really is clean
python3 "$GATE" --scan-blobs         # exit 1: object ... (s.env):1: email
```

## Earlier findings of the same kind

The fifteen classes above do not include several defects from the first seven
rounds in which the scan also reported clean. The round 1 message says so in
those words; for the later rounds it is our reading of what the commit fixed.
They belong on this page for the same reason; each is held by a test.

| What reported clean | Test |
|---|---|
| A record separator inside a commit message ended the record early, so what followed it — trailers included — was never scanned | `test_separator_bytes_in_a_message_cannot_hide_a_trailer` |
| The address allowlist matched by substring, so an address at a longer domain that merely contained an allowed one passed | `test_allowlist_matches_the_domain_not_a_substring` |
| Trailer keys were matched case-sensitively; a lowercase key passed | `test_lowercase_trailer_key_does_not_evade` |
| A field separator inside an author name shifted every later field, and the real address landed in a slot nothing read as an address | `test_separator_in_an_author_name_cannot_move_an_address_out_of_view` |
| Two `@` signs borrowed an allowlisted domain from the tail of the value | `test_two_at_signs_do_not_borrow_an_allowed_domain` |
| An address at a dotless domain produced no token for the text extractor (introduced by the previous round's fix) | `test_dotless_domain_in_a_real_commit_is_not_invisible` |
| A NUL in a commit message broke the sentinel framing | `test_nul_in_a_commit_message_cannot_hide_a_trailer` |
| A killed git process was treated as a successful one, so a well-formed half of a scan read as the whole | `test_a_failing_git_command_is_an_error_not_an_empty_scan` |

## What this page does not claim

- **Completeness.** Twenty-six rounds, none empty. The honest reading is that
  the list is as long as the review was, not as long as the problem is.
- **Detection inside hand-written objects.** Some shapes above are known to
  us only as hand-written objects: the two-header case in class 6, the
  bracket shapes in class 8, and classes 9–11. We have not found a way to
  produce them with ordinary git, which is not proof that there is none. For
  objects like those the guarantee is refusal of what cannot be read, as
  KNOWN-LIMITATIONS states, not detection of everything a hand-built object
  can carry. Classes 5 and 7, the first case of class 6 and the two address
  shapes in class 8 are not in that group, even though their tests build the
  objects by hand: ordinary git writes them, so they are on the side the scan
  is meant to detect.
- **Portability of the reproductions.** They were run on one platform and one
  git version. Grafts in particular are deprecated and may stop reproducing.
- **Anything about unreachable objects.** After a history rewrite the old
  commits stay fetchable by name until the host collects them; no local scan
  sees or changes that.
