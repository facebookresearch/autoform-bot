"""Read-backs: blind testimony about what a skeleton literally asserts.

A skeleton tells a reviewer what to read. A read-back tells them what it says,
in mathematical English, written by someone who was shown only the Lean and
never the article, the source, or the author's intent. The reviewer then
compares the read-back with the source statement; every gap between the two is
exactly what they are looking for. The practice follows the read-back audits
of the Prove2me platform.

Read-backs live in the vault under ``blueprint/readbacks/<article_id>/<encoded
Lean name>.md``. Each file is a self-contained review card: the exact skeleton the
auditor was shown, in a Lean block, followed by the testimony in a text block
under a ``## Read-back`` heading, so a reviewer working in the vault or on
GitHub sees the Lean and the testimony side by side without the site. They are
testimony, not derived state, so they are committed with the book, and each one
records the evidence hash of the packet text it was written from, and the
declaration's statement hash then. When the packet no longer matches a fresh
extraction's, the card is stale: a check that compares them reports it, and the
site shows it marked invalid, rather than presenting it as current. The
statement hash is a record only: it covers the Lean version, which a bump can
change while the packet the auditor read stays the same.

Testimony is plain text with TeX formulas between dollar signs, which
:mod:`autoform_cli.testimony` checks. No file under ``readbacks/`` is copied to
the site. Loading accepts code points this Python's Unicode data leaves
unassigned, which a write refuses, so a filed card loads the same under every
Python. The user-facing rules are in the CLI reference's read-back cards
section and in ``skills/human-review/references/readback.md``; the mechanics
below are what they leave out.

Paths. The durable ``article_id`` keeps testimony attached when an article
moves. The encoded filename avoids platform-specific Lean-name collisions, and
before its first dot never spells a name Windows keeps for a device, as
``Aux.lemma`` would.

Loading. No card file is read more than one byte past its 4 MiB limit, and a
card's frontmatter is read from its first 32 lines. A card is attributed to the
declaration its path names, even when that name is too long for the filename to
spell, so a card that records another declaration, or whose ``.md`` is in
another case, is reported as that declaration's invalid card.

Writing. Writes name a card by the hash of its bytes, so a card that is not
UTF-8 is replaced like any other. Filing content identical to the current card
writes nothing in its directory, so it succeeds even in a read-only one. The
blueprint ``.gitignore`` that ``autoform init`` writes for a new project
ignores the ``.autoform-readback-*.tmp`` staging file. A crash just after a
write can lose the card; filing it again restores it. The lock and
compare-and-swap cover Autoform's writers only: an editor's save, or a
directory moved, during a write is not detected. Git for Windows checks text
out with CRLF line ends by default, hence the ``* -text`` ``.gitattributes``.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping
from urllib.parse import unquote_to_bytes

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows, which cannot publish cards
    fcntl = None  # type: ignore[assignment]

from .graph import ARTICLE_ID_PATTERN
from .markdown import blueprint_root
from .skeleton import declaration_filename, evidence_hash_of
from .testimony import MAX_NAMED, baseless_mark, errors_in_testimony, hidden_characters, overstacked, quoted

READBACKS_DIR = "readbacks"
READBACK_HEADING = "## Read-back"
READBACK_SCHEMA = "autoform-readback/v1"
SKELETON_HEADING = "## Skeleton"
_FRONTMATTER_FIELDS = frozenset({"schema", "article_id", "declaration", "statement", "packet", "model"})
_QUOTED_FRONTMATTER_FIELDS = frozenset({"article_id", "declaration", "model"})
#: The only hash form a card may record: what `autoform skeleton` prints.
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ALTERED_PACKET = "the displayed skeleton does not match the recorded packet hash"
#: What the read-back folder's ``.gitattributes`` says: no line-end conversion.
_GITATTRIBUTES = b"* -text\n"
#: Staging files beside a card start with this and never end in ``.md``, so
#: the loader never takes one for a card.
_WORK_PREFIX = ".autoform-readback-"
#: How long a publication waits for another one in the same card directory.
_LOCK_TIMEOUT = 10.0
#: The most bytes a card file may hold. Testimony is limited to 32 KiB, and the
#: packet a card shows has no limit of its own, so this leaves it room for
#: large kernel material. No card larger than this is built, and no file is
#: read more than one byte past it: that byte marks a larger one as an invalid
#: card, whose bytes are never kept.
_CARD_MAX_BYTES = 4 * 1024 * 1024
#: The only errors opening a card that mean there is no card there to read:
#: a link, nothing, or a file where a directory should be. Any other error
#: leaves a card unread, so it is reported rather than skipped.
_NO_CARD_ERRNOS = frozenset({errno.ELOOP, errno.ENOENT, errno.ENOTDIR})


@dataclass(frozen=True, slots=True)
class Readback:
    """One read-back file, parsed."""

    article_id: str
    declaration: str
    #: The statement hash of the declaration the auditor read about, which a
    #: proof edit leaves unchanged, if the card records it. A record only: it
    #: covers the Lean version, which a bump can change under the same packet.
    statement_hash: str | None
    #: The evidence hash of the packet the auditor read, if the card records it.
    packet_hash: str | None
    model: str | None
    #: The testimony alone, without the skeleton block the file also carries.
    text: str
    path: Path
    #: The evidence hash of the packet the card *shows*, which a reviewer reads
    #: beside the testimony. ``None`` when the card displays no packet.
    shown_hash: str | None = None
    #: The exact packet text recovered from the fenced block, including its
    #: terminal newline. This is the evidence the auditor actually saw.
    shown_text: str | None = None
    #: Hash of the complete on-disk card, used for compare-and-swap updates.
    file_hash: str | None = None
    #: Intrinsic format and identity failures. A malformed card is evidence for
    #: nothing, even when one of its hashes happens to match a declaration.
    validation_errors: tuple[str, ...] = ()
    #: Whether the file at the card's path could not be read as a card at all;
    #: ``validation_errors`` then says why.
    unreadable: bool = False

    @property
    def valid(self) -> bool:
        """Whether the card is complete and internally self-consistent."""

        return not self.validate()

    def validate(self) -> tuple[str, ...]:
        """Why the card is invalid; empty when it is valid.

        :func:`load_readbacks` judges a card once, when it reads it, and
        records why in ``validation_errors``. A card it found valid must
        still be the card it read: its fields must rebuild the bytes its
        ``file_hash`` names, so a card built or changed by hand is not valid
        for the card it claims to be.
        """

        errors = list(self.validation_errors)
        fields = (
            self.article_id, self.declaration, self.statement_hash, self.packet_hash, self.model, self.shown_text,
            self.text,
        )
        if not errors and not (
            all(isinstance(value, str) for value in fields)
            and self.file_hash
            == _card_hash(
                _card_content(
                    article_id=self.article_id,
                    declaration=self.declaration,
                    statement_hash=self.statement_hash,
                    packet_hash=self.packet_hash,
                    model=self.model,
                    packet_text=self.shown_text,
                    testimony=self.text,
                ).encode("utf-8", "surrogatepass")
            )
        ):
            errors.append("whole-file hash does not match the canonical card bytes")
        return tuple(dict.fromkeys(errors))


def readback_path(blueprint: Path, article_id: str, declaration: str) -> Path:
    """The card's place in the vault; refuses names that would leave the readbacks tree."""

    if not ARTICLE_ID_PATTERN.fullmatch(article_id):
        raise ValueError(f"invalid article_id for a read-back: {article_id!r}")
    try:
        filename = declaration_filename(declaration, suffix=".md")
    except ValueError as exc:
        raise ValueError(f"invalid read-back declaration: {declaration!r}") from exc
    return blueprint / READBACKS_DIR / article_id / filename


def readback_keys(article_id: str, declaration: str) -> tuple[tuple[str, str], ...]:
    """The keys :func:`load_readbacks` may file a declaration's card under.

    A card is keyed by the Lean name its filename spells. A name too long for
    a filename is spelled only by a digest, which the name the card records
    must match; a card whose bytes cannot be read, or whose recorded name
    does not match, is keyed by its filename's stem.
    """

    try:
        filename = declaration_filename(declaration, suffix=".md")
    except ValueError:
        return ((article_id, declaration),)
    return (article_id, declaration), (article_id, filename[: -len(".md")])


def readback_for(
    readbacks: Mapping[tuple[str, str], Readback], article_id: str, declaration: str
) -> Readback | None:
    """The card :func:`load_readbacks` filed for a declaration, wherever its name left it keyed."""

    found: dict[tuple[str, str], Readback] = {}
    for key in readback_keys(article_id, declaration):
        if (card := readbacks.get(key)) is not None:
            _add_card(found, replace(card, declaration=declaration))
    return found.get((article_id, declaration))


def load_readbacks(blueprint: str | Path) -> dict[tuple[str, str], Readback]:
    """Read every read-back in the vault, keyed by article id and Lean name.

    A directory under ``readbacks/`` that cannot be listed would hide every
    card in it, so loading refuses with a :class:`ValueError` naming it
    rather than reading as though no card were filed there.
    """

    root = blueprint_root(blueprint) / READBACKS_DIR
    found: dict[tuple[str, str], Readback] = {}
    for path, raw, unreadable in _card_files(root):
        relative = path.relative_to(root)
        try:
            relative.as_posix().encode("utf-8")
        except UnicodeEncodeError:
            # Linux reads a name that is not UTF-8 with surrogate escapes, which
            # no card's name has and no message can print.
            relative = Path(os.fsencode(relative).decode("utf-8", "replace"))
            unreadable = "card path is not UTF-8"
        path_article_id = relative.parent.name if len(relative.parts) == 2 else None
        declaration = relative.stem
        if raw is not None and unreadable is None:
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                unreadable = "card is not UTF-8 text"
        if unreadable is not None:
            # Reported rather than skipped, so a review calls the card invalid
            # instead of missing, and a write can replace it by naming the
            # hash of its bytes.
            _add_card(
                found,
                Readback(
                    article_id=path_article_id or relative.parent.as_posix(),
                    declaration=_declaration_from_filename(relative.name) or declaration,
                    statement_hash=None,
                    packet_hash=None,
                    model=None,
                    text="",
                    path=path,
                    file_hash=None if raw is None else _card_hash(raw),
                    validation_errors=(unreadable,),
                    unreadable=True,
                ),
            )
            continue
        metadata, body, frontmatter_errors = _split(text)
        recorded_article_id = metadata.get("article_id")
        recorded_declaration = metadata.get("declaration")
        filename_declaration = _declaration_from_filename(relative.name)
        if filename_declaration is None and recorded_declaration:
            # A long name's filename is a digest, which only the recorded name can match.
            try:
                if declaration_filename(recorded_declaration, suffix=".md") == relative.name:
                    filename_declaration = recorded_declaration
            except ValueError:
                pass
        # Keyed by the path, so a card recording another name is reported for
        # the declaration whose card the writer would find there.
        declaration = filename_declaration or relative.stem
        errors = list(frontmatter_errors)
        # From the path, so a card in the wrong folder cannot name another article.
        article_id = path_article_id or relative.parent.as_posix()
        if recorded_article_id is not None and not ARTICLE_ID_PATTERN.fullmatch(recorded_article_id):
            errors.append(f"card records an invalid article_id: '{quoted(recorded_article_id)}'")
        if path_article_id is None or not ARTICLE_ID_PATTERN.fullmatch(path_article_id):
            errors.append("card path does not use one valid article_id directory")
        if recorded_article_id and path_article_id and recorded_article_id != path_article_id:
            errors.append(
                f"frontmatter article_id '{quoted(recorded_article_id)}' does not match "
                f"the card path article_id {path_article_id!r}"
            )
        if not recorded_declaration:
            errors.append("card records no declaration")
        else:
            try:
                expected = readback_path(root.parent, recorded_article_id or "", recorded_declaration)
            except ValueError:
                errors.append(f"card records an invalid declaration: '{quoted(recorded_declaration)}'")
            else:
                # As text, so a suffix in another case differs on Windows too.
                if expected.as_posix() != path.as_posix():
                    errors.append(
                        f"frontmatter declaration '{quoted(recorded_declaration)}' does not match "
                        f"the card path {relative.as_posix()!r}"
                    )
        statement_hash, packet_hash = (_hash_or_none(metadata.get(field)) for field in ("statement", "packet"))
        for field, recorded in (("statement", statement_hash), ("packet", packet_hash)):
            if recorded is None:
                errors.append(f"card records {'no' if metadata.get(field) is None else 'a malformed'} {field} hash")
        model = metadata.get("model")
        if model is None or not model.strip():
            errors.append("card records no nonempty model label")
        shown_text, testimony = None, ""
        if (parts := _card_parts(body)) is None:
            errors.append("card body must be one skeleton block followed by one read-back block")
        else:
            shown_text, testimony = parts
            if testimony.strip():
                errors.extend(errors_in_testimony(testimony, writing=False))
            else:
                errors.append("card contains no nonempty read-back testimony")
        shown_hash = evidence_hash_of(shown_text) if shown_text is not None else None
        if shown_hash is not None and packet_hash is not None and shown_hash != packet_hash:
            errors.append(_ALTERED_PACKET)
        if None not in (recorded_article_id, recorded_declaration, statement_hash, packet_hash, model, shown_text):
            canonical = _card_content(
                article_id=recorded_article_id,
                declaration=recorded_declaration,
                statement_hash=statement_hash,
                packet_hash=packet_hash,
                model=model,
                packet_text=shown_text,
                testimony=testimony,
            )
            if canonical != text:
                errors.append("card is not in canonical autoform-readback/v1 form")
        readback = Readback(
            article_id=article_id,
            declaration=declaration,
            statement_hash=statement_hash,
            packet_hash=packet_hash,
            model=model,
            text=testimony,
            path=path,
            shown_hash=shown_hash,
            shown_text=shown_text,
            file_hash=_card_hash(raw),
            validation_errors=tuple(errors),
        )
        _add_card(found, readback)
    return found


def _add_card(found: dict[tuple[str, str], Readback], readback: Readback) -> None:
    """Key a loaded card by its identity; a second card with that identity makes the first invalid."""

    key = (readback.article_id, readback.declaration)
    if previous := found.get(key):
        # Named from the vault, as a site shows them, never by a machine's
        # path: a card's article id is the folder it was found in.
        first, second = (PurePosixPath(READBACKS_DIR, card.article_id, card.path.name) for card in (previous, readback))
        duplicate = f"multiple cards claim the same declaration identity: {str(first)!r} and {str(second)!r}"
        found[key] = replace(
            previous,
            validation_errors=tuple(dict.fromkeys((*previous.validation_errors, duplicate))),
        )
    else:
        found[key] = readback


def _card_files(root: Path) -> Iterator[tuple[Path, bytes | None, str | None]]:
    """Every ``*.md`` entry under ``root``, in any case, with its bytes, read without following a link.

    A card is a file inside the vault; a symlink could point anywhere. The
    walk does not enter linked directories, and each card is opened without
    following a link, so a linked card or directory is skipped. Anything else
    that is not a regular file, cannot be read, or holds more than a card may
    comes with no bytes and the reason instead. A directory that cannot be
    listed is refused with a :class:`ValueError`.
    """

    def unlistable(exc: OSError) -> None:
        if exc.errno not in _NO_CARD_ERRNOS:
            raise ValueError(f"cannot list read-back directory {exc.filename}: {exc.strerror}") from exc

    if root.is_symlink():
        return
    # Unlike a glob, a walk reports each directory it cannot list.
    listed = (
        Path(directory, name)
        for directory, subdirectories, files in os.walk(root, onerror=unlistable)
        for name in (*subdirectories, *files)
        # Matched in any case: on a volume that ignores case, the writer's
        # ``.md`` path reaches a ``.MD`` file, so the loader reports it too.
        if name[-3:].lower() == ".md"
    )
    for path in sorted(listed):
        read = _read_card_file(path)
        if read is not None:
            yield path, *read


def _read_card_file(path: Path) -> tuple[bytes | None, str | None] | None:
    """The bytes of the regular file at ``path``, or why they could not be read.

    ``None`` if there is nothing there to read: the path is a link, or no
    longer leads anywhere. Opening never blocks, even on a FIFO.
    """

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if path.is_symlink():  # where there is no O_NOFOLLOW
            return None
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in _NO_CARD_ERRNOS:
            return None
        return None, f"card cannot be read: {exc.strerror}"
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, "card path is not a regular file"
        raw = _read_card_bytes(descriptor)
    except OSError as exc:
        return None, f"card cannot be read: {exc.strerror}"
    finally:
        os.close(descriptor)
    if raw is None:
        return None, f"card is over the {_CARD_MAX_BYTES}-byte limit for a card file"
    return raw, None


def _read_card_bytes(descriptor: int) -> bytes | None:
    """Read an open card file through its descriptor; ``None`` if it holds more than a card may.

    At most one byte past the limit is read, so an oversized file costs no
    more than a card. The descriptor is read directly, not through a file
    object that would also own it, so an interruption cannot close it twice.
    """

    blocks: list[bytes] = []
    remaining = _CARD_MAX_BYTES + 1
    while remaining and (block := os.read(descriptor, min(remaining, 64 * 1024))):
        blocks.append(block)
        remaining -= len(block)
    return None if remaining == 0 else b"".join(blocks)


@dataclass(frozen=True, slots=True)
class PreparedReadback:
    """A finished card, not yet written: where it goes and exactly what it says.

    A request names inputs (a packet and a testimony); this is the output
    built from them: the card's destination and its complete text, with the
    packet and testimony embedded. :func:`prepare_readback` builds one only
    after every check passes, and :func:`publish_readback` writes it.
    """

    #: The blueprint the card is filed in.
    blueprint: Path
    article_id: str
    declaration: str
    #: Destination, ``<blueprint>/readbacks/<article_id>/<Declaration>--<sha256 of
    #: the name>.md``. It depends only on the name, so a re-review of a changed
    #: packet lands on the card it supersedes.
    path: Path
    #: The complete card: frontmatter, packet, and testimony.
    content: str
    #: Carried from the request: the hash of the card this one may replace.
    expected_card_hash: str | None

    def __post_init__(self) -> None:
        if len(self.content.encode("utf-8")) > _CARD_MAX_BYTES:
            raise ValueError(
                f"read-back card for {self.declaration} is over the {_CARD_MAX_BYTES}-byte limit for a card file"
            )


def prepare_readback(
    blueprint: str | Path,
    *,
    article_id: str,
    declaration: str,
    statement_hash: str,
    packet_hash: str,
    model: str,
    text: str,
    packet_text: str,
    expected_card_hash: str | None = None,
) -> PreparedReadback:
    """Run every check a card must pass, and build it, without touching the
    filesystem, so that a bad card is refused before anything is written.

    ``packet_text`` is required because the card attests to what the
    independent reader actually received, not to a packet reconstructed
    later; it must hash to ``packet_hash``. Whether the packet is still the
    declaration's is judged later, by comparing that hash with a fresh
    extraction's. Line ends in the testimony are written as ``\\n``.
    """

    blueprint_path = blueprint_root(blueprint)
    path = readback_path(blueprint_path, article_id, declaration)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not model.strip():
        raise ValueError("a read-back requires a nonempty model label")
    if not _safe_model_label(model):
        raise ValueError(
            "a read-back model label must be printable, single-line text without invisible or reordering "
            "characters or stacked marks"
        )
    if not text.strip():
        raise ValueError("a read-back requires nonempty testimony")
    if testimony_errors := errors_in_testimony(text):
        raise ValueError("unsafe read-back testimony: " + "; ".join(testimony_errors))
    if expected_card_hash is not None and _hash_or_none(expected_card_hash) is None:
        raise ValueError(f"invalid expected card hash: {expected_card_hash!r}")
    if _hash_or_none(statement_hash) is None:
        raise ValueError(f"invalid statement hash: {statement_hash!r}")
    if _hash_or_none(packet_hash) is None or evidence_hash_of(packet_text) != packet_hash:
        raise ValueError(f"read-back packet does not match its packet hash for {declaration}")
    if not packet_text.endswith("\n"):
        # Its closing fence would share its last line, and a Markdown reader
        # would keep the Lean block open into the testimony.
        raise ValueError(f"read-back packet does not end with a line break for {declaration}")
    return PreparedReadback(
        blueprint=blueprint_path,
        article_id=article_id,
        declaration=declaration,
        path=path,
        content=_card_content(
            article_id=article_id,
            declaration=declaration,
            statement_hash=statement_hash,
            packet_hash=packet_hash,
            model=model,
            packet_text=packet_text,
            testimony=text,
        ),
        expected_card_hash=expected_card_hash,
    )


def publish_readback(prepared: PreparedReadback) -> Path:
    """Publish a prepared card without following links or clobbering another writer.

    Existing different content is replaced only when ``expected_card_hash``
    names it. This compare-and-swap rule prevents asynchronous reviewers from
    silently overwriting one another. Filing identical content is idempotent.

    The card's directory is locked, so publications into it happen one at a
    time and none lands between another's compare and its replacement. The
    lock is held by a descriptor that never leaves this function and dies
    with the process, so neither an interruption nor a crash strands it.
    """

    _require_publication_support()
    # Derived again, as the card's path follows from its article and name alone.
    path = readback_path(prepared.blueprint, prepared.article_id, prepared.declaration)
    if path != prepared.path:
        raise ValueError(f"a prepared read-back names a path that is not its card's: {prepared.path}")
    directory = _open_card_directory(prepared.blueprint, prepared.article_id)
    try:
        _lock_card_directory(directory, path)
        _publish_card(directory, path, prepared.content, expected_card_hash=prepared.expected_card_hash)
    finally:
        _release_card_directory(directory)
    return path


def _hash_or_none(value: str | None) -> str | None:
    """A recorded hash, or ``None`` when the card carries none or a malformed one."""

    return value if value is not None and _HASH.fullmatch(value) else None


def _safe_model_label(value: str, *, writing: bool = True) -> bool:
    """Whether ``value`` may label a card's model: short printable text on one
    line, with none of the characters testimony may not hold: no invisible or
    reordering character, and no combining marks stacked or with nothing to
    sit on. As in :func:`errors_in_testimony`, code points this Python's
    Unicode data leaves unassigned are refused only when ``writing``."""

    return (
        bool(value)
        and value == value.strip()
        and len(value) <= 200
        and all(
            character.isprintable() or (not writing and unicodedata.category(character) == "Cn")
            for character in value
        )
        and not hidden_characters(value)
        and not overstacked(unicodedata.normalize("NFD", value))
        and not baseless_mark(value)
    )


# --------------------------------------------------------------------------- #
# The card file
# --------------------------------------------------------------------------- #


#: How many lines may hold a card's frontmatter: its six fields, with room for
#: blank lines a hand edit leaves.
_MAX_FRONTMATTER_LINES = 32


def _card_content(
    *,
    article_id: str,
    declaration: str,
    statement_hash: str,
    packet_hash: str,
    model: str,
    packet_text: str,
    testimony: str,
) -> str:
    packet_fence = _fence(packet_text)
    testimony = testimony.strip("\n")
    testimony_fence = _fence(testimony)
    return "".join(
        [
            "---\n",
            f"schema: {READBACK_SCHEMA}\n",
            f"article_id: {json.dumps(article_id, ensure_ascii=False)}\n",
            f"declaration: {json.dumps(declaration, ensure_ascii=False)}\n",
            f"statement: {statement_hash}\n",
            f"packet: {packet_hash}\n",
            f"model: {json.dumps(model, ensure_ascii=False)}\n",
            "---\n",
            f"\n{SKELETON_HEADING}\n\n",
            f"{packet_fence}lean\n",
            packet_text,
            f"{packet_fence}\n\n",
            f"{READBACK_HEADING}\n\n",
            f"{testimony_fence}text\n",
            testimony,
            f"\n{testimony_fence}\n",
        ]
    )


def _fence(text: str) -> str:
    """A code fence nothing in ``text`` can close: one backtick longer than its longest run, and at least three."""

    return "`" * max(3, max((len(run) for run in re.findall("`+", text)), default=0) + 1)


def _card_parts(body: str) -> tuple[str, str] | None:
    """The packet and testimony of a card body laid out as :func:`_card_content`
    writes it: the packet in a Lean block, then the testimony in a text block,
    each fenced one backtick longer than the longest run in it, so nothing in
    either can close its block. Found in one pass, since a pattern with a
    lazy group before a greedy one backtracks over a body that repeats the
    closing lines; whatever this accepts is still compared with the canonical
    form."""

    opening = f"\n{SKELETON_HEADING}\n\n"
    if not body.startswith(opening):
        return None
    fence = _fence_at(body, len(opening))
    start = len(opening) + len(fence) + len("lean\n")
    if not fence or not body.startswith("lean\n", start - len("lean\n")):
        return None
    closing = f"{fence}\n\n{READBACK_HEADING}\n\n"
    end = body.find(closing, start)
    if end < 0:
        return None
    testimony_fence = _fence_at(body, end + len(closing))
    testimony_start = end + len(closing) + len(testimony_fence) + len("text\n")
    if not testimony_fence or not body.startswith("text\n", testimony_start - len("text\n")):
        return None
    for ending in (f"\n{testimony_fence}\n", f"\n{testimony_fence}"):
        if body.endswith(ending) and len(body) - len(ending) >= testimony_start:
            return body[start:end], body[testimony_start : len(body) - len(ending)]
    return None


def _fence_at(text: str, index: int) -> str:
    """The run of three or more backticks that starts at ``index`` in ``text``, or ""."""

    end = index
    while end < len(text) and text[end] == "`":
        end += 1
    return text[index:end] if end - index >= 3 else ""


def _declaration_from_filename(filename: str) -> str | None:
    """Decode a short canonical card filename; long names rely on metadata."""

    if not filename.endswith(".md"):
        return None
    encoded, separator, digest = filename[:-3].rpartition("--")
    if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    try:
        declaration = unquote_to_bytes(encoded).decode("utf-8")
        return declaration if declaration_filename(declaration, suffix=".md") == filename else None
    except (UnicodeError, ValueError):
        # Not UTF-8, or a name no card could have, such as an empty one.
        return None


def _split(text: str) -> tuple[dict[str, str], str, tuple[str, ...]]:
    """Parse the deliberately small, versioned read-back frontmatter schema.

    At most :data:`MAX_NAMED` errors are named, and a count of the rest, so a
    card cannot fill a report with them."""

    # The frontmatter is read from its first lines only, so a card of junk
    # lines costs no more than a card.
    lines = text.split("\n", _MAX_FRONTMATTER_LINES + 1)
    metadata: dict[str, str] = {}
    seen: set[str] = set()
    errors: list[str] = []
    if lines[0].strip() != "---":
        return metadata, text, ("card has no frontmatter block",)
    closing = next(
        (index for index, line in enumerate(lines[1 : _MAX_FRONTMATTER_LINES + 1], 1) if line.strip() == "---"), None
    )
    if closing is None:
        return metadata, "", (f"card has no closing frontmatter delimiter in its first {_MAX_FRONTMATTER_LINES} lines",)
    for line_number, raw in enumerate(lines[1:closing], 2):
        if not raw.strip():
            continue
        if ":" not in raw:
            errors.append(f"frontmatter line {line_number} is malformed")
            continue
        key, value = (part.strip() for part in raw.split(":", 1))
        if not key:
            errors.append(f"frontmatter line {line_number} has no key")
            continue
        if key in seen:
            errors.append(f"frontmatter field '{quoted(key)}' appears more than once")
            continue
        seen.add(key)
        if key not in _FRONTMATTER_FIELDS:
            errors.append(f"unknown frontmatter field '{quoted(key)}'")
        if key in _QUOTED_FRONTMATTER_FIELDS:
            # Only a JSON string is accepted, and one opens with a quote; a
            # value that opens with one decodes to a string or not at all.
            # Anything else is refused undecoded: the decoder recurses once per
            # nested array, so enough "[" raise RecursionError.
            if not value.startswith('"'):
                errors.append(f"frontmatter field {key!r} must be a JSON double-quoted string")
                continue
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                errors.append(f"frontmatter field {key!r} must be a JSON double-quoted string")
                continue
            if key == "model" and not _safe_model_label(decoded, writing=False):
                errors.append(
                    "frontmatter field 'model' must be printable, single-line text without invisible or "
                    "reordering characters or stacked marks"
                )
                continue
            metadata[key] = decoded
        else:
            metadata[key] = value
    for field in sorted(_FRONTMATTER_FIELDS - metadata.keys()):
        errors.append(f"missing frontmatter field {field!r}")
    schema = metadata.get("schema")
    if schema is not None and schema != READBACK_SCHEMA:
        errors.append(f"unsupported read-back schema '{quoted(schema)}'; expected {READBACK_SCHEMA!r}")
    if len(errors) > MAX_NAMED:
        errors[MAX_NAMED:] = [f"and {len(errors) - MAX_NAMED} more frontmatter errors"]
    return metadata, "\n".join(lines[closing + 1 :]), tuple(errors)


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #


def _require_publication_support() -> None:
    """Refuse, before anything is created or read, where this platform cannot lock a card directory."""

    if fcntl is None or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("this platform cannot safely publish read-back cards")


def _open_card_directory(blueprint: Path, article_id: str) -> int:
    """Make the card directory if it is missing, and open it, refusing a link on the way.

    The blueprint is a resolved path. Each directory below it is made, or
    found and checked not to be a link, before the next is made inside it,
    and the last is opened without following a link. Once both are made, the
    read-back folder gets a ``.gitattributes`` if it has none.
    """

    if not ARTICLE_ID_PATTERN.fullmatch(article_id):
        raise ValueError(f"invalid article_id for a read-back: {article_id!r}")
    reached = blueprint
    for part in (READBACKS_DIR, article_id):
        reached = reached / part
        try:
            os.mkdir(reached, 0o755)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError(f"cannot create read-back directory component: {reached}: {exc.strerror}") from exc
        if reached.is_symlink():
            raise ValueError(f"refusing a symlink in the read-back path: {reached}")
    _keep_line_ends(blueprint / READBACKS_DIR)
    try:
        return os.open(reached, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"refusing a symlink or unsafe component in the read-back path: {reached}") from exc
        raise ValueError(f"cannot open read-back directory: {reached}: {exc.strerror}") from exc


def _keep_line_ends(readbacks: Path) -> None:
    """Give the read-back folder a ``.gitattributes`` that keeps Git from
    converting line ends, unless it has one: a card checked out with CRLF line
    ends, as Git for Windows writes by default, loads as invalid."""

    path = readbacks / ".gitattributes"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        return
    except OSError as exc:
        raise ValueError(f"cannot create {path}: {exc.strerror}") from exc
    try:
        if os.write(descriptor, _GITATTRIBUTES) != len(_GITATTRIBUTES):
            raise OSError(errno.EIO, "short write")
    except OSError as exc:
        # Removed, so the next publication writes it whole.
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise ValueError(f"cannot write {path}: {exc.strerror}") from exc
    finally:
        os.close(descriptor)


def _card_hash_at(path: Path, directory: int) -> str | None:
    """The hash of the card's bytes, read without following a link; ``None`` if there is no card.

    The card is opened by its name in the open card ``directory``. Only a
    regular file is read, and at most one byte past the card limit, which
    marks a card over it; opening never blocks, even on a FIFO. The bytes are
    hashed whatever they hold, so a card that is not UTF-8 can still be
    named, and replaced, by its hash.
    """

    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot safely inspect existing read-back: {path}: {exc.strerror}") from exc
    try:
        try:
            regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
            raw = _read_card_bytes(descriptor) if regular else None
        except OSError as exc:
            raise ValueError(f"cannot read existing read-back: {path}: {exc.strerror}") from exc
        if not regular:
            raise ValueError(f"read-back destination is not a regular file: {path}")
    finally:
        os.close(descriptor)
    if raw is None:
        raise ValueError(
            f"existing read-back is over the {_CARD_MAX_BYTES}-byte limit for a card file: {path}; "
            "remove it to file a new card"
        )
    return _card_hash(raw)


def _card_hash(raw: bytes) -> str:
    """The hash compare-and-swap names a card by: of its bytes, as stored."""

    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _card_conflict(path: Path, before: str | None, expected_card_hash: str | None) -> str | None:
    """Why compare-and-swap refuses to replace the card hashed ``before`` with different content.

    ``None`` if it allows it: existing content is replaced only when
    ``expected_card_hash`` names it, and a card that names a hash replaces
    only that content, not a missing card.
    """

    if before is not None and expected_card_hash is None:
        return f"read-back already exists with different content: {path}; retry with expected_card_hash={before!r}"
    if before != expected_card_hash:
        found = "no card" if before is None else repr(before)
        return f"read-back changed before replacement: expected {expected_card_hash!r}, found {found}"
    return None


def _lock_card_directory(directory: int, path: Path) -> None:
    """Take the card directory's exclusive lock, so its publications happen one at a time.

    The lock belongs to the descriptor's open file description, which a child
    forked while the lock is held shares, so closing the descriptor alone
    would leave the lock with that child; :func:`_release_card_directory`
    unlocks first. A process that dies holding the lock releases it once no
    process shares the description: at once, unless such a child still runs.
    """

    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"another read-back publication in {path.parent} did not finish; retry later"
                ) from exc
            time.sleep(0.005)
        except OSError as exc:
            raise ValueError(f"cannot lock read-back directory for writing: {path.parent}: {exc}") from exc


def _release_card_directory(directory: int) -> None:
    """Unlock a card directory descriptor, then close it."""

    try:
        fcntl.flock(directory, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(directory)


def _publish_card(directory: int, path: Path, content: str, *, expected_card_hash: str | None) -> None:
    """Publish a card under compare-and-swap, while the caller holds its directory's lock.

    Every step goes by name in the open, locked card ``directory``, so a
    folder swapped for a link after it was opened is not followed. No other
    publication acts between the compare and the replacement.
    Content identical to the card's is not written again. Otherwise the new
    card is staged beside the old one, flushed to disk, and renamed over it
    in one step, so the card's name holds the old complete card until it
    holds the new one. A failure or interruption before the rename removes
    the staging file if it can; a crash, SIGTERM, or SIGKILL can leave it,
    and the loader never reads it as a card.
    """

    data = content.encode("utf-8")
    before = _card_hash_at(path, directory)
    if before == _card_hash(data):
        return
    if conflict := _card_conflict(path, before, expected_card_hash):
        raise ValueError(conflict)
    # Named before it exists, so the cleanup below sees the staging file from
    # the moment it is created. The name is too random to be anyone else's.
    staged: str | None = f"{_WORK_PREFIX}{secrets.token_hex(12)}.tmp"
    try:
        try:
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except OSError:
            # A failed exclusive create makes no file, and a name already
            # taken is not this write's file, so there is nothing to remove.
            staged = None
            raise
        try:
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(staged, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        staged = None
    except OSError as exc:
        raise ValueError(f"cannot publish read-back: {path}: {exc}") from exc
    finally:
        if staged is not None:
            with contextlib.suppress(OSError):
                os.unlink(staged, dir_fd=directory)


__all__ = [
    "READBACKS_DIR",
    "READBACK_HEADING",
    "READBACK_SCHEMA",
    "PreparedReadback",
    "Readback",
    "load_readbacks",
    "prepare_readback",
    "publish_readback",
    "readback_for",
    "readback_keys",
    "readback_path",
]
