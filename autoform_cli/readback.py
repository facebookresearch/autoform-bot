"""Read-backs: blind testimony about what a skeleton literally asserts.

A skeleton tells a reviewer what to read. A read-back tells them what it says,
in mathematical English, written by someone who was shown only the Lean and
never the article, the source, or the author's intent. The reviewer then
compares the read-back with the source statement; every gap between the two is
exactly what they are looking for. The practice follows the read-back audits
of the Prove2me platform.

Read-backs live in the vault under ``blueprint/readbacks/<article_id>/<encoded
Lean name>.md``. Each file is a self-contained review card: the exact skeleton the
auditor was shown, in a Lean block, followed by the testimony under a
``## Read-back`` heading, so a reviewer working in the vault sees the Lean and
its rendering side by side without the site. They are testimony, not derived
state, so they are committed with the book, and each one records the skeleton
hash it testifies about and the evidence hash of the packet text it was
written from. When the skeleton's meaning moves, the read-back is stale; when
only the packet text changes, it is revised; the audit says so either way,
and the renderer still shows it, marked as testimony about an earlier
skeleton, rather than silently presenting stale evidence as current.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import stat
import time
import unicodedata
import warnings
from collections import Counter
from dataclasses import dataclass, replace
from html.entities import html5 as _NAMED_REFERENCES
from pathlib import Path
from typing import Iterable, Iterator, Mapping
from urllib.parse import unquote_to_bytes

import html5lib
import markdown as markdown_renderer
from markdown.blockprocessors import HashHeaderProcessor
from markdown.treeprocessors import Treeprocessor
from markdown.util import ETX, STX

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows, which cannot publish cards
    fcntl = None  # type: ignore[assignment]

from .graph import ARTICLE_ID_PATTERN
from .markdown import SITE_EXTENSION_CONFIGS, SITE_EXTENSIONS
from .skeleton import (
    DeclarationSkeleton,
    SkeletonReport,
    atomic_rename,
    declaration_filename,
    evidence_hash_of,
    require_atomic_exchange,
)

READBACKS_DIR = "readbacks"
READBACK_HEADING = "## Read-back"
READBACK_SCHEMA = "autoform-readback/v1"
SKELETON_HEADING = "## Skeleton"
_FRONTMATTER_FIELDS = frozenset({"schema", "article_id", "declaration", "skeleton", "packet", "model"})
_QUOTED_FRONTMATTER_FIELDS = frozenset({"article_id", "declaration", "model"})
#: The only hash form a card may record: what `autoform skeleton` prints.
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ALTERED_PACKET = "the displayed skeleton does not match the recorded packet hash"
#: Staging files beside a card, which can hold a card just replaced, start with
#: this and never end in ``.md``, so the loader never takes one for a card.
_WORK_PREFIX = ".autoform-readback-"
#: How often a publication walks to the card directory again after finding,
#: once it holds the lock, that the directory was replaced meanwhile.
_DIRECTORY_ATTEMPTS = 3
#: How many times withdrawing a card puts back a save made over it meanwhile
#: before it gives up and names where each card was left.
_RESTORE_ROUNDS = 4
#: What a name in a card directory holds: the device and inode it names, and
#: the hash of its bytes if it is a readable regular file. ``(None, None)``
#: stands for a name that holds something that could not even be looked at.
_FileState = tuple[tuple[int, int] | None, str | None]
#: How long a publication waits for another one in the same card directory.
_LOCK_TIMEOUT = 10.0
#: A ``%`` after an even number of backslashes starts a TeX comment, which
#: silently drops the rest of its line from the typeset formula.
_TEX_COMMENT = re.compile(r"(?<!\\)(?:\\\\)*%")

#: Characters that render as nothing, or reorder the text around them, so a
#: card could say more or other than a reader sees. Unicode's general
#: categories catch most (controls, format characters such as zero-width
#: spaces and bidirectional overrides, separators, private-use and unassigned
#: code points); the rest are default-ignorable or blank letters and symbols.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})
_HIDDEN_CODE_POINTS = frozenset(
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180F, 0x2800, 0x3164, 0xFFA0}
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)
_ALLOWED_CONTROLS = frozenset("\t\n\r")

#: Limits a testimony must meet before the Markdown renderer reads it. Python-
#: Markdown's inline processing is superlinear in the number of spans and of
#: unmatched openers, cubic in a run of backticks, and recursive in nesting
#: depth, so a byte limit alone does not bound the work. Four characters cost
#: a pass over the rest of the text each: a "[", which three link patterns
#: scan from to its closing bracket; a "<" before a letter, "/", "!" or "?",
#: which the HTML block parser scans from for the end of a tag, even inside a
#: formula; an underscore that starts a word, which the emphasis patterns scan
#: from for a closing one; and a backslash, since each escape rebuilds the text
#: and lengthens what the link patterns scan. The byte, line, delimiter,
#: backtick, bracket, and nesting limits are several times what 120 read-backs
#: of a real-analysis textbook use: at most 5.9 KB, 62 lines, 304 math
#: delimiters, 72 backticks in runs of one, 2 brackets, and 3 columns of
#: indentation. The tag, underscore, and backslash limits keep each of their
#: passes near half a second at the byte limit, and at every limit at once
#: validation takes under two seconds.
TESTIMONY_MAX_BYTES = 32 * 1024
TESTIMONY_MAX_LINES = 500
TESTIMONY_MAX_MATH_DELIMITERS = 1024
TESTIMONY_MAX_BACKTICKS = 512
TESTIMONY_MAX_BACKTICK_RUN = 16
TESTIMONY_MAX_BRACKETS = 64
TESTIMONY_MAX_NESTING = 64
TESTIMONY_MAX_TAG_OPENERS = 256
TESTIMONY_MAX_UNDERSCORE_OPENERS = 256
TESTIMONY_MAX_BACKSLASHES = 2048
_MATH_DELIMITER = re.compile(r"\$|\\[()\[\]]")
_BACKTICK_RUN = re.compile(r"`+")
_TAG_OPENER = re.compile(r"<[A-Za-z/!?]")
_UNDERSCORE_OPENER = re.compile(r"(?<!\w)_")
#: Everything a line can open before its content: indentation, block quotes,
#: and list markers, each of which nests one more block.
_NESTING_PREFIX = re.compile(r"(?:[ >]|[-+*](?= )|\d{1,9}[.)](?= ))*")
#: What a CommonMark viewer of the vault, GitHub's or Obsidian's, reads as HTML
#: outside code: open and closing tags in CommonMark's grammar, the openers of
#: comments, declarations, and processing instructions, closed or not,
#: autolinks, and character references by number or by a name HTML defines.
#: The testimony renderer reads none of it and shows it as typed, but a
#: reviewer reading the card in the vault would be shown something else.
_HTML_TAG = re.compile(
    r"<\?[^>\n]*>?|<![A-Za-z\[][^>\n]*>?"
    r"|</?[A-Za-z][A-Za-z0-9-]*"
    r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*\s*/?>"
)
_HTML_NAME = re.compile(r"<[!?](?:\[CDATA\[|[A-Za-z]*)|</?[A-Za-z][A-Za-z0-9-]*")
_AUTOLINK = re.compile(
    r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>"
    r"|<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*>"
)
_HTML_ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{0,31});")


@dataclass(frozen=True, slots=True)
class Readback:
    """One read-back file, parsed."""

    article_id: str
    declaration: str
    skeleton_hash: str | None
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

    @property
    def valid(self) -> bool:
        """Whether the card is complete and internally self-consistent."""

        return not self.validate()

    @property
    def shows_what_it_attests(self) -> bool:
        """Whether the displayed packet is the one the card's hash names.

        A card is read in the vault, so the Lean a reviewer sees is the
        evidence. An edit to that block would otherwise leave the hashes
        attesting to a packet nobody read.
        """

        return self.shown_hash is not None and self.packet_hash is not None and self.shown_hash == self.packet_hash

    def validate(
        self,
        expected: DeclarationSkeleton | None = None,
        *,
        article_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return intrinsic errors and, when given, mismatches with a declaration."""

        errors = list(self.validation_errors)
        if not isinstance(self.article_id, str) or ARTICLE_ID_PATTERN.fullmatch(self.article_id) is None:
            errors.append("card has no valid article_id")
        if not isinstance(self.declaration, str) or not self.declaration:
            errors.append("card has no declaration")
        else:
            try:
                declaration_filename(self.declaration, suffix=".md")
            except ValueError:
                errors.append("card has an invalid declaration")
        if not isinstance(self.skeleton_hash, str) or _HASH.fullmatch(self.skeleton_hash) is None:
            errors.append("card has no valid skeleton hash")
        if not isinstance(self.packet_hash, str) or _HASH.fullmatch(self.packet_hash) is None:
            errors.append("card has no valid packet hash")
        if not isinstance(self.model, str) or not _safe_model_label(self.model):
            errors.append("card has no valid model label")
        if not isinstance(self.text, str) or not self.text.strip():
            errors.append("card has no nonempty testimony")
        elif testimony_errors := _testimony_errors(self.text):
            errors.extend(testimony_errors)
        if not isinstance(self.shown_text, str):
            errors.append("card has no exact displayed packet")
        else:
            calculated_shown_hash = evidence_hash_of(self.shown_text)
            if self.shown_hash != calculated_shown_hash:
                errors.append("displayed packet hash does not match its bytes")
            if self.packet_hash != calculated_shown_hash:
                errors.append(_ALTERED_PACKET)
        if not isinstance(self.file_hash, str) or _HASH.fullmatch(self.file_hash) is None:
            errors.append("card has no valid whole-file hash")
        elif all(
            isinstance(value, str)
            for value in (
                self.article_id,
                self.declaration,
                self.skeleton_hash,
                self.packet_hash,
                self.model,
                self.shown_text,
                self.text,
            )
        ):
            canonical = _card_content(
                article_id=self.article_id,
                declaration=self.declaration,
                skeleton_hash=self.skeleton_hash or "",
                packet_hash=self.packet_hash or "",
                model=self.model or "",
                packet_text=self.shown_text or "",
                testimony=self.text,
            )
            if self.file_hash != evidence_hash_of(canonical):
                errors.append("whole-file hash does not match the canonical card bytes")
        if article_id is not None and self.article_id != article_id:
            errors.append(f"card article_id is {self.article_id!r}, expected {article_id!r}")
        if expected is not None:
            if self.declaration != expected.name:
                errors.append(f"card identity is {self.declaration!r}, expected {expected.name!r}")
            if self.skeleton_hash != expected.hash:
                errors.append(f"skeleton hash is {self.skeleton_hash!r}, expected {expected.hash}")
            if self.packet_hash != expected.evidence_hash:
                errors.append(f"packet hash is {self.packet_hash!r}, expected {expected.evidence_hash}")
        return tuple(dict.fromkeys(errors))

    def status(self, skeleton: DeclarationSkeleton) -> str:
        """``current``, ``revised`` (same meaning, packet text changed), or ``stale``."""

        if not self.valid:
            return "invalid"
        if self.skeleton_hash != skeleton.hash:
            return "stale"
        if self.packet_hash != skeleton.evidence_hash:
            return "revised"
        return "current"


def readback_path(blueprint: Path, article_id: str, declaration: str) -> Path:
    """The card's place in the vault; refuses names that would leave the readbacks tree."""

    if not ARTICLE_ID_PATTERN.fullmatch(article_id):
        raise ValueError(f"invalid article_id for a read-back: {article_id!r}")
    try:
        filename = declaration_filename(declaration, suffix=".md")
    except ValueError as exc:
        raise ValueError(f"invalid read-back declaration: {declaration!r}") from exc
    return blueprint / READBACKS_DIR / article_id / filename


def load_readbacks(blueprint: str | Path) -> dict[tuple[str, str], Readback]:
    """Read every read-back in the vault, keyed by article id and Lean name."""

    root = Path(blueprint).expanduser().resolve() / READBACKS_DIR
    found: dict[tuple[str, str], Readback] = {}
    for path, raw in _card_files(root):
        relative = path.relative_to(root)
        path_article_id = relative.parent.name if len(relative.parts) == 2 else None
        declaration = relative.stem
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            continue
        metadata, body, frontmatter_errors = _split(text)
        recorded_article_id = metadata.get("article_id")
        recorded_declaration = metadata.get("declaration")
        filename_declaration = _declaration_from_filename(relative.name)
        declaration = filename_declaration or recorded_declaration or relative.stem
        errors = list(frontmatter_errors)
        article_id = path_article_id or recorded_article_id or relative.parent.as_posix()
        if recorded_article_id is not None and not ARTICLE_ID_PATTERN.fullmatch(recorded_article_id):
            errors.append(f"card records an invalid article_id: {recorded_article_id!r}")
        if path_article_id is None or not ARTICLE_ID_PATTERN.fullmatch(path_article_id):
            errors.append("card path does not use one valid article_id directory")
        if recorded_article_id and path_article_id and recorded_article_id != path_article_id:
            errors.append(
                f"frontmatter article_id {recorded_article_id!r} does not match "
                f"the card path article_id {path_article_id!r}"
            )
        if not recorded_declaration:
            errors.append("card records no declaration")
        else:
            try:
                expected = readback_path(root.parent, recorded_article_id or "", recorded_declaration)
            except ValueError:
                errors.append(f"card records an invalid declaration: {recorded_declaration!r}")
            else:
                if expected != path:
                    errors.append(
                        f"frontmatter declaration {recorded_declaration!r} does not match "
                        f"the card path {relative.as_posix()!r}"
                    )
        raw_skeleton_hash = metadata.get("skeleton")
        skeleton_hash = _hash_or_none(raw_skeleton_hash)
        if skeleton_hash is None:
            qualifier = "no" if raw_skeleton_hash is None else "a malformed"
            errors.append(f"card records {qualifier} skeleton hash")
        raw_packet_hash = metadata.get("packet")
        packet_hash = _hash_or_none(raw_packet_hash)
        if packet_hash is None:
            qualifier = "no" if raw_packet_hash is None else "a malformed"
            errors.append(f"card records {qualifier} packet hash")
        model = metadata.get("model")
        if model is None or not model.strip():
            errors.append("card records no nonempty model label")
        shown_text, testimony, body_errors = _card_body(
            body,
            article_id=recorded_article_id,
            declaration=recorded_declaration,
            skeleton_hash=skeleton_hash,
            model=model,
        )
        errors.extend(body_errors)
        errors.extend(_testimony_errors(testimony))
        shown_hash = evidence_hash_of(shown_text) if shown_text is not None else None
        if shown_hash is not None and packet_hash is not None and shown_hash != packet_hash:
            errors.append(_ALTERED_PACKET)
        if all(
            value is not None
            for value in (
                recorded_article_id,
                recorded_declaration,
                skeleton_hash,
                packet_hash,
                model,
                shown_text,
            )
        ):
            canonical = _card_content(
                article_id=recorded_article_id or "",
                declaration=recorded_declaration or "",
                skeleton_hash=skeleton_hash or "",
                packet_hash=packet_hash or "",
                model=model or "",
                packet_text=shown_text or "",
                testimony=testimony,
            )
            if canonical != text:
                errors.append("card is not in canonical autoform-readback/v1 form")
        readback = Readback(
            article_id=article_id,
            declaration=declaration,
            skeleton_hash=skeleton_hash,
            packet_hash=packet_hash,
            model=model,
            text=testimony,
            path=path,
            shown_hash=shown_hash,
            shown_text=shown_text,
            file_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
            validation_errors=tuple(errors),
        )
        key = (article_id, declaration)
        if previous := found.get(key):
            duplicate = f"multiple cards claim the same declaration identity: {previous.path} and {path}"
            found[key] = replace(
                previous,
                validation_errors=tuple(dict.fromkeys((*previous.validation_errors, duplicate))),
            )
        else:
            found[key] = readback
    return found


def _card_files(root: Path) -> Iterator[tuple[Path, bytes]]:
    """Every regular ``*.md`` file under ``root``, with its bytes, read without following a link.

    A card is a file inside the vault; a symlink could point anywhere. The
    candidates come from a listing, which can be out of date by the time a
    file is opened, so a path-level check is not enough. Where the platform
    allows, each candidate is opened through no-follow descriptors walked
    down from ``root``, and a card or directory swapped for a symlink after
    the listing is refused rather than followed. Elsewhere (Windows) the file
    is opened first; then no component of its path may be a link, and the
    open file must be the one a no-follow stat of the path names.
    """

    walk = hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
    root_descriptor: int | None = None
    if walk:
        try:
            root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return
    elif root.is_symlink() or not root.is_dir():
        return
    try:
        for path in sorted(root.rglob("*.md")):
            raw = _read_card_file(root, root_descriptor, path.relative_to(root))
            if raw is not None:
                yield path, raw
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _read_card_file(root: Path, root_descriptor: int | None, relative: Path) -> bytes | None:
    """The bytes of the regular file at ``root / relative``, or ``None`` if it is not one."""

    file_flags = (
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    opened: list[int] = []
    try:
        if root_descriptor is not None:
            directory = root_descriptor
            for part in relative.parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                opened.append(directory)
            descriptor = os.open(relative.name, file_flags, dir_fd=directory)
            opened.append(descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
        else:
            # Checked only after the open: a link swapped in before it is
            # still there, or else the path now names a different file.
            path = root / relative
            descriptor = os.open(path, file_flags)
            opened.append(descriptor)
            opened_file = os.fstat(descriptor)
            if any((root / parent).is_symlink() for parent in relative.parents):
                return None
            named_file = os.lstat(path)
            if not (stat.S_ISREG(opened_file.st_mode) and stat.S_ISREG(named_file.st_mode)):
                return None
            if (opened_file.st_dev, opened_file.st_ino) != (named_file.st_dev, named_file.st_ino):
                return None
        chunks: list[bytes] = []
        while block := os.read(descriptor, 64 * 1024):
            chunks.append(block)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        for held in reversed(opened):
            os.close(held)


@dataclass(frozen=True, slots=True)
class PreparedReadback:
    """A finished card, not yet written: where it goes and exactly what it says.

    A request names inputs (a packet file and a testimony file); this is the
    output built from them: the card's destination and its complete text, with
    the packet and testimony embedded. :func:`prepare_readback` builds one only
    after every check passes, so a batch can check all its cards before
    :func:`publish_readback` writes the first.
    """

    #: The blueprint the card is filed in; publishing opens it without
    #: following links.
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


def write_readback(
    blueprint: str | Path,
    *,
    article_id: str,
    declaration: DeclarationSkeleton,
    model: str,
    text: str,
    packet_text: str,
    expected_card_hash: str | None = None,
) -> Path:
    """File a complete card without following links or clobbering another writer.

    ``packet_text`` is required because the card attests to what the independent
    reader actually received, not to a packet reconstructed later. A differing
    packet is rejected even if a caller supplies matching metadata.

    Existing different content is replaced only when ``expected_card_hash``
    names it. This compare-and-swap rule prevents asynchronous reviewers from
    silently overwriting one another. Filing identical content is idempotent.
    """

    return publish_readback(
        prepare_readback(
            blueprint,
            article_id=article_id,
            declaration=declaration,
            model=model,
            text=text,
            packet_text=packet_text,
            expected_card_hash=expected_card_hash,
        )
    )


def prepare_readback(
    blueprint: str | Path,
    *,
    article_id: str,
    declaration: DeclarationSkeleton,
    model: str,
    text: str,
    packet_text: str,
    expected_card_hash: str | None = None,
) -> PreparedReadback:
    """Run every check :func:`write_readback` runs, and build the card, without
    touching the filesystem. A caller filing several cards prepares them all
    first, so that one bad card stops the batch before any is written."""

    blueprint_path = Path(blueprint).expanduser().resolve()
    path = readback_path(blueprint_path, article_id, declaration.name)
    _validate_readback_fields(model, text, expected_card_hash)
    expected_packet = declaration.blind_text()
    if packet_text != expected_packet or evidence_hash_of(packet_text) != declaration.evidence_hash:
        raise ValueError(f"read-back packet does not match the current packet for {declaration.name}")

    content = _card_content(
        article_id=article_id,
        declaration=declaration.name,
        skeleton_hash=declaration.hash,
        packet_hash=declaration.evidence_hash,
        model=model,
        packet_text=packet_text,
        testimony=text,
    )
    return PreparedReadback(
        blueprint=blueprint_path,
        article_id=article_id,
        declaration=declaration.name,
        path=path,
        content=content,
        expected_card_hash=expected_card_hash,
    )


def _validate_readback_fields(
    model: str,
    text: str,
    expected_card_hash: str | None,
) -> None:
    if not model.strip():
        raise ValueError("a read-back requires a nonempty model label")
    if not _safe_model_label(model):
        raise ValueError("a read-back model label must be printable, single-line text")
    if not text.strip():
        raise ValueError("a read-back requires nonempty testimony")
    testimony_errors = _testimony_errors(text)
    if testimony_errors:
        raise ValueError("unsafe read-back testimony: " + "; ".join(testimony_errors))
    if expected_card_hash is not None and _hash_or_none(expected_card_hash) is None:
        raise ValueError(f"invalid expected card hash: {expected_card_hash!r}")


def planned_readback(
    blueprint: str | Path,
    *,
    article_id: str,
    declaration: str,
    skeleton_hash: str,
    packet_hash: str,
    model: str,
    text: str,
    packet_text: str,
    expected_card_hash: str | None = None,
) -> PreparedReadback:
    """The card a record would file, built from prepared evidence before Lean runs.

    It is not checked against the current Lean tree and must never be
    published; :func:`prepare_readback` builds the card that is. When the
    prepared evidence is current, the two are identical, so a batch can find
    the cards it would conflict with before paying for an extraction.
    """

    blueprint_path = Path(blueprint).expanduser().resolve()
    _validate_readback_fields(model, text, expected_card_hash)
    if _hash_or_none(skeleton_hash) is None:
        raise ValueError(f"invalid skeleton hash: {skeleton_hash!r}")
    if _hash_or_none(packet_hash) is None or evidence_hash_of(packet_text) != packet_hash:
        raise ValueError("read-back packet does not match its prepared packet hash")
    return PreparedReadback(
        blueprint=blueprint_path,
        article_id=article_id,
        declaration=declaration,
        path=readback_path(blueprint_path, article_id, declaration),
        content=_card_content(
            article_id=article_id,
            declaration=declaration,
            skeleton_hash=skeleton_hash,
            packet_hash=packet_hash,
            model=model,
            packet_text=packet_text,
            testimony=text,
        ),
        expected_card_hash=expected_card_hash,
    )


def readback_conflicts(cards: Iterable[PreparedReadback]) -> list[str]:
    """Every card publishing would refuse, found without writing anything.

    This is the compare-and-swap rule :func:`publish_readback` applies, checked
    for a whole batch first: a card may replace existing different content
    only when it names that content's hash, and a card that names a hash
    replaces only that content, not a missing card. Each conflict names the
    hash to pass. Publishing still checks each card, since another writer can
    act in between. Where publishing is refused, so is this check.
    """

    _require_publication_support()
    conflicts: list[str] = []
    for card in cards:
        before = _existing_card_hash(card.blueprint, card.article_id, card.path)
        if before == evidence_hash_of(card.content):
            continue
        if before is not None and card.expected_card_hash is None:
            conflicts.append(
                f"{card.declaration}: read-back already exists with different content: {card.path}; "
                f"to replace it, pass expected_card_hash={before!r}"
            )
        elif card.expected_card_hash != before:
            conflicts.append(
                f"{card.declaration}: read-back changed before replacement: "
                f"expected {card.expected_card_hash!r}, found {before!r}"
            )
    return conflicts


def publish_readback(prepared: PreparedReadback) -> Path:
    """Publish a prepared card under the same compare-and-swap rule.

    The returned path is checked to name the card just published: if the card
    directory was moved meanwhile, or the card replaced or rewritten, this
    raises instead of returning a path that does not lead to it. The check
    walks from the blueprint without following links, as the loader does,
    compares the file it finds by device, inode, and content, and runs before
    the directory's lock is released, so no other publication can change the
    answer.
    """

    published, reported = _publish_locked(prepared)
    if reported != published:
        raise ValueError(
            f"cannot confirm the published read-back: {prepared.path} no longer names it; "
            "its directory was moved or replaced, or an editor replaced or rewrote the card, during publication"
        )
    return prepared.path


@dataclass(frozen=True, slots=True)
class ReadbackFinding:
    """A read-back that is missing or no longer testifies about the current skeleton."""

    node_id: str
    declaration: str
    code: str
    reason: str


def readback_findings(
    report: SkeletonReport,
    readbacks: dict[tuple[str, str], Readback],
    *,
    article_ids: Mapping[str, str] | None = None,
) -> list[ReadbackFinding]:
    """Compare every skeleton with testimony keyed by durable article id.

    ``article_ids`` maps each report node id to the corresponding graph
    ``article_id``. It is required when those identities differ.
    """

    findings: list[ReadbackFinding] = []
    identities = article_ids or {}
    for node in report.nodes:
        article_id = identities.get(node.node_id, node.node_id)
        for declaration in node.declarations:
            readback = readbacks.get((article_id, declaration.name))
            if readback is None:
                findings.append(
                    ReadbackFinding(
                        node.node_id,
                        declaration.name,
                        "readback-missing",
                        f"no read-back filed for {declaration.name}; write one from its blind packet",
                    )
                )
            elif validation_errors := readback.validate():
                code = "readback-altered" if validation_errors == (_ALTERED_PACKET,) else "readback-invalid"
                detail = "; ".join(validation_errors)
                if code == "readback-altered":
                    detail += f" (records {readback.packet_hash}, shows {readback.shown_hash})"
                findings.append(
                    ReadbackFinding(
                        node.node_id,
                        declaration.name,
                        code,
                        f"read-back for {declaration.name} is not valid: {detail}",
                    )
                )
            elif readback.status(declaration) == "stale":
                findings.append(
                    ReadbackFinding(
                        node.node_id,
                        declaration.name,
                        "readback-stale",
                        (
                            f"read-back for {declaration.name} testifies about skeleton "
                            f"{readback.skeleton_hash}; the current skeleton is {declaration.hash}"
                            if readback.skeleton_hash
                            else f"read-back for {declaration.name} records no valid skeleton hash; "
                            f"the current skeleton is {declaration.hash}"
                        ),
                    )
                )
            elif readback.status(declaration) == "revised":
                findings.append(
                    ReadbackFinding(
                        node.node_id,
                        declaration.name,
                        "readback-revised",
                        f"read-back for {declaration.name} was written from packet "
                        f"{readback.packet_hash}; the packet text is now {declaration.evidence_hash}",
                    )
                )
    # Testimony about a declaration the blueprint no longer names is evidence
    # for nothing, and would otherwise sit in the vault unmentioned forever.
    named = {
        (identities.get(node.node_id, node.node_id), declaration.name)
        for node in report.nodes
        for declaration in node.declarations
    }
    for article_id, name in sorted(set(readbacks) - named):
        findings.append(
            ReadbackFinding(
                article_id,
                name,
                "readback-orphaned",
                f"read-back filed for {name} under article_id {article_id}, which names no such declaration; "
                "the statement was renamed or removed, so delete the card or restore the name",
            )
        )
    return findings


def _hash_or_none(value: str | None) -> str | None:
    """A recorded hash, or ``None`` when the card carries none or a malformed one."""

    return value if value is not None and _HASH.fullmatch(value) else None


def _safe_model_label(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and len(value) <= 200
        and all(character.isprintable() for character in value)
    )


#: Python-Markdown's placeholder for a backslash-escaped dollar sign.
_ESCAPED_DOLLAR = STX + str(ord("$")) + ETX


class _LiteralText(Treeprocessor):
    """Show every ``&`` as typed, and keep an escaped dollar sign escaped.

    The testimony renderer reads no HTML, so each ``<``, ``>``, and ``&`` in a
    testimony is text. The serializer escapes ``<`` and ``>`` but leaves an
    ``&`` that starts something shaped like a character reference, so ``&``
    is escaped here first, outside code, whose text the renderer escaped
    already. ``\\$`` would otherwise come out as a bare ``$``, which MathJax
    pairs with the next one into a formula; it is kept as ``\\$``, which
    MathJax shows as a dollar sign.
    """

    def run(self, root: object) -> None:
        for element in root.iter():  # type: ignore[attr-defined]
            if element.tag != "code" and element.text:
                element.text = self._literal(element.text)
            if element.tail:
                element.tail = self._literal(element.tail)

    @staticmethod
    def _literal(text: str) -> str:
        literal = text.replace("&", "&amp;").replace(_ESCAPED_DOLLAR, "\\" + _ESCAPED_DOLLAR)
        # Formulas are atomic strings, which the renderer must not read again.
        return type(text)(literal) if literal != text else text


class _HashHeading(HashHeaderProcessor):
    """A ``#`` heading only where CommonMark reads one, with a space or the
    line's end after the hashes, so ``#41755`` starting a line stays text, as
    a viewer of the vault shows it."""

    RE = re.compile(r"(?:^|\n)(?P<level>#{1,6})(?=[ \t\n]|$)(?P<header>(?:\\.|[^\\])*?)#*(?:\n|$)")


def _testimony_converter() -> markdown_renderer.Markdown:
    """A Markdown converter for testimony: paragraphs, emphasis, lists, block
    quotes, tables, code, and formulas, and no HTML, entities, autolinks,
    attribute lists, heading IDs, or diagrams. Code blocks get no syntax
    highlighting and display formulas a ``<p>``, so the output holds no
    ``<div>``, the one element the site's page could take for its own."""

    converter = markdown_renderer.Markdown(
        extensions=["tables", "pymdownx.arithmatex", "pymdownx.highlight", "pymdownx.superfences"],
        extension_configs={
            "pymdownx.arithmatex": {"generic": True, "block_tag": "p"},
            "pymdownx.highlight": {"use_pygments": False},
            "pymdownx.superfences": {"custom_fences": []},
        },
    )
    converter.preprocessors.deregister("html_block")
    converter.parser.blockprocessors.register(_HashHeading(converter.parser), "hashheader", 70)
    for pattern in ("html", "entity", "autolink", "automail"):
        converter.inlinePatterns.deregister(pattern)
    converter.treeprocessors.register(_LiteralText(converter), "literal_text", 5)
    return converter


def render_testimony(text: str) -> str:
    """The HTML a testimony is shown as: what the validator inspects and what
    the site embeds, byte for byte."""

    return _render_testimony(text)[0]


def _render_testimony(text: str) -> tuple[str, bool]:
    """The HTML for ``text``, and whether it defines Markdown links.

    The site places the HTML inside its own Markdown page, whose parser takes
    a block-level tag at the start of a line for the start of a block it reads
    again. Each such tag is put on the line before it, where it is left as
    written; the newlines between blocks are not shown.
    """

    converter = _testimony_converter()
    rendered = converter.convert(text)
    names = "|".join(sorted(converter.block_level_elements, key=len, reverse=True))
    return re.sub(rf"\n(?=<(?:{names})[\s/>])", "", rendered), bool(converter.references)


#: The elements the testimony renderer emits for what a testimony may use.
_TESTIMONY_ELEMENTS = frozenset(
    {"p", "br", "hr", "em", "strong", "code", "pre", "span", "blockquote", "ul", "ol", "li"}
    | {"table", "thead", "tbody", "tr", "th", "td"}
)
#: The attributes the renderer sets, by element: its classes for code blocks
#: and formulas, table alignment, and where an ordered list starts. Any other,
#: an ``id`` from a fenced block's header say, was written by the testimony.
_TESTIMONY_ATTRIBUTES: dict[str, dict[str, re.Pattern[str]]] = {
    "pre": {"class": re.compile("highlight")},
    "code": {"class": re.compile(r"language-[\w#.+-]+")},
    "span": {"class": re.compile("arithmatex")},
    "p": {"class": re.compile("arithmatex")},
    "th": {"style": re.compile("text-align: (?:left|center|right);")},
    "td": {"style": re.compile("text-align: (?:left|center|right);")},
    "ol": {"start": re.compile("[0-9]{1,9}")},
}
_MERMAID_FENCE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})[ \t]*mermaid(?:[ \t]|$)", re.MULTILINE | re.IGNORECASE)


#: Commands that set a letter, so testimony showing only these still shows one.
_TEX_LETTERS = frozenset(
    r"""
    \alpha \beta \gamma \delta \epsilon \varepsilon \zeta \eta \theta \vartheta \iota \kappa
    \lambda \mu \nu \xi \omicron \pi \varpi \rho \varrho \sigma \varsigma \tau \upsilon \phi
    \varphi \chi \psi \omega \Gamma \Delta \Theta \Lambda \Xi \Pi \Sigma \Upsilon \Phi \Psi
    \Omega \aleph \ell \hbar \imath \jmath \wp \Re \Im
    """.split()
)
#: Spaces no wider than two quads. A single negative thin space (``\!``) is
#: listed too, since it tightens ``\int\! f``, but two in a row slide one
#: symbol over another, and more than :data:`_TEX_MAX_SPACE_RUN` spaces in a
#: row push the rest of a formula aside, so both are refused.
_TEX_SPACES = frozenset({r"\,", r"\:", r"\;", "\\ ", "\\\n", "\\\t", r"\quad", r"\qquad", "~"})
_TEX_MAX_SPACE_RUN = 4
#: The TeX testimony may use: the notation statements need, out of what the
#: site's pinned MathJax 3.2.2 defines in the base and ams packages, the only
#: ones javascripts/mathjax.js loads. Nothing listed sizes, moves, hides,
#: overlaps, colours, boxes, or labels content, and nothing defines a macro.
#: Each command maps to how many arguments must be given that show something.
#: Any other command or environment is refused by name, so extending this is a
#: deliberate edit, to be checked against that MathJax version.
_TESTIMONY_TEX: dict[str, int] = {
    **dict.fromkeys(_TEX_LETTERS, 0),
    # Symbols and logic.
    **dict.fromkeys(
        r"""
        \infty \partial \nabla \emptyset \varnothing \prime \forall \exists \nexists \neg \lnot
        \top \bot \angle \triangle \backslash \complement \therefore \because \colon \not
        \ldots \cdots \vdots \ddots \dots \dotsc \dotsb
        """.split(),
        0,
    ),
    # Relations.
    **dict.fromkeys(
        r"""
        \lt \gt \le \leq \ge \geq \ne \neq \ll \gg \leqslant \geqslant \nleq \ngeq \lneq \gneq
        \lesssim \gtrsim \equiv \approx \cong \ncong \sim \simeq \asymp \propto \doteq \triangleq
        \prec \preceq \succ \succeq \in \ni \notin \owns \subset \subseteq \supset \supseteq
        \subsetneq \supsetneq \nsubseteq \nsupseteq \sqsubseteq \sqsupseteq \mid \nmid
        \parallel \nparallel \perp \vdash \dashv \models \vDash \nvdash \nvDash \triangleleft
        \trianglelefteq \triangleright \trianglerighteq
        """.split(),
        0,
    ),
    # Binary operators.
    **dict.fromkeys(
        r"""
        \pm \mp \times \div \cdot \ast \star \circ \bullet \cap \cup \setminus \smallsetminus
        \wedge \vee \land \lor \oplus \ominus \otimes \oslash \odot \sqcap \sqcup \uplus \amalg
        \dagger \ddagger \diamond \ltimes \rtimes \boxplus \boxtimes \bmod \pmod \mod
        """.split(),
        0,
    ),
    # Arrows.
    **dict.fromkeys(
        r"""
        \to \gets \mapsto \longmapsto \rightarrow \leftarrow \leftrightarrow \Rightarrow
        \Leftarrow \Leftrightarrow \longrightarrow \longleftarrow \longleftrightarrow
        \Longrightarrow \Longleftarrow \Longleftrightarrow \iff \implies \impliedby
        \hookrightarrow \hookleftarrow \twoheadrightarrow \uparrow \downarrow \updownarrow
        \Uparrow \Downarrow \Updownarrow \nearrow \searrow \swarrow \nwarrow \restriction
        \xrightarrow \xleftarrow
        """.split(),
        0,
    ),
    # Big operators and named operators.
    **dict.fromkeys(
        r"""
        \sum \prod \coprod \int \iint \iiint \oint \bigcup \bigcap \bigoplus \bigotimes \bigvee
        \bigwedge \bigsqcup \biguplus \bigodot \limits \nolimits \lim \liminf \limsup \sup \inf
        \max \min \sin \cos \tan \sec \csc \cot \sinh \cosh \tanh \coth \arcsin \arccos \arctan
        \log \ln \lg \exp \det \dim \ker \deg \gcd \hom \arg \Pr
        """.split(),
        0,
    ),
    **dict.fromkeys(r"\substack".split(), 1),
    # Delimiters and their sizes.
    **dict.fromkeys(
        r"""
        \{ \} \| \langle \rangle \lfloor \rfloor \lceil \rceil \vert \Vert \lvert \rvert \lVert
        \rVert \lbrace \rbrace \left \right \middle \big \Big \bigg \Bigg \bigl \bigr \Bigl \Bigr
        \biggl \biggr \Biggl \Biggr \bigm \Bigm \biggm \Biggm
        """.split(),
        0,
    ),
    # Fractions, roots, accents, and stacking. A root's optional index is not counted.
    **dict.fromkeys(r"\frac \dfrac \tfrac \binom \dbinom \tbinom \overset \underset \stackrel".split(), 2),
    r"\sqrt": 0,
    **dict.fromkeys(
        r"""
        \hat \bar \tilde \vec \dot \ddot \check \breve \acute \grave \mathring \widehat
        \widetilde \overline \underline \overrightarrow \overleftarrow \overbrace \underbrace
        """.split(),
        1,
    ),
    # Fonts and text.
    **dict.fromkeys(
        r"""
        \mathbb \mathcal \mathfrak \mathscr \mathrm \mathbf \mathsf \mathit \mathtt \operatorname
        \text \textrm \textbf \textit
        """.split(),
        1,
    ),
    **dict.fromkeys(r"\displaystyle \textstyle".split(), 0),
    # Spaces, escaped characters, rows and columns, and math delimiters.
    **dict.fromkeys(_TEX_SPACES - {"~"}, 0),
    r"\!": 0,
    **dict.fromkeys(r"\# \$ \% \& \_ \\ \( \) \[ \]".split(), 0),
    **dict.fromkeys(
        r"""
        \begin{cases} \begin{matrix} \begin{pmatrix} \begin{bmatrix} \begin{Bmatrix}
        \begin{vmatrix} \begin{Vmatrix} \begin{aligned} \begin{gathered}
        """.split(),
        0,
    ),
}
_TEX_TOKEN = re.compile(r"\\(?:[A-Za-z]+|.)?|[{}~$]|[^\\{}~$\s]+|\s+", re.DOTALL)
_TEX_FORMULA_BOUNDARIES = frozenset({"$", r"\(", r"\)", r"\[", r"\]"})
_TEX_ENVIRONMENT = re.compile(r"\s*\{([^{}\\]{0,64})\}")
_TEX_STAR = re.compile(r"\s*\*")
#: ``\\[<dimension>]`` spaces rows apart, or with a negative dimension draws
#: one over another.
_TEX_ROW_SPACING = re.compile(r"\*?\[")
_TEX_ENVIRONMENT_NAME = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
_TEX_CONTROL_SEQUENCE = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)


def _testimony_errors(text: str) -> tuple[str, ...]:
    """Reject testimony that could run code, fetch remote content, or say more
    or other than a reader sees.

    Read-backs need prose, lists, emphasis, code, and mathematical notation.
    They do not need links, headings, or embedded content.

    A testimony is measured against :data:`TESTIMONY_MAX_BYTES` and the other
    limits first, and one that exceeds any of them is refused unparsed: every
    card in a pull request is validated, so parsing must be bounded before
    anything is known about it. What is then checked is the HTML
    :func:`render_testimony` makes of it, which is exactly what the site
    shows: only the elements and attributes the renderer emits for prose,
    code, and formulas; no HTML a Markdown viewer of the vault would read
    outside code; no invisible or reordering characters; in text the
    typesetter reads, only the TeX listed in :data:`_TESTIMONY_TEX`, with
    arguments that show something and spacing that neither overlaps symbols
    nor pushes them apart; and at least one visible letter or digit.
    """

    if limits := _testimony_limit_errors(text):
        return limits
    rendered, defines_links = _render_testimony(text)
    document = html5lib.parseFragment(rendered, namespaceHTMLElements=False)
    errors: list[str] = []
    if defines_links:
        errors.append("Markdown link definitions are not allowed")
    for element in document.iter():
        if element is document:
            continue
        tag = element.tag.lower() if isinstance(element.tag, str) else ""
        if tag in {"a", "img"}:
            errors.append("Markdown links, images, and autolinks are not allowed")
        elif re.fullmatch("h[1-6]", tag):
            errors.append("Markdown headings are not allowed: they read as the page's own; use **bold** text")
        elif tag not in _TESTIMONY_ELEMENTS:
            errors.append(f"HTML the testimony renderer does not emit is not allowed: <{tag or 'comment'}>")
        elif any(
            name not in _TESTIMONY_ATTRIBUTES.get(tag, {}) or _TESTIMONY_ATTRIBUTES[tag][name].fullmatch(value) is None
            for name, value in element.attrib.items()
        ):
            errors.append("user-supplied Markdown attributes are not allowed")
        if tag == "code" and element.attrib.get("class", "").lower() == "language-mermaid":
            errors.append("active Mermaid blocks are not allowed")
    if _MERMAID_FENCE.search(text):
        errors.append("active Mermaid blocks are not allowed")
    pieces = _testimony_pieces(document)
    errors.extend(_vault_markup_errors(text, [piece for piece, kind in pieces if kind == "code"]))
    hidden = _hidden_characters(text + "".join(document.itertext()))
    if hidden:
        errors.append("invisible or reordering characters are not allowed: " + ", ".join(hidden))
    typeset = "".join(piece for piece, kind in pieces if kind != "code")
    errors.extend(_tex_errors(typeset))
    if _TEX_COMMENT.search(typeset):
        errors.append("TeX comments are not allowed: they drop the rest of their line; write \\% for a percent sign")
    if not _shows_letter_or_digit("".join(document.itertext())):
        errors.append("testimony renders no visible text: it must show at least one letter or digit")
    return tuple(dict.fromkeys(errors))


def _testimony_pieces(document: object) -> list[tuple[str, str]]:
    """The text nodes of ``document`` in order, each marked ``"code"``,
    ``"math"`` for a formula the renderer marked, or ``"text"``. MathJax reads
    each text node apart from the others."""

    pieces: list[tuple[str, str]] = []

    def walk(node: object, kind: str) -> None:
        tag = getattr(node, "tag", None)
        if not isinstance(tag, str):
            return
        if tag.lower() in {"code", "pre"}:
            kind = "code"
        elif kind == "text" and "arithmatex" in node.attrib.get("class", "").split():  # type: ignore[attr-defined]
            kind = "math"
        if node.text:  # type: ignore[attr-defined]
            pieces.append((node.text, kind))  # type: ignore[attr-defined]
        for child in node:  # type: ignore[attr-defined]
            walk(child, kind)
            if child.tail:
                pieces.append((child.tail, kind))

    walk(document, "text")
    return pieces


def _vault_markup_errors(text: str, code: list[str]) -> list[str]:
    """Name the HTML, autolinks, and character references ``text`` holds
    outside the pieces of ``code`` the renderer shows as code.

    The site shows them as typed; a CommonMark viewer of the vault reads them,
    and could hide text, restyle it, link it, or stand for a character a
    reader cannot see. They are found in the source, since the renderer
    consumes Markdown a viewer could read as part of a tag, such as the
    ``>`` that starts a line, and each found is excused only by as many
    occurrences of it in code. Formulas are not code there.
    """

    def outside_code(pattern: re.Pattern[str]) -> list[str]:
        found = Counter(match.group() for match in pattern.finditer(text))
        for piece in code:
            found.subtract(match.group() for match in pattern.finditer(piece))
        return [markup for markup, count in found.items() if count > 0]

    tags = dict.fromkeys(_HTML_NAME.match(markup).group() for markup in outside_code(_HTML_TAG))
    entities = [
        _entity_name(entity)
        for entity in outside_code(_HTML_ENTITY)
        if entity[1] == "#" or entity[1:] in _NAMED_REFERENCES
    ]
    errors: list[str] = []
    if outside_code(re.compile("<!--")):
        errors.append("HTML comments are not allowed: Markdown viewers hide the text they enclose")
    if tags:
        names = (name if name.startswith(("<!", "<?")) else name + ">" for name in tags)
        errors.append("raw HTML is not allowed: " + ", ".join(names) + "; in a formula, put a space after <")
    if outside_code(_AUTOLINK):
        errors.append("Markdown links, images, and autolinks are not allowed")
    if entities:
        errors.append(
            "HTML character references are not allowed: " + ", ".join(entities) + "; type the character itself"
        )
    return errors


def _tex_errors(typeset: str) -> list[str]:
    """Check, in one pass over ``typeset``, that every TeX command and
    environment is listed in :data:`_TESTIMONY_TEX`, that each listed
    command's arguments show something, and that spacing neither slides
    symbols over one another nor pushes them apart."""

    unlisted: dict[str, None] = {}
    empty: dict[str, None] = {}
    errors: list[str] = []
    # One entry per open brace group: whether it shows anything, and the
    # command it is an argument of.
    shows: list[bool] = [True]
    owners: list[str | None] = [None]
    # Commands still waiting for arguments: the command, how many arguments
    # are still to come, and the group depth they must come at.
    waiting: list[tuple[str, int, int]] = []
    spaces = negative = longest = 0
    stacked = row_spacing = False
    position = 0
    while position < len(typeset):
        token = _TEX_TOKEN.match(typeset, position)
        assert token is not None
        position = token.end()
        text = token.group()
        if text.isspace():
            continue
        if text == "\\":
            text = "\\ "
        if text in _TEX_FORMULA_BOUNDARIES:
            empty.update(dict.fromkeys(command for command, _, _ in waiting))
            waiting.clear()
            del shows[1:], owners[1:]
            spaces = negative = 0
            continue
        if text == "{":
            owner = None
            if waiting and waiting[-1][2] == len(shows):
                owner, remaining, depth = waiting.pop()
                if remaining > 1:
                    waiting.append((owner, remaining - 1, depth))
            shows.append(False)
            owners.append(owner)
            continue
        if text == "}":
            while waiting and waiting[-1][2] == len(shows):
                empty[waiting.pop()[0]] = None
            if len(shows) > 1:
                shown, owner = shows.pop(), owners.pop()
                if owner is not None and not shown:
                    empty[owner] = None
                shows[-1] = shows[-1] or shown
            continue
        space = text in _TEX_SPACES or text == r"\!"
        if space:
            spaces += 1
            negative += text == r"\!"
            stacked = stacked or negative > 1
            longest = max(longest, spaces)
        else:
            spaces = negative = 0
            shows[-1] = True
        if waiting and waiting[-1][2] == len(shows):
            command, remaining, depth = waiting.pop()
            taken = 1 if text.startswith("\\") else len(text)
            if space:
                empty[command] = None
            if remaining > taken:
                waiting.append((command, remaining - taken, depth))
        if not text.startswith("\\"):
            continue
        if text in {r"\begin", r"\end"}:
            environment = _TEX_ENVIRONMENT.match(typeset, position)
            if environment is not None and f"\\begin{{{environment.group(1)}}}" in _TESTIMONY_TEX:
                position = environment.end()
            else:
                unlisted[f"{text}{{{environment.group(1)}}}" if environment else text] = None
            continue
        arguments = _TESTIMONY_TEX.get(text)
        if arguments is None:
            unlisted[text] = None
            continue
        if text == r"\operatorname" and (star := _TEX_STAR.match(typeset, position)):
            position = star.end()
        if text == "\\\\" and _TEX_ROW_SPACING.match(typeset, position):
            row_spacing = True
        if arguments:
            waiting.append((text, arguments, len(shows)))
    empty.update(dict.fromkeys(command for command, _, _ in waiting))
    if unlisted:
        errors.append("TeX outside the read-back allowlist is not allowed: " + ", ".join(sorted(unlisted)))
    if empty:
        errors.append("TeX arguments that show nothing are not allowed: " + ", ".join(sorted(empty)))
    if stacked:
        errors.append("repeated negative TeX spacing is not allowed: it slides symbols over one another")
    if longest > _TEX_MAX_SPACE_RUN:
        errors.append(
            f"a run of {longest} TeX spaces is not allowed: more than {_TEX_MAX_SPACE_RUN} in a row "
            "push symbols apart or out of view"
        )
    if row_spacing:
        errors.append(
            "TeX row spacing after \\\\ is not allowed: it can draw rows over one another; "
            "write {} before a bracket that starts a row"
        )
    return errors


def _shows_letter_or_digit(text: str) -> bool:
    """Whether ``text`` shows a letter or digit. TeX commands and environment
    names are not shown as written, so they are set aside, except commands
    that set a letter; spaces, delimiters, and punctuation alone say nothing."""

    text = _TEX_ENVIRONMENT_NAME.sub(" ", text)
    text = _TEX_CONTROL_SEQUENCE.sub(lambda command: "a" if command.group() in _TEX_LETTERS else " ", text)
    return any(character.isalnum() for character in text)


def _testimony_limit_errors(text: str) -> tuple[str, ...]:
    """Every limit a testimony exceeds, measured without parsing it."""

    errors: list[str] = []
    size = len(text.encode("utf-8"))
    if size > TESTIMONY_MAX_BYTES:
        errors.append(f"testimony is {size} bytes, over the {TESTIMONY_MAX_BYTES}-byte limit")
    lines = text.split("\n")
    if len(lines) > TESTIMONY_MAX_LINES:
        errors.append(f"testimony has {len(lines)} lines, over the {TESTIMONY_MAX_LINES}-line limit")
    delimiters = len(_MATH_DELIMITER.findall(text))
    if delimiters > TESTIMONY_MAX_MATH_DELIMITERS:
        errors.append(
            f"testimony has {delimiters} math delimiters, over the limit of {TESTIMONY_MAX_MATH_DELIMITERS}"
        )
    backticks = text.count("`")
    if backticks > TESTIMONY_MAX_BACKTICKS:
        errors.append(f"testimony has {backticks} backticks, over the limit of {TESTIMONY_MAX_BACKTICKS}")
    longest_run = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    if longest_run > TESTIMONY_MAX_BACKTICK_RUN:
        errors.append(
            f"testimony has a run of {longest_run} backticks, over the limit of {TESTIMONY_MAX_BACKTICK_RUN}"
        )
    brackets = text.count("[")
    if brackets > TESTIMONY_MAX_BRACKETS:
        errors.append(f"testimony has {brackets} opening brackets, over the limit of {TESTIMONY_MAX_BRACKETS}")
    nesting = max((_NESTING_PREFIX.match(line.expandtabs(4)).end() for line in lines), default=0)
    if nesting > TESTIMONY_MAX_NESTING:
        errors.append(
            f"testimony nests blocks {nesting} columns deep, over the limit of {TESTIMONY_MAX_NESTING}"
        )
    tags = len(_TAG_OPENER.findall(text))
    if tags > TESTIMONY_MAX_TAG_OPENERS:
        errors.append(
            f"testimony has {tags} tag openers (< before a letter, /, ! or ?), "
            f"over the limit of {TESTIMONY_MAX_TAG_OPENERS}"
        )
    underscores = len(_UNDERSCORE_OPENER.findall(text))
    if underscores > TESTIMONY_MAX_UNDERSCORE_OPENERS:
        errors.append(
            f"testimony has {underscores} underscores that start a word, "
            f"over the limit of {TESTIMONY_MAX_UNDERSCORE_OPENERS}"
        )
    backslashes = text.count("\\")
    if backslashes > TESTIMONY_MAX_BACKSLASHES:
        errors.append(f"testimony has {backslashes} backslashes, over the limit of {TESTIMONY_MAX_BACKSLASHES}")
    return tuple(errors)


def _raw_html_errors(text: str) -> tuple[str, ...]:
    """Name the raw HTML in ``text``: tags, comments, declarations, processing
    instructions, and character references. Read-backs are Markdown and TeX;
    HTML can hide text, restyle it, or stand for characters a reader cannot
    see. It is found before parsing, so it is refused wherever it is written,
    formulas and code included; a space after "<" keeps a formula clear."""

    errors: list[str] = []
    if "<!--" in text:
        errors.append("HTML comments are not allowed: they hide the text they enclose")
    tags: dict[str, None] = {}
    for markup in _HTML_TAG.finditer(text):
        name = _HTML_NAME.match(text, markup.start()).group()
        tags[name if name.startswith(("<!", "<?")) else name + ">"] = None
    if tags:
        errors.append("raw HTML is not allowed: " + ", ".join(tags) + "; in a formula, put a space after <")
    entities = dict.fromkeys(_entity_name(entity) for entity in _HTML_ENTITY.findall(text))
    if entities:
        errors.append(
            "HTML character references are not allowed: " + ", ".join(entities) + "; type the character itself"
        )
    return tuple(errors)


#: An HTML comment with its end: the text a site leaves out of an article.
_COMPLETE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def publishable_article(text: str) -> tuple[str, tuple[str, ...]]:
    """The text of an article a site may publish, and what refuses it.

    Articles are Markdown and TeX, as read-backs are. Raw HTML in one would
    reach the published page as markup that can restyle it, show an approval
    nobody gave, or run script, and the reviewers who approve articles do not
    own the site's templates. Complete HTML comments are a common way to leave
    a note, so they are dropped rather than refused; the text returned is the
    article without them, so a comment a browser would end sooner than the
    pattern does exposes nothing. What remains is refused when
    :func:`_raw_html_errors` finds HTML in it, or when the site's Markdown
    renderer would pass any of it through as raw HTML: the two parse markup
    differently, and the renderer's reading is what gets published.
    """

    visible = _COMPLETE_HTML_COMMENT.sub("", text)
    errors = list(_raw_html_errors(visible))
    passed = _passed_through(visible)
    if passed and not errors:
        shown = ", ".join(repr(block[:40]) for block in dict.fromkeys(passed))
        errors.append(f"raw HTML is not allowed: the site would publish {shown} as HTML")
    return visible, tuple(errors)


def _passed_through(text: str) -> list[str]:
    """What the site's renderer passes through from ``text`` as raw HTML.

    The renderer stashes raw HTML, character references, and highlighted code
    blocks alike. Code blocks are stashed while fences are read, before any
    raw HTML is, so whatever is stashed after that came from the text itself.
    """

    parser = markdown_renderer.Markdown(extensions=list(SITE_EXTENSIONS), extension_configs=SITE_EXTENSION_CONFIGS)
    fences = parser.preprocessors["fenced_code_block"]
    read_fences = fences.run
    highlighted = 0

    def counted(lines: list[str]) -> list[str]:
        nonlocal highlighted
        lines = read_fences(lines)
        highlighted = len(parser.htmlStash.rawHtmlBlocks)
        return lines

    fences.run = counted  # type: ignore[method-assign]
    parser.convert(text)
    return [block if isinstance(block, str) else "<element>" for block in parser.htmlStash.rawHtmlBlocks[highlighted:]]


def _entity_name(entity: str) -> str:
    """``entity`` and, when it stands for one character, that character's code
    point and name."""

    character = html.unescape(entity)
    if len(character) != 1:
        return entity
    return f"{entity} (U+{ord(character):04X} {unicodedata.name(character, 'unnamed')})"


def _hidden_characters(text: str) -> list[str]:
    """Name each distinct invisible or reordering character in ``text``."""

    found: dict[str, None] = {}
    for character in text:
        if character in _ALLOWED_CONTROLS:
            continue
        if unicodedata.category(character) in _HIDDEN_CATEGORIES or ord(character) in _HIDDEN_CODE_POINTS:
            name = unicodedata.name(character, "unnamed")
            found[f"U+{ord(character):04X} {name}"] = None
    return list(found)


def _card_body(
    body: str,
    *,
    article_id: str | None,
    declaration: str | None,
    skeleton_hash: str | None,
    model: str | None,
) -> tuple[str | None, str, tuple[str, ...]]:
    """Recover one exact packet and the testimony from the canonical card body."""

    if None in {article_id, declaration, skeleton_hash, model}:
        return None, "", ("card metadata is incomplete, so its body cannot be validated",)
    preamble = _card_preamble(
        article_id=article_id or "",
        declaration=declaration or "",
        skeleton_hash=skeleton_hash or "",
        model=model or "",
    )
    if not body.startswith(preamble):
        return None, "", ("card must contain exactly one skeleton block in the canonical preamble",)
    remainder = body[len(preamble) :]
    opening = re.match(r"(?P<fence>`{3,})lean\n", remainder)
    if opening is None:
        return None, "", ("card must contain one canonical skeleton block",)
    fence = opening.group("fence")
    packet_and_testimony = remainder[opening.end() :]
    closing_marker = f"{fence}\n"
    closing = packet_and_testimony.find(closing_marker)
    if closing < 0:
        return None, "", ("card must contain exactly one skeleton block",)
    shown_text = packet_and_testimony[:closing]
    after_packet = packet_and_testimony[closing + len(closing_marker) :]
    heading = f"\n{READBACK_HEADING}\n"
    if after_packet == f"\n{READBACK_HEADING}":
        return shown_text, "", ("card contains no nonempty read-back testimony",)
    if not after_packet.startswith(heading):
        return shown_text, "", ("card must put the read-back heading immediately after the skeleton block",)
    after_heading = after_packet[len(heading) :]
    testimony = after_heading[1:] if after_heading.startswith("\n") else after_heading
    errors: list[str] = []
    if not after_heading.startswith("\n"):
        errors.append("card must put one blank line after the read-back heading")
    expected_fence = "`" * max(3, _longest_backtick_run(shown_text) + 1)
    if fence != expected_fence:
        errors.append("card skeleton fence is not canonical for its packet")
    if not shown_text.endswith("\n"):
        errors.append("card skeleton packet must end with a newline")
    if not testimony.strip():
        errors.append("card contains no nonempty read-back testimony")
    return shown_text, testimony, tuple(errors)


def _card_preamble(
    *,
    article_id: str,
    declaration: str,
    skeleton_hash: str,
    model: str,
) -> str:
    return "".join(
        [
            "\n# Read-back\n\n",
            f"Article <code>{html.escape(article_id)}</code> · "
            f"declaration <code>{html.escape(declaration)}</code> · "
            f"skeleton <code>{html.escape(skeleton_hash[:12])}…</code> · "
            f"read back by <code>{html.escape(model)}</code>.\n\n",
            f"{SKELETON_HEADING}\n\n",
        ]
    )


def _card_content(
    *,
    article_id: str,
    declaration: str,
    skeleton_hash: str,
    packet_hash: str,
    model: str,
    packet_text: str,
    testimony: str,
) -> str:
    fence = "`" * max(3, _longest_backtick_run(packet_text) + 1)
    return "".join(
        [
            "---\n",
            f"schema: {READBACK_SCHEMA}\n",
            f"article_id: {json.dumps(article_id, ensure_ascii=False)}\n",
            f"declaration: {json.dumps(declaration, ensure_ascii=False)}\n",
            f"skeleton: {skeleton_hash}\n",
            f"packet: {packet_hash}\n",
            f"model: {json.dumps(model, ensure_ascii=False)}\n",
            "---\n",
            _card_preamble(
                article_id=article_id,
                declaration=declaration,
                skeleton_hash=skeleton_hash,
                model=model,
            ),
            f"{fence}lean\n",
            packet_text,
            f"{fence}\n\n",
            f"{READBACK_HEADING}\n\n",
            testimony.strip("\n"),
            "\n",
        ]
    )


def _declaration_from_filename(filename: str) -> str | None:
    """Decode a short canonical card filename; long names rely on metadata."""

    if not filename.endswith(".md"):
        return None
    encoded, separator, digest = filename[:-3].rpartition("--")
    if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    try:
        declaration = unquote_to_bytes(encoded).decode("utf-8")
    except UnicodeError:
        return None
    return declaration if declaration_filename(declaration, suffix=".md") == filename else None


def _require_publication_support() -> None:
    """Refuse, before anything is created or read, where cards cannot be published safely."""

    required = (
        hasattr(os, "O_DIRECTORY"),
        hasattr(os, "O_NOFOLLOW"),
        os.open in os.supports_dir_fd,
        os.mkdir in os.supports_dir_fd,
        os.stat in os.supports_dir_fd,
        os.stat in os.supports_follow_symlinks,
        os.unlink in os.supports_dir_fd,
        hasattr(os, "fchmod"),
        fcntl is not None,
    )
    if not all(required):
        raise ValueError("this platform cannot safely publish read-back cards")
    try:
        require_atomic_exchange()
    except NotImplementedError as exc:
        raise ValueError(f"this platform cannot safely publish read-back cards: {exc}") from exc


def _publish_locked(prepared: PreparedReadback) -> tuple[_FileState, _FileState | None]:
    """Publish a card holding its directory's lock, once the card's path still leads to that directory.

    The directory can be moved, and another made at its name, between the
    walk and the lock. A publication that went on would change a directory
    the card's path no longer reaches, and would not be ordered with
    publications into the new one. So once the lock is held the directory is
    compared with a fresh no-follow walk from the blueprint; on a mismatch it
    is unlocked and the walk starts over, a few times before the write is
    refused. Nothing is staged before the check passes.

    Returns what the publication filed and, found before the lock is
    released, what the card's path names. The locked descriptor never leaves
    this function, so no interruption can strand the lock with a descriptor
    nothing will close.
    """

    _require_publication_support()
    blueprint, article_id, path = prepared.blueprint, prepared.article_id, prepared.path
    for _ in range(_DIRECTORY_ATTEMPTS):
        directory = _open_card_parent(blueprint, article_id)
        try:
            _lock_card_directory(directory, path)
            held = os.fstat(directory)
            if _card_directory_identity(blueprint, article_id) == (held.st_dev, held.st_ino):
                published = _publish_card(
                    directory, path, prepared.content, expected_card_hash=prepared.expected_card_hash
                )
                return published, _card_identity(blueprint, article_id, path)
        finally:
            _release_card_directory(directory)
    raise ValueError(f"read-back directory {path.parent} kept being replaced while it was locked; retry later")


def _open_card_parent(blueprint: Path, article_id: str) -> int:
    """Open the card directory through held, no-follow directory descriptors."""

    if not ARTICLE_ID_PATTERN.fullmatch(article_id):
        raise ValueError(f"invalid article_id for a read-back: {article_id!r}")
    relative = Path(READBACKS_DIR) / article_id
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(blueprint, flags)
    except OSError as exc:
        raise ValueError(f"cannot safely open blueprint directory: {blueprint}") from exc
    try:
        for part in relative.parts:
            try:
                following = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValueError(f"cannot create read-back directory component: {part}") from exc
                try:
                    following = os.open(part, flags, dir_fd=current)
                except OSError as exc:
                    raise ValueError(
                        f"refusing a symlink or unsafe component in the read-back path: {part}"
                    ) from exc
            except OSError as exc:
                raise ValueError(
                    f"refusing a symlink or unsafe component in the read-back path: {part}"
                ) from exc
            # Move on before closing, so an interruption never leaves
            # ``current`` naming a closed descriptor.
            current, previous = following, current
            os.close(previous)
        return current
    except BaseException:
        os.close(current)
        raise


def _open_existing_card_parent(blueprint: Path, article_id: str) -> int | None:
    """Open the card directory through no-follow descriptors, creating nothing.

    A missing directory on the way means there is no card yet: ``None``.
    """

    if not ARTICLE_ID_PATTERN.fullmatch(article_id):
        raise ValueError(f"invalid article_id for a read-back: {article_id!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(blueprint, flags)
    except OSError as exc:
        raise ValueError(f"cannot safely open blueprint directory: {blueprint}") from exc
    try:
        for part in Path(READBACKS_DIR, article_id).parts:
            try:
                following = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ValueError(f"refusing a symlink or unsafe component in the read-back path: {part}") from exc
            current, previous = following, current
            os.close(previous)
        directory, current = current, -1
        return directory
    finally:
        if current >= 0:
            os.close(current)


def _existing_card_hash(blueprint: Path, article_id: str, path: Path) -> str | None:
    """Hash the card at ``path`` through no-follow descriptors, creating nothing."""

    directory = _open_existing_card_parent(blueprint, article_id)
    if directory is None:
        return None
    try:
        found = _card_hash_at(directory, path.name, path)
    finally:
        os.close(directory)
    return None if found is None else found[0]


def _card_directory_identity(blueprint: Path, article_id: str) -> tuple[int, int] | None:
    """The device and inode of the directory the card's path leads to, reached without following a link."""

    try:
        directory = _open_existing_card_parent(blueprint, article_id)
    except ValueError:
        return None
    if directory is None:
        return None
    try:
        found = os.fstat(directory)
    finally:
        os.close(directory)
    return found.st_dev, found.st_ino


def _card_identity(blueprint: Path, article_id: str, path: Path) -> _FileState | None:
    """What ``path`` names, reached without following a link: its device, inode, and content hash."""

    try:
        directory = _open_existing_card_parent(blueprint, article_id)
    except ValueError:
        return None
    if directory is None:
        return None
    try:
        return _file_at(directory, path.name)
    finally:
        os.close(directory)


def _card_hash_at(directory: int, filename: str, display_path: Path) -> tuple[str, tuple[int, int]] | None:
    """Read a card relative to a held directory without following links.

    Only a regular file is read; opening never blocks, even on a FIFO. Returns
    the card's hash with the device and inode of the descriptor that was
    read, so the two always describe the same file.
    """

    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot safely inspect existing read-back: {display_path}") from exc
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            raise ValueError(f"read-back destination is not a regular file: {display_path}")
        # Read through the descriptor itself, not a file object that would
        # also own it, so an interruption cannot close it twice.
        blocks = []
        while block := os.read(descriptor, 64 * 1024):
            blocks.append(block)
        try:
            text = b"".join(blocks).decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"existing read-back is not UTF-8: {display_path}") from exc
        return evidence_hash_of(text), (found.st_dev, found.st_ino)
    finally:
        os.close(descriptor)


def _file_at(directory: int, name: str) -> _FileState | None:
    """What ``name`` holds, found without following a link or blocking; ``None`` if nothing.

    Unlike :func:`_card_hash_at` this never refuses: a link, a directory, or a
    file that cannot be read is described by its device and inode alone. The
    hash of a regular file is the one :func:`evidence_hash_of` gives its text.
    """

    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        try:
            found = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            return None, None
        return (found.st_dev, found.st_ino), None
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            return (found.st_dev, found.st_ino), None
        content = hashlib.sha256()
        while block := os.read(descriptor, 64 * 1024):
            content.update(block)
        return (found.st_dev, found.st_ino), f"sha256:{content.hexdigest()}"
    except OSError:
        return None, None
    finally:
        os.close(descriptor)


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


def _publish_card(
    directory: int,
    path: Path,
    content: str,
    *,
    expected_card_hash: str | None,
) -> _FileState:
    """Publish a card through a held, locked directory descriptor under compare-and-swap.

    The caller holds the directory's lock, so no other publication acts
    between the compare and the swap. The new card is staged beside the old
    one and exchanged with it in one step, so the card's name always holds a
    complete card. The card swapped out must be the one compared: if an
    editor saved over it in between, the new card is withdrawn (see
    :func:`_restore_card`) and the write reports a conflict. A first card is
    renamed into place without replacing a name. Writers that work by path
    (renaming over the card, deleting it, or opening it with truncation after
    the swap) are never lost; one that opened the old card before the swap
    and writes to it afterwards writes to the replaced file, which is deleted.
    On any exception, a card still swapped out is swapped back; a card that
    cannot be put back is left under the staging name and named, never
    deleted. A crash can leave that name holding the card just replaced, or
    the unpublished card; the loader never reads it as a card.

    Returns what ``path.name`` holds once the card is filed: its device and
    inode, and the hash of its bytes.
    """

    filename = path.name
    content_hash = evidence_hash_of(content)
    staged_name: str | None = None
    staged: _FileState | None = None
    swapping = settled = False
    note: str | None = None
    try:
        for _ in range(100):
            # Named before it exists, so the handlers below see the staging
            # file from the moment it is created; the name is too random to
            # be anyone else's.
            staged_name = f"{_WORK_PREFIX}{secrets.token_hex(12)}.tmp"
            try:
                staged = _stage_card(directory, staged_name, content)
            except FileExistsError:
                staged_name = None
                continue
            break
        else:
            raise ValueError(f"cannot allocate a unique staging file for read-back: {path}")
        staged_path = path.with_name(staged_name)
        found = _card_hash_at(directory, filename, path)
        before = None if found is None else found[0]
        if found is not None and before == content_hash:
            # Report the file that was hashed, not whatever the name holds by now.
            return found[1], before
        if before is not None and expected_card_hash is None:
            raise ValueError(
                f"read-back already exists with different content: {path}; retry with expected_card_hash={before!r}"
            )
        if before != expected_card_hash:
            raise ValueError(
                f"read-back changed before replacement: expected {expected_card_hash!r}, found {before!r}"
            )
        conflict = f"read-back changed concurrently while writing: {path}"
        if _file_at(directory, staged_name) != staged:
            raise ValueError(f"read-back staging file changed before publication: {path}")
        # From here on the staging name may hold a card taken from the card's
        # name; before, whatever it holds is not this write's to put there.
        swapping = True
        try:
            atomic_rename(staged_name, filename, directory=directory, exchange=before is not None)
        except FileExistsError as exc:
            raise ValueError(f"{conflict}; another writer filed a card there, which was kept") from exc
        except FileNotFoundError as exc:
            raise ValueError(f"{conflict}; the card was removed, and nothing was published") from exc
        except NotImplementedError as exc:
            raise ValueError(f"this platform cannot safely publish read-back cards: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"cannot publish read-back: {path}: {exc}") from exc
        if before is not None:
            # The staging name now holds the card just replaced.
            if not _card_matches(directory, staged_name, staged_path, before):
                note = _restore_card(directory, staged_name, filename, staged, path)
                settled = True
                raise ValueError(conflict if note else f"{conflict}; the other writer's card was kept")
            try:
                os.unlink(staged_name, dir_fd=directory)
            except OSError as exc:
                warnings.warn(
                    f"could not remove the replaced read-back left at {staged_path}: {exc}",
                    RuntimeWarning,
                    stacklevel=4,
                )
        settled = True
    except BaseException as exc:
        # Decided from what the staging name holds, not from how far the code
        # got: an interruption can land between a rename and the next line.
        if swapping and not settled and _file_at(directory, staged_name) not in (None, staged):
            note = _restore_card(directory, staged_name, filename, staged, path)
        if note is None:
            raise
        if isinstance(exc, ValueError):
            raise ValueError(f"{exc}; {note}") from exc
        warnings.warn(f"read-back publication interrupted: {note}", RuntimeWarning, stacklevel=4)
        raise
    finally:
        if staged_name is not None:
            _discard_staged_card(directory, staged_name, staged)
    try:
        os.fsync(directory)
    except OSError:
        pass
    return staged


def _stage_card(directory: int, name: str, content: str) -> _FileState:
    """Write ``content`` to a new file ``name`` beside the card; return what it holds.

    Raises :class:`FileExistsError` if ``name`` exists. A file this creates
    is the caller's to remove, whatever interrupts.
    """

    data = content.encode("utf-8")
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        found = os.fstat(descriptor)
        staged = (found.st_dev, found.st_ino), evidence_hash_of(content)
        # Forgotten before it is closed, so it is never closed twice.
        descriptor, closing = -1, descriptor
        os.close(closing)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return staged


def _card_matches(directory: int, filename: str, display_path: Path, expected: str) -> bool:
    """Whether ``filename`` is a regular UTF-8 card with hash ``expected``."""

    try:
        found = _card_hash_at(directory, filename, display_path)
    except ValueError:
        return False
    return found is not None and found[0] == expected


def _restore_card(directory: int, staged_name: str, filename: str, staged: _FileState, path: Path) -> str | None:
    """Withdraw a published card: swap back what it replaced, or what was saved over it since.

    The staging name holds what the exchange took from the card's name, and
    is exchanged with it again. What comes back should be the new card; if it
    is not, a save landed at the card's name after the last exchange. That
    save is newer than the card just put back, so it is exchanged back in
    turn, until the card's name holds the newest save. A card deleted from
    its name stays deleted.

    Returns ``None`` once the card's name holds what the publication replaced
    and the staging name the unpublished card. Otherwise returns, for the
    error, what was left where; the staging name then holds a card and is not
    removed.
    """

    staged_path = path.with_name(staged_name)
    placed, placed_is = staged, "the new card"
    leaving_is = "the card this write replaced"
    for _ in range(_RESTORE_ROUNDS):
        leaving = _file_at(directory, staged_name)
        if leaving is None:
            return f"{leaving_is} was removed from {staged_path} before it could be put back; {path} holds {placed_is}"
        try:
            atomic_rename(staged_name, filename, directory=directory, exchange=True)
        except FileNotFoundError:
            if _file_at(directory, staged_name) is None:
                return (
                    f"{leaving_is} was removed from {staged_path} before it could be put back; "
                    f"{path} holds {placed_is}"
                )
            return (
                f"{path} was deleted while this write withdrew its card: the deletion stands, "
                f"and {leaving_is} was preserved at {staged_path}"
            )
        except (OSError, NotImplementedError) as exc:
            return f"{leaving_is} could not be put back ({exc}) and was preserved at {staged_path}; {path} holds {placed_is}"
        came_back = _file_at(directory, staged_name)
        if came_back == placed:
            break
        # The card's name was saved over after the last exchange; that save is
        # newer than what was just put back, so it goes back under the name.
        placed, placed_is = leaving, leaving_is
        leaving_is = "a later save to the card"
    else:
        return (
            f"the card kept being saved over while this write withdrew its card: the newest save seen was "
            f"preserved at {staged_path}, and {path} holds {placed_is}"
        )
    if came_back == staged:
        return None
    return (
        f"the card was saved again before this write could withdraw its card: that later save was kept at "
        f"{path}, and {placed_is}, which it superseded, was preserved at {staged_path}"
    )


def _discard_staged_card(directory: int, staged_name: str, staged: _FileState | None) -> None:
    """Remove the staging file if it still holds the unpublished card, unchanged.

    Anything else there is a card taken from the card's name, which the error
    names, or another process's file. ``staged`` is ``None`` when staging
    stopped before it learned what it wrote: nothing has been exchanged then,
    so whatever the name holds is this write's own. An interruption that lands
    while this decides is let through only after it decides again, so one
    interruption leaves no file behind.
    """

    try:
        if staged is None or _file_at(directory, staged_name) == staged:
            _unlink_quietly(directory, staged_name)
    except BaseException:
        if staged is None or _file_at(directory, staged_name) == staged:
            _unlink_quietly(directory, staged_name)
        raise


def _unlink_quietly(directory: int, filename: str) -> None:
    try:
        os.unlink(filename, dir_fd=directory)
    except OSError:
        pass


def _longest_backtick_run(text: str) -> int:
    runs = re.findall(r"`+", text)
    return max((len(run) for run in runs), default=0)


def _split(text: str) -> tuple[dict[str, str], str, tuple[str, ...]]:
    """Parse the deliberately small, versioned read-back frontmatter schema."""

    lines = text.splitlines()
    metadata: dict[str, str] = {}
    seen: set[str] = set()
    errors: list[str] = []
    if not lines or lines[0].strip() != "---":
        return metadata, text, ("card has no frontmatter block",)
    closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if closing is None:
        return metadata, "", ("card has no closing frontmatter delimiter",)
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
            errors.append(f"frontmatter field {key!r} appears more than once")
            continue
        seen.add(key)
        if key not in _FRONTMATTER_FIELDS:
            errors.append(f"unknown frontmatter field {key!r}")
        if key in _QUOTED_FRONTMATTER_FIELDS:
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                errors.append(f"frontmatter field {key!r} must be a JSON double-quoted string")
                continue
            if not isinstance(decoded, str):
                errors.append(f"frontmatter field {key!r} must decode to a string")
                continue
            if key == "model" and not _safe_model_label(decoded):
                errors.append("frontmatter field 'model' must be printable, single-line text")
            metadata[key] = decoded
        else:
            metadata[key] = value
    for field in sorted(_FRONTMATTER_FIELDS - metadata.keys()):
        errors.append(f"missing frontmatter field {field!r}")
    schema = metadata.get("schema")
    if schema is not None and schema != READBACK_SCHEMA:
        errors.append(f"unsupported read-back schema {schema!r}; expected {READBACK_SCHEMA!r}")
    return metadata, "\n".join(lines[closing + 1 :]), tuple(errors)


__all__ = [
    "READBACKS_DIR",
    "READBACK_HEADING",
    "READBACK_SCHEMA",
    "PreparedReadback",
    "Readback",
    "ReadbackFinding",
    "load_readbacks",
    "planned_readback",
    "prepare_readback",
    "publish_readback",
    "publishable_article",
    "readback_conflicts",
    "readback_findings",
    "readback_path",
    "write_readback",
]
