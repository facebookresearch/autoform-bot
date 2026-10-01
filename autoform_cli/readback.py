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
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator, Mapping, NamedTuple
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
from .mathjax import TEX_MACROS, stateful_commands
from .skeleton import (
    DeclarationSkeleton,
    SkeletonReport,
    declaration_filename,
    evidence_hash_of,
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
#: Staging files beside a card start with this and never end in ``.md``, so
#: the loader never takes one for a card.
_WORK_PREFIX = ".autoform-readback-"
#: How often a publication walks to the card directory again after finding,
#: once it holds the lock, that the directory was replaced meanwhile.
_DIRECTORY_ATTEMPTS = 3
#: How long a publication waits for another one in the same card directory.
_LOCK_TIMEOUT = 10.0
#: The most bytes a card file may hold. Testimony is limited to 32 KiB, and the
#: packet a card shows has no limit of its own, so this leaves it room for
#: large kernel material. No card larger than this is built, and no file is
#: read past it: a larger one is an invalid card, whose bytes are never kept.
_CARD_MAX_BYTES = 4 * 1024 * 1024
#: A ``%`` after an even number of backslashes starts a TeX comment, which
#: silently drops the rest of its line from the typeset formula.
_TEX_COMMENT = re.compile(r"(?<!\\)(?:\\\\)*%")

#: Characters that render as nothing, or reorder the text around them, so a
#: card could say more or other than a reader sees. Unicode's general
#: categories catch most (controls, format characters such as zero-width
#: spaces and bidirectional overrides, separators, private-use and unassigned
#: code points). Spaces other than the ASCII one are blank too, and a browser
#: does not collapse them, so a run of them pushes text aside or off the card.
#: Letters and digits of the right-to-left bidirectional classes reorder the
#: characters around them. The rest are listed by code point: the
#: Default_Ignorable_Code_Point ranges of Unicode 17.0, which renderers show as
#: nothing and Python's unicodedata does not expose, and blank letters and
#: symbols outside them.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})
_HIDDEN_BIDI_CLASSES = frozenset({"R", "AL", "AN"})
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
_HIDDEN_CODE_POINTS = frozenset(
    {0x2800, 0x16FE4}  # BRAILLE PATTERN BLANK, KHITAN SMALL SCRIPT FILLER
    | {code for first, last in _DEFAULT_IGNORABLE for code in range(first, last + 1)}
)
_ALLOWED_CONTROLS = frozenset("\t\n\r")
#: How many combining marks one character may carry. The scripts written with
#: them put at most three or four on a letter; more stack over the lines above
#: and below.
_MAX_COMBINING_MARKS = 4

#: Limits a testimony must meet before the Markdown renderer reads it. Python-
#: Markdown's inline processing is superlinear in the number of spans and of
#: unmatched openers, cubic in a run of backticks, and recursive in nesting
#: depth, so a byte limit alone does not bound the work. Four characters cost
#: a scan each: a "[", which three link patterns scan from to its closing
#: bracket; an underscore that starts a word, which the emphasis patterns scan
#: from for a closing one; an asterisk, which they scan from to the next; and
#: a backslash, since each escape rebuilds the text and lengthens what the link
#: patterns scan. The byte, line, delimiter, backtick, bracket, and nesting
#: limits are several times what 120 read-backs of a real-analysis textbook
#: use: at most 5.9 KB, 62 lines, 304 math delimiters, 72 backticks in runs of
#: one, 2 brackets, and 3 columns of indentation; the asterisk limit is five
#: times the most any blueprint page at hand holds. The renderer reads no HTML,
#: and the scans for HTML a vault viewer would read are linear, so "<" needs no
#: limit: a thousand nested tags, which once overflowed the stack, are refused
#: in a hundredth of a second. Brackets cost most, 0.6 s at their limit alone.
#: At every limit at once the slowest testimony found validates in 1.6 s, two
#: thirds of the 2.3 s the slowest took on the same machine under the previous
#: renderer and limits.
TESTIMONY_MAX_BYTES = 32 * 1024
TESTIMONY_MAX_LINES = 500
TESTIMONY_MAX_MATH_DELIMITERS = 1024
TESTIMONY_MAX_BACKTICKS = 512
TESTIMONY_MAX_BACKTICK_RUN = 16
TESTIMONY_MAX_BRACKETS = 64
TESTIMONY_MAX_NESTING = 64
TESTIMONY_MAX_UNDERSCORE_OPENERS = 256
TESTIMONY_MAX_ASTERISKS = 1024
TESTIMONY_MAX_BACKSLASHES = 2048
_MATH_DELIMITER = re.compile(r"\$|\\[()\[\]]")
_BACKTICK_RUN = re.compile(r"`+")
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
#: Character references as the site's Markdown converter reads them, which
#: takes a number without its semicolon and any name with one.
_LOOSE_HTML_ENTITY = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9A-Fa-f]+;?|[A-Za-z][A-Za-z0-9]*;)")
#: A character reference by number, its leading zeros apart from its digits.
_NUMERIC_REFERENCE = re.compile(r"&#(?:([xX])0*([0-9A-Fa-f]+)|0*([0-9]+))")


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
    for path, raw, unreadable in _card_files(root):
        relative = path.relative_to(root)
        path_article_id = relative.parent.name if len(relative.parts) == 2 else None
        declaration = relative.stem
        if raw is not None:
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
                    skeleton_hash=None,
                    packet_hash=None,
                    model=None,
                    text="",
                    path=path,
                    file_hash=None if raw is None else _card_hash(raw),
                    validation_errors=(unreadable,),
                ),
            )
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
            file_hash=_card_hash(raw),
            validation_errors=tuple(errors),
        )
        _add_card(found, readback)
    return found


def _add_card(found: dict[tuple[str, str], Readback], readback: Readback) -> None:
    """Key a loaded card by its identity; a second card with that identity makes the first invalid."""

    key = (readback.article_id, readback.declaration)
    if previous := found.get(key):
        duplicate = f"multiple cards claim the same declaration identity: {previous.path} and {readback.path}"
        found[key] = replace(
            previous,
            validation_errors=tuple(dict.fromkeys((*previous.validation_errors, duplicate))),
        )
    else:
        found[key] = readback


def _card_files(root: Path) -> Iterator[tuple[Path, bytes | None, str | None]]:
    """Every regular ``*.md`` file under ``root``, with its bytes, read without following a link.

    A card is a file inside the vault; a symlink could point anywhere. The
    candidates come from a listing, which can be out of date by the time a
    file is opened, so a path-level check is not enough. Where the platform
    allows, each candidate is opened through no-follow descriptors walked
    down from ``root``, and a card or directory swapped for a symlink after
    the listing is refused rather than followed. Elsewhere (Windows) the file
    is opened first; then no component of its path may be a link, and the
    open file must be the one a no-follow stat of the path names. A file that
    cannot be read, or holds more than a card may, comes with no bytes and
    the reason instead.
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
            read = _read_card_file(root, root_descriptor, path.relative_to(root))
            if read is not None:
                yield path, *read
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _read_card_file(root: Path, root_descriptor: int | None, relative: Path) -> tuple[bytes | None, str | None] | None:
    """The bytes of the regular file at ``root / relative``, or why they could not be read.

    ``None`` if the path does not lead, without a link, to a regular file.
    """

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
        try:
            raw = _read_card_bytes(descriptor)
        except OSError as exc:
            return None, f"card cannot be read: {exc.strerror}"
    except PermissionError as exc:
        return None, f"card cannot be read: {exc.strerror}"
    except OSError:
        return None
    finally:
        for held in reversed(opened):
            os.close(held)
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

    def __post_init__(self) -> None:
        if len(self.content.encode("utf-8")) > _CARD_MAX_BYTES:
            raise ValueError(
                f"read-back card for {self.declaration} is over the {_CARD_MAX_BYTES}-byte limit for a card file"
            )


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
    only when it names the hash of that content's bytes, and a card that names
    a hash replaces only that content, not a missing card. A card identical to
    the one filed is no conflict, since publishing it writes nothing. Each
    conflict names the hash to pass. Publishing still checks each card, since
    another writer can act in between. Where publishing is refused, so is
    this check.
    """

    _require_publication_support()
    conflicts: list[str] = []
    for card in cards:
        before = _existing_card_hash(card.blueprint, card.article_id, card.path)
        if before == _card_hash(card.content.encode("utf-8")):
            continue
        if conflict := _card_conflict(card.path, before, card.expected_card_hash):
            conflicts.append(f"{card.declaration}: {conflict}")
    return conflicts


def publish_readback(prepared: PreparedReadback) -> Path:
    """Publish a prepared card under the same compare-and-swap rule.

    The card's directory is reached from the blueprint without following
    links and locked, so publications into it happen one at a time. Since it
    can be replaced between the walk and the lock, the locked directory must
    be the one a fresh walk reaches; if not, the walk starts over, a few
    times before the write is refused. The locked descriptor never leaves
    this function, so no interruption can strand the lock with a descriptor
    nothing will close.
    """

    _require_publication_support()
    blueprint, article_id, path = prepared.blueprint, prepared.article_id, prepared.path
    for _ in range(_DIRECTORY_ATTEMPTS):
        directory = _open_card_directory(blueprint, article_id, create=True)
        try:
            _lock_card_directory(directory, path)
            held = os.fstat(directory)
            if _card_directory_identity(blueprint, article_id) == (held.st_dev, held.st_ino):
                _publish_card(directory, path, prepared.content, expected_card_hash=prepared.expected_card_hash)
                return path
        finally:
            _release_card_directory(directory)
    raise ValueError(f"read-back directory {path.parent} kept being replaced while it was locked; retry later")


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
#: A fence that opens a Mermaid block in a CommonMark viewer of the vault,
#: which also reads one after blockquote and list markers on its line, where
#: the renderer reads only text. Indentation is not weighed, so such a fence
#: in an indented code block is refused too.
_MERMAID_FENCE = re.compile(
    r"^(?:[ \t>]|[-+*](?=[ \t])|\d{1,9}[.)](?=[ \t]))*(?:`{3,}|~{3,})[ \t]*mermaid(?:[ \t]|$)",
    re.MULTILINE | re.IGNORECASE,
)


#: Commands that set a letter, so testimony showing only these still shows one.
_TEX_LETTERS = frozenset(
    r"""
    \alpha \beta \gamma \delta \epsilon \varepsilon \zeta \eta \theta \vartheta \iota \kappa
    \varkappa \lambda \mu \nu \xi \omicron \pi \varpi \rho \varrho \sigma \varsigma \tau \upsilon
    \phi \varphi \chi \psi \omega \digamma \Gamma \Delta \Theta \Lambda \Xi \Pi \Sigma \Upsilon
    \Phi \Psi \Omega \aleph \beth \gimel \daleth \eth \ell \hbar \hslash \imath \jmath \wp \Re
    \Im \Bbbk
    """.split()
)


class _Tex(NamedTuple):
    """How MathJax sets one TeX command.

    ``kind`` is ``"glyph"`` for a command that sets a symbol, ``"operator"``
    for a large or named operator, which ``\\limits`` may follow, ``"space"``
    for horizontal space ``width`` mu wide (an eighteenth of an em; negative
    space pulls the next symbol back), ``"style"`` for a command that changes
    the size of what follows but sets nothing itself, ``"size"`` for one that
    sizes a delimiter, or the name of a command the layout model reads itself.
    ``arguments`` spells what follows the command, in order: ``g`` where it
    sets its own symbol, ``m`` an argument that must show something, ``e`` one
    that may be empty, ``t`` an argument set as text that must show
    something, ``o`` an optional argument in brackets, ``*`` an optional star,
    and ``d`` a delimiter.
    """

    kind: str
    arguments: str = "g"
    width: float = 0.0


def _tex(names: str, kind: str = "glyph", arguments: str = "g", width: float = 0.0) -> dict[str, _Tex]:
    return dict.fromkeys(names.split(), _Tex(kind, arguments, width))


#: Relations, which alone may follow ``\not``, which strikes through the next
#: symbol whatever it is.
_TEX_RELATIONS = frozenset(
    r"""
    = < > \lt \gt \le \leq \ge \geq \ne \neq \ll \gg \leqslant \geqslant \nleq \ngeq \lneq \gneq
    \lneqq \gneqq \lvertneqq \lesssim \gtrsim \lessapprox \gtrapprox \lessdot \gtrdot \equiv
    \approx \approxeq \cong \ncong \sim \nsim \simeq \backsim \backsimeq \eqsim \asymp \propto
    \varpropto \doteq \triangleq \prec \preceq \succ \succeq \precsim \succsim \precapprox
    \succapprox \precneqq \succneqq \nprec \nsucc \npreceq \nsucceq \in \ni \notin \owns \subset
    \subseteq \supset \supseteq \subseteqq \supseteqq \subsetneq \supsetneq \varsubsetneq
    \nsubseteq \nsupseteq \Subset \Supset \sqsubseteq \sqsupseteq \mid \nmid \shortmid \nshortmid
    \parallel \nparallel \shortparallel \perp \between \pitchfork \smile \frown \smallsmile
    \smallfrown \bowtie \Join \vdash \dashv \models \vDash \Vdash \nvdash \nvDash \nVdash \nVDash
    \triangleleft \trianglelefteq \triangleright \trianglerighteq \vartriangleleft
    \vartriangleright \ntriangleleft \ntriangleright \ntrianglelefteq \ntrianglerighteq
    \to \gets \mapsto \longmapsto \rightarrow \leftarrow \leftrightarrow \Rightarrow \Leftarrow
    \Leftrightarrow \longrightarrow \longleftarrow \longleftrightarrow \Longrightarrow
    \Longleftarrow \Longleftrightarrow \iff \implies \impliedby \hookrightarrow \hookleftarrow
    \twoheadrightarrow \uparrow \downarrow \updownarrow \Uparrow \Downarrow \Updownarrow \nearrow
    \searrow \swarrow \nwarrow \nleftarrow \nrightarrow \nLeftarrow \nRightarrow \nleftrightarrow
    \nLeftrightarrow \leftleftarrows \rightrightarrows \upuparrows \downdownarrows
    \circlearrowleft \circlearrowright \curvearrowleft \curvearrowright \Lsh \Rsh \looparrowleft
    \looparrowright \leadsto \rightsquigarrow \leftrightsquigarrow \multimap \rightleftharpoons
    \upharpoonright \restriction
    """.split()
)
#: What may follow ``\left``, ``\right``, ``\middle``, and ``\big`` and its
#: kin; ``.`` is the empty delimiter, which sets nothing.
_TEX_DELIMITERS = frozenset(
    r"""
    ( ) [ ] | / < > . \{ \} \| \langle \rangle \lfloor \rfloor \lceil \rceil \vert \Vert \lvert
    \rvert \lVert \rVert \lbrace \rbrace \lbrack \rbrack \lgroup \rgroup \lmoustache \rmoustache
    \uparrow \downarrow \updownarrow \Uparrow \Downarrow \Updownarrow \backslash
    """.split()
)
#: The TeX testimony may use: the notation statements need, out of what the
#: site's pinned MathJax 3.2.2 defines in the base and ams packages, the only
#: ones javascripts/mathjax.js loads. Nothing listed moves, hides, colours,
#: boxes, labels, or numbers content, and nothing defines a macro; what sizes
#: or spaces it is measured by :class:`_TexLayout`. Any other command or
#: environment is refused by name, so extending this is a deliberate edit, to
#: be checked against that MathJax version.
_TESTIMONY_TEX: dict[str, _Tex] = {
    **_tex(" ".join(_TEX_LETTERS)),
    **_tex(" ".join(name for name in _TEX_RELATIONS if name.startswith("\\"))),
    **_tex(r"\{ \} \| \# \$ \% \& \_"),
    # Symbols.
    **_tex(
        r"""
        \infty \partial \nabla \emptyset \varnothing \prime \backprime \forall \exists \nexists
        \neg \lnot \top \bot \angle \measuredangle \sphericalangle \triangle \vartriangle
        \blacktriangle \bigtriangleup \bigtriangledown \backslash \complement \therefore \because
        \colon \cdotp \ldotp \ldots \cdots \vdots \ddots \dots \dotsc \dotsb \Box \square
        \blacksquare \Diamond \lozenge \bigstar \checkmark \flat \sharp \natural \surd \mho \Finv
        \Game \S \yen \circledR \maltese
        """
    ),
    # Binary operators.
    **_tex(
        r"""
        \pm \mp \times \div \cdot \centerdot \ast \star \circ \bullet \cap \cup \Cap \Cup \setminus
        \smallsetminus \wedge \vee \land \lor \barwedge \veebar \doublebarwedge \curlywedge
        \curlyvee \oplus \ominus \otimes \oslash \odot \circledast \circleddash \dotplus \sqcap
        \sqcup \uplus \amalg \dagger \ddagger \diamond \intercal \wr \divideontimes \ltimes \rtimes
        \leftthreetimes \rightthreetimes \lhd \rhd \unlhd \unrhd \boxplus \boxminus \boxtimes
        \boxdot \bmod
        """
    ),
    **_tex(r"\mod \pmod", arguments="ge"),
    # Large and named operators.
    **_tex(
        r"""
        \sum \prod \coprod \int \iint \iiint \oint \bigcup \bigcap \bigoplus \bigotimes \bigvee
        \bigwedge \bigsqcup \biguplus \bigodot \lim \liminf \limsup \varinjlim \varprojlim \sup
        \inf \max \min \sin \cos \tan \sec \csc \cot \sinh \cosh \tanh \coth \arcsin \arccos
        \arctan \log \ln \lg \exp \det \dim \ker \deg \gcd \hom \arg \Pr
        """,
        "operator",
    ),
    r"\operatorname": _Tex("operator", "*m"),
    r"\mathop": _Tex("operator", "m"),
    **_tex(r"\limits \nolimits", "limits", ""),
    # Delimiters and their sizes.
    **_tex(
        r"""
        \langle \rangle \lfloor \rfloor \lceil \rceil \vert \Vert \lvert \rvert \lVert \rVert
        \lbrace \rbrace \lbrack \rbrack \lgroup \rgroup \lmoustache \rmoustache \ulcorner \urcorner
        \llcorner \lrcorner
        """
    ),
    **_tex(r"\left", "left", "d"),
    **_tex(r"\right", "right", "d"),
    **_tex(r"\middle", "middle", "d"),
    **_tex(
        r"\big \Big \bigg \Bigg \bigl \bigr \Bigl \Bigr \biggl \biggr \Biggl \Biggr \bigm \Bigm \biggm \Biggm",
        "size",
        "d",
    ),
    r"\not": _Tex("not", ""),
    # Fractions, roots, arrows with labels, accents, and stacking.
    **_tex(r"\frac \dfrac \tfrac \binom \dbinom \tbinom \overset \underset \stackrel", arguments="mm"),
    r"\sqrt": _Tex("glyph", "oge"),
    **_tex(r"\xrightarrow \xleftarrow", arguments="oeg"),
    **_tex(r"\substack", arguments="m"),
    **_tex(
        r"""
        \hat \bar \tilde \vec \dot \ddot \check \breve \acute \grave \mathring \widehat
        \widetilde \overline \underline \overrightarrow \overleftarrow \overbrace \underbrace
        """,
        arguments="m",
    ),
    # Fonts, classes, and text.
    **_tex(
        r"""
        \mathbb \mathcal \mathfrak \mathscr \mathrm \mathbf \mathsf \mathit \mathtt \pmb \mathrel
        \mathbin \mathord
        """,
        arguments="m",
    ),
    **_tex(r"\text \textrm \textbf \textit \texttt \textsf", arguments="t"),
    **_tex(r"\displaystyle \textstyle \scriptstyle \scriptscriptstyle", "style", ""),
    # Spaces, rows, and environments.
    **_tex(r"\, \thinspace", "space", "", 3),
    r"\:": _Tex("space", "", 4),
    r"\;": _Tex("space", "", 5),
    "\\ ": _Tex("space", "", 4.5),
    "\\\n": _Tex("space", "", 4.5),
    r"\enspace": _Tex("space", "", 9),
    r"\quad": _Tex("space", "", 18),
    r"\qquad": _Tex("space", "", 36),
    **_tex(r"\! \negthinspace", "space", "", -3),
    "\\\\": _Tex("rows", ""),
    r"\begin": _Tex("begin", ""),
    r"\end": _Tex("end", ""),
}
#: Environments testimony may use, all of them rows of cells.
_TEX_ENVIRONMENTS = frozenset(
    {"cases", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "aligned", "gathered"}
)
#: The environments MathJax 3.2.2 reads a bracket after, for where their rows
#: sit: it applies ``[t]``, ``[b]``, or ``[c]`` and shows nothing else written
#: there.
_TEX_ALIGNABLE = frozenset({"aligned", "gathered", "array"})
#: The environments MathJax 3.2.2 sets once in a formula: a second, even one
#: nested in the first, gets an error in place of the formula.
_TEX_EQUATIONS = frozenset({"align", "align*", "gather", "gather*"})
#: The columns ``\begin{array}`` may take. MathJax draws ``|`` and ``:`` as
#: rules between columns and drops any other character without showing it.
_TEX_ARRAY_COLUMNS = re.compile(r"\s*\{ *[lcr][lcr ]{0,31}\}")
#: Commands MathJax 3.2.2 reads as a function name waiting for what follows,
#: or expands into several items. A superscript or subscript that is one of
#: them alone gets an error in place of the formula, or takes only the first
#: item, so it must be braced, as ``x^{\sin}`` is.
_TEX_BRACED_SCRIPTS = frozenset(
    r"""
    \arcsin \arccos \arctan \arg \cos \cosh \cot \coth \csc \deg \dim \exp \hom \ker \lg \ln \log \sec
    \sin \sinh \tan \tanh \mathop \dots \varinjlim \varprojlim \varliminf \varlimsup \idotsint \iff
    \implies \impliedby \pmb \mod \pmod
    """.split()
)
#: The characters past ASCII MathJax 3.2.2 sets in a formula, outside
#: ``\text``: those in the ranges of its operator dictionary
#: (OperatorDictionary.RANGES), merged here. Any other gets an error in place
#: of the formula.
_TEX_CHARACTER_RANGES = (
    (0x00A0, 0x024F), (0x02B0, 0x1A20), (0x1AB0, 0x209F), (0x2100, 0x23FF), (0x2460, 0x2DE0), (0x2E00, 0x2FDF),
    (0x2FF0, 0xA49F), (0xA4D0, 0xD7FF), (0xF900, 0x1D25F), (0x1D360, 0x1D37F), (0x1D400, 0x1D7FF),
    (0x1DF00, 0x1F9FF), (0x20000, 0x2FA1F),
)
#: How long a formula may be, in UTF-16 code units, so a character past the
#: Basic Multilingual Plane counts twice. MathJax 3.2.2 refuses a formula once
#: expanding a command makes what is left to read longer than 5120 (its
#: ``maxBuffer``); ``\pmb``, which draws its argument twice, expands the most,
#: so 2048 keeps every formula under it with room to spare. ``\pmb`` inside
#: ``\pmb`` would double again at every level, and is refused.
_TEX_MAX_LENGTH = 2048
#: How deep one formula may nest groups, arguments, delimiters, and
#: environments, and scripts on scripts: more than twice what statements use
#: (6 and 3 deep), and far below the depth near 200 at which MathJax 3.2.2
#: overflows its stack.
_TEX_MAX_DEPTH = 16
_TEX_MAX_SCRIPT_DEPTH = 8
#: What one formula may spend on content that pushes the rest aside or down:
#: positive horizontal space, in mu (an eighteenth of an em), ``&`` per row,
#: ``\\`` per environment and per formula, and empty cells. Each is several
#: times what statements use; the 873 formulas of the vault and ArkLib
#: blueprints at hand use at most 3.3 em of space, one ``&`` in a row, two
#: ``\\``, and no empty cell. Ten columns is amsmath's own limit for a matrix.
#: An empty row shows nothing at all, so none is allowed. Space in the rows of
#: an environment counts once per column, as much as its widest cell holds,
#: since the rows stack rather than run on. A testimony may spend eight times
#: what one formula may, however its formulas split it.
_TEX_MAX_SPACING = 8 * 18
_TEX_MAX_TESTIMONY_SPACING = 64 * 18
_TEX_MAX_COLUMNS = 9
_TEX_MAX_ROWS = 16
_TEX_MAX_FORMULA_ROWS = 32
_TEX_MAX_EMPTY_CELLS = 16
#: One negative thin space between two symbols tightens ``\int\! f``; any
#: more, net of the space beside it, slides one symbol over the other.
_TEX_MIN_RUN = -3
_TEX_TOKEN = re.compile(r"\\(?:[A-Za-z]+|.)?|\s+|.", re.DOTALL)
#: Which token closes each kind of list :class:`_TexLayout` reads.
_TEX_CLOSERS = {"group": "}", "substack": "}", "left": r"\right", "environment": r"\end"}
#: What cannot start a script, and the commands MathJax will not take as one.
_TEX_NOT_SCRIPTS = frozenset({"}", "&", "^", "_", "'", "’"})
#: The two characters MathJax sets as a prime.
_TEX_PRIMES = frozenset({"'", "’"})
_TEX_UNSCRIPTED = frozenset({"style", "not", "begin", "limits", "middle", "right", "end", "rows"})
_TEX_ENVIRONMENT = re.compile(r"\s*\{([^{}\\]{0,64})\}")
#: ``\\[<dimension>]`` spaces rows apart, or with a negative dimension draws
#: one over another; MathJax reads the bracket only right after the ``\\``.
_TEX_ROW_SPACING = re.compile(r"\*?\[")
#: The escapes ``\text`` and its kin read, rather than show as typed.
_TEX_TEXT_ESCAPE = re.compile(r"\\[${}\\]")
_TEX_DOUBLE_INTEGRAL = re.compile(r"\\int\s*(?:\\!\s*){2,}\\int")
#: What MathJax, which reads the escapes ``\\`` and ``\$`` first, takes for
#: either end of a formula in text: the delimiters javascripts/mathjax.js
#: configures, environments, and references. In a formula they are typeset as
#: one, or end it early.
_TEX_TEXT_DELIMITER = re.compile(r"\\\\|\\\$|(\$|\\[()\[\]]|\\begin\s*\{|\\(?:eq)?ref\s*\{)")
_TEX_FORMULA_DELIMITERS = frozenset({"$", r"\(", r"\)", r"\[", r"\]"})
_TEX_ENVIRONMENT_NAME = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
_TEX_CONTROL_SEQUENCE = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)

_TEX_BRACES = "unbalanced TeX braces are not allowed"
_TEX_LEFT_RIGHT = "unbalanced \\left and \\right are not allowed"
_TEX_MIDDLE = "\\middle is allowed only between \\left and \\right"
_TEX_LIMITS = "\\limits and \\nolimits are allowed only after a large or named operator"
_TEX_NOT = "\\not is allowed only before a relation such as =, \\in, or \\le"
_TEX_SCRIPTS = "a second TeX superscript or subscript on one symbol is not allowed: use braces"
_TEX_MISPLACED = "TeX & and \\\\ are allowed only between the cells and rows of an environment"
_TEX_UNBALANCED_ENVIRONMENT = "TeX \\begin and \\end that do not match are not allowed"
_TEX_ARRAY = "TeX \\begin{array} is allowed only with its columns as l, c, and r in braces, such as {lcr}"
_TEX_ALIGNMENT = (
    "a bracket after TeX \\begin{aligned}, \\begin{gathered}, or \\begin{array} other than [t], [b], or [c] "
    "is not allowed: MathJax does not show what it holds; write {} before a bracket that starts the first row"
)
_TEX_TEXT = (
    "TeX commands and formulas inside \\text are not allowed: they are shown as typed or typeset apart; "
    "write \\$, \\{, \\}, or \\\\ for the character"
)


class _TexAbort(Exception):
    """Stops reading a formula nested deeper than the layout model follows."""


@dataclass
class _TexRows:
    """The rows of one environment or ``\\substack`` being read: how many
    rows have ended, how many cells the current row has ended, how many
    symbols the formula had set when the current cell and row began, its
    spacing when the environment began, and the most any cell of each column
    has spent."""

    cell: int
    row: int
    spacing: float
    widths: list[float]
    rows: int = 0
    columns: int = 0


class _TexLayout:
    """Formulas read the way MathJax 3.2.2 lays them out, and what in them it
    would refuse to set, is not on the allowlist, or hides, overlaps, or
    spreads content.

    It follows TeX's grammar as far as the allowlist needs: groups,
    arguments, scripts, delimiters, and the rows and cells of environments.
    Every token is a symbol, a space of signed width, a command that sets
    nothing (a style, an empty delimiter, an empty group or script), or
    structure. An argument shows something only if a symbol is set inside it.
    Spacing is a signed run, in mu, from one symbol to the next, read in
    source order across groups, arguments, cells, and commands that set
    nothing, so a run below one negative thin space slides a symbol over its
    neighbour wherever the two sit. A run never carries from one formula to
    the next, but the spacing of every formula counts toward the testimony's.
    """

    def __init__(self) -> None:
        self.errors: dict[str, None] = {}
        self.unlisted: dict[str, None] = {}
        self.empty: dict[str, None] = {}
        self.missing: dict[str, None] = {}
        self.delimiters: dict[str, None] = {}
        self.braced: dict[str, None] = {}
        self.unset: dict[str, None] = {}
        self.stray: dict[str, None] = {}
        self.overlap = self.double_integral = self.marks = False
        self.total_spacing = 0.0

    def read(self, tex: str) -> None:
        """Read one formula, ``tex`` without its delimiters."""

        if len(tex.encode("utf-16-le")) // 2 > _TEX_MAX_LENGTH:
            self.errors[
                f"TeX formulas over {_TEX_MAX_LENGTH} characters are not allowed: MathJax may refuse to set them"
            ] = None
            return
        self.tex = tex
        self.tokens = [(match.start(), match.group()) for match in _TEX_TOKEN.finditer(tex)]
        self.tokens = [(start, token) for start, token in self.tokens if not token.isspace()]
        self.index = 0
        self.limit = len(self.tokens)
        self.glyphs = self.script_depth = 0
        self.depth = -1  # the formula itself is not nested
        self.rows = self.empty_cells = self.empty_rows = 0
        self.run = self.spacing = 0.0
        self.new_atom()
        self.primed = self.doubled = self.equation = False
        try:
            self.read_list("formula", None)
        except _TexAbort as abort:
            self.errors[str(abort)] = None
            return
        self.overlap = self.overlap or self.run < _TEX_MIN_RUN
        self.total_spacing += self.spacing
        if not self.glyphs:
            self.errors["TeX formulas that show nothing are not allowed"] = None
        if self.spacing > _TEX_MAX_SPACING:
            self.errors[
                f"TeX spacing over {_TEX_MAX_SPACING // 18} em in one formula is not allowed: "
                "it pushes symbols apart or out of view"
            ] = None
        if self.rows > _TEX_MAX_FORMULA_ROWS:
            self.errors[f"more than {_TEX_MAX_FORMULA_ROWS} TeX \\\\ in one formula are not allowed"] = None
        if self.empty_cells > _TEX_MAX_EMPTY_CELLS:
            self.errors[f"more than {_TEX_MAX_EMPTY_CELLS} empty TeX cells in one formula are not allowed"] = None
        if self.empty_rows:
            self.errors["empty TeX rows are not allowed: they push what follows down without showing anything"] = None
        self.double_integral = self.double_integral or bool(_TEX_DOUBLE_INTEGRAL.search(tex))

    def messages(self) -> list[str]:
        """What every formula read so far holds that is not allowed."""

        named = [
            ("TeX outside the read-back allowlist is not allowed: ", self.unlisted, ""),
            ("TeX arguments that show nothing are not allowed: ", self.empty, ""),
            ("TeX commands missing an argument are not allowed: ", self.missing, ""),
            ("TeX delimiters MathJax does not accept are not allowed after: ", self.delimiters, ""),
            (
                "TeX superscripts and subscripts that are one of these alone are not allowed: ",
                self.braced,
                "; MathJax refuses them or sets only part, so brace them, as in x^{\\sin}",
            ),
            (
                "characters MathJax cannot set in a formula are not allowed: ",
                self.unset,
                "; write them in \\text{...} or outside the formula",
            ),
        ]
        errors = [prefix + ", ".join(sorted(names)) + suffix for prefix, names, suffix in named if names]
        if self.marks:
            errors.append(
                "combining marks in a formula are not allowed: MathJax sets each apart from the symbol before it; "
                "write an accent such as \\acute{x}, or the character already composed"
            )
        if self.total_spacing > _TEX_MAX_TESTIMONY_SPACING:
            errors.append(
                f"TeX spacing over {_TEX_MAX_TESTIMONY_SPACING // 18} em in one testimony is not allowed: "
                "it pushes symbols apart or out of view"
            )
        if self.stray:
            errors.append(
                "math delimiters the renderer did not read as a formula are not allowed: "
                + ", ".join(sorted(self.stray))
                + "; write \\$ for a dollar sign, and put displayed math in a paragraph of its own"
            )
        if self.overlap:
            errors.append(
                "repeated negative TeX spacing is not allowed: it slides symbols over one another"
                + ("; write \\iint for a double integral" if self.double_integral else "")
            )
        return errors + list(self.errors)

    def peek(self) -> str | None:
        return self.tokens[self.index][1] if self.index < self.limit else None

    def skip_to(self, position: int) -> None:
        while self.index < self.limit and self.tokens[self.index][0] < position:
            self.index += 1

    def new_atom(self, operator: bool = False) -> None:
        """Start a new symbol for scripts and ``\\limits`` to attach to."""

        self.operator = operator
        self.superscript = 0  # 1 after primes alone, 2 after ^
        self.subscript = False

    def glyph(self) -> None:
        self.overlap = self.overlap or self.run < _TEX_MIN_RUN
        self.run = 0.0
        self.glyphs += 1

    def space(self, width: float) -> None:
        self.run += width
        self.spacing += max(width, 0)

    def read_list(self, context: str, rows: _TexRows | None) -> str | None:
        """Read items up to the token that closes ``context`` and return it,
        or return ``None`` at the end of the formula or optional argument."""

        self.depth += 1
        if self.depth > _TEX_MAX_DEPTH:
            raise _TexAbort(f"TeX nested more than {_TEX_MAX_DEPTH} deep is not allowed")
        closer = _TEX_CLOSERS.get(context)
        while (token := self.peek()) is not None:
            self.index += 1
            if token == closer:
                self.depth -= 1
                return token
            self.item(token, context, rows)
        self.depth -= 1
        return None

    def item(self, token: str, context: str, rows: _TexRows | None, alone: bool = False) -> None:
        """Read the item ``token`` starts. ``alone`` marks an argument given
        without braces, which MathJax reads by itself."""

        primed, self.primed = self.primed, False
        if token == "{":
            self.group("group")
        elif token == "}":
            self.errors[_TEX_BRACES] = None
        elif token in {"^", "_"}:
            self.script(token)
        elif token in _TEX_PRIMES:
            if self.superscript and not primed:
                self.errors[_TEX_SCRIPTS] = None
            self.superscript = self.superscript or 1
            self.operator, self.primed = False, True
            self.glyph()
        elif token == "&":
            if context == "environment" and rows is not None:
                self.cell(rows)
            else:
                self.errors[_TEX_MISPLACED] = None
            self.new_atom()
        elif token == "~":
            self.new_atom()
            self.space(4.5)
        elif token in _TEX_FORMULA_DELIMITERS:
            self.stray[token] = None
        elif token.startswith("\\"):
            self.command(token, context, rows, alone)
        else:
            if token == "#":
                self.unlisted[token] = None
            elif unicodedata.category(token) in {"Mn", "Me"}:
                self.marks = True
            elif ord(token) > 0x7F and not any(low <= ord(token) <= high for low, high in _TEX_CHARACTER_RANGES):
                self.unset[f"U+{ord(token):04X} {unicodedata.name(token, '')}".strip()] = None
            self.new_atom()
            self.glyph()

    def command(self, token: str, context: str, rows: _TexRows | None, alone: bool) -> None:
        entry = _TESTIMONY_TEX.get(token)
        if entry is None:
            shown = token[1:2].isprintable() and not token[1:2].isspace()
            self.unlisted[token if shown else f"\\U+{ord(token[1]):04X}"] = None
            self.new_atom()
            self.glyph()
            return
        kind = entry.kind
        if alone and (entry.arguments.strip("g") or kind in {"left", "right", "middle", "begin", "end", "rows"}):
            self.missing[token] = None
        elif kind == "limits":
            if alone or not self.operator:
                self.errors[_TEX_LIMITS] = None
        elif kind == "not":
            if alone or self.peek() not in _TEX_RELATIONS:
                self.errors[_TEX_NOT] = None
        elif kind == "rows":
            if _TEX_ROW_SPACING.match(self.tex, self.tokens[self.index - 1][0] + 2):
                self.errors[
                    "TeX row spacing after \\\\ is not allowed: it can draw rows over one another; "
                    "write {} before a bracket that starts a row"
                ] = None
            if context in {"formula", "environment", "substack"}:
                self.row(rows)
            else:
                self.errors[_TEX_MISPLACED] = None
            self.new_atom()
        elif kind == "right":
            self.errors[_TEX_LEFT_RIGHT] = None
            self.delimiter(token)
        elif kind == "middle":
            if context != "left":
                self.errors[_TEX_MIDDLE] = None
            self.new_atom()
            self.delimiter(token)
        elif kind == "end":
            self.errors[_TEX_UNBALANCED_ENVIRONMENT] = None
            self.environment_name(r"\end")
        else:
            self.new_atom(kind == "operator")
            if kind == "space":
                self.space(entry.width)
            elif kind == "left":
                self.left()
            elif kind == "begin":
                self.environment()
            else:
                self.arguments(token, entry.arguments)
            self.new_atom(kind == "operator")

    def group(self, context: str) -> None:
        rows = _TexRows(self.glyphs, self.glyphs, self.spacing, []) if context == "substack" else None
        if self.read_list(context, rows) is None:
            self.errors[_TEX_BRACES] = None
        if rows is not None:
            self.end_rows(rows)
        self.new_atom()

    def script(self, token: str) -> None:
        if token == "^":
            if self.superscript == 2:
                self.errors[_TEX_SCRIPTS] = None
            self.superscript = 2
        else:
            if self.subscript:
                self.errors[_TEX_SCRIPTS] = None
            self.subscript = True
        atom = self.operator, self.superscript, self.subscript
        following = self.peek()
        entry = _TESTIMONY_TEX.get(following or "")
        if (
            following is None
            or following in _TEX_NOT_SCRIPTS
            or (entry is not None and (entry.kind in _TEX_UNSCRIPTED or following == r"\substack"))
        ):
            self.missing[token] = None
        elif following in _TEX_BRACED_SCRIPTS:
            self.braced[following] = None
        else:
            self.script_depth += 1
            if self.script_depth > _TEX_MAX_SCRIPT_DEPTH:
                raise _TexAbort(f"TeX scripts nested more than {_TEX_MAX_SCRIPT_DEPTH} deep are not allowed")
            self.index += 1
            self.item(following, "script", None)
            self.script_depth -= 1
        self.operator, self.superscript, self.subscript = atom

    def arguments(self, command: str, spec: str) -> None:
        doubled = self.doubled
        if command == r"\pmb":
            if doubled:
                self.errors[
                    "TeX \\pmb inside \\pmb is not allowed: MathJax draws its argument again at every level"
                ] = None
            self.doubled = True
        for kind in spec:
            if kind == "g":
                self.glyph()
            elif kind == "*":
                if self.peek() == "*":
                    self.index += 1
            elif kind == "o":
                self.optional(command)
            elif kind == "d":
                self.delimiter(command)
            elif kind == "t":
                self.text(command)
            else:
                self.argument(command, shows=kind == "m")
        self.doubled = doubled

    def argument(self, command: str, shows: bool) -> None:
        token = self.peek()
        if token is None or token in {"}", "&", "^", "_"}:
            self.missing[command] = None
            return
        glyphs = self.glyphs
        self.index += 1
        if token == "{":
            self.group("substack" if command == r"\substack" else "group")
        else:
            self.item(token, "argument", None, alone=True)
        if shows and self.glyphs == glyphs:
            self.empty[command] = None

    def optional(self, command: str) -> None:
        """Read an argument in brackets, which MathJax ends at the first ``]``
        outside braces."""

        if self.peek() != "[":
            return
        depth = 0
        for index in range(self.index + 1, self.limit):
            token = self.tokens[index][1]
            if token == "]" and not depth:
                limit, self.limit, self.index = self.limit, index, self.index + 1
                self.read_list("optional", None)
                self.index, self.limit = index + 1, limit
                return
            depth += {"{": 1, "}": -1}.get(token, 0)
            if depth < 0:
                break
        self.missing[command] = None

    def delimiter(self, command: str) -> None:
        token = self.peek()
        if token not in _TEX_DELIMITERS:
            self.delimiters[command] = None
            return
        self.index += 1
        if token != ".":
            self.glyph()

    def left(self) -> None:
        self.delimiter(r"\left")
        if self.read_list("left", None) is None:
            self.errors[_TEX_LEFT_RIGHT] = None
        else:
            self.delimiter(r"\right")

    def text(self, command: str) -> None:
        """Read an argument set as text: shown as typed, but for a few escapes
        and for formulas inside it, with each space in it as wide as ``\\ ``."""

        token = self.peek()
        if token is None or token in {"}", "&", "^", "_"}:
            self.missing[command] = None
            return
        self.index += 1
        content = token
        if token == "{":
            start = position = self.tokens[self.index - 1][0] + 1
            depth = 1
            while depth and position < len(self.tex):
                character = self.tex[position]
                position += 2 if character == "\\" else 1
                depth += {"{": 1, "}": -1}.get(character, 0)
            if depth:
                self.errors[_TEX_BRACES] = None
                self.index = self.limit
                return
            content = self.tex[start : position - 1]
            self.skip_to(position)
        if re.search(r"[\\$]", _TEX_TEXT_ESCAPE.sub("", content)):
            self.errors[_TEX_TEXT] = None
        shown = content.strip()
        if content[:1].isspace():
            self.space(4.5)
        if not shown:
            self.empty[command] = None
            return
        self.glyph()
        self.spacing += 4.5 * sum(len(gap) - 1 for gap in re.findall(r"\s+", shown))
        if content[-1:].isspace():
            self.space(4.5)

    def environment(self) -> None:
        name = self.environment_name(r"\begin")
        if name is None:
            return
        if name in _TEX_EQUATIONS:
            if self.equation:
                self.errors[
                    "more than one TeX align or gather environment in a formula is not allowed: MathJax refuses "
                    "the formula; write aligned or gathered"
                ] = None
            self.equation = True
        if name in _TEX_ALIGNABLE:
            self.alignment(name)
        if name == "array":
            match = _TEX_ARRAY_COLUMNS.match(self.tex, self.tokens[self.index][0]) if self.index < self.limit else None
            if match is None:
                self.errors[_TEX_ARRAY] = None
            else:
                self.skip_to(match.end())
        rows = _TexRows(self.glyphs, self.glyphs, self.spacing, [])
        if self.read_list("environment", rows) is None or self.environment_name(r"\end") != name:
            self.errors[_TEX_UNBALANCED_ENVIRONMENT] = None
        self.end_rows(rows)

    def alignment(self, name: str) -> None:
        """Read the bracket after ``\\begin{name}``, which MathJax 3.2.2 ends
        at the first ``]`` outside braces and applies only as ``t``, ``b``, or
        ``c``, spaces aside."""

        if self.peek() != "[":
            return
        depth = 0
        for index in range(self.index + 1, self.limit):
            token = self.tokens[index][1]
            if token == "]" and not depth:
                held = self.tex[self.tokens[self.index][0] + 1 : self.tokens[index][0]]
                if held.strip(" \t\n\r") not in {"", "t", "b", "c"}:
                    self.errors[_TEX_ALIGNMENT] = None
                self.index = index + 1
                return
            depth += {"{": 1, "}": -1}.get(token, 0)
            if depth < 0:
                break
        self.missing[rf"\begin{{{name}}}"] = None

    def environment_name(self, command: str) -> str | None:
        """Read the name after ``command``, ``\\begin`` or ``\\end``, which
        has just been read."""

        match = _TEX_ENVIRONMENT.match(self.tex, self.tokens[self.index - 1][0] + len(command))
        if match is None:
            self.missing[command] = None
            return None
        self.skip_to(match.end())
        if match.group(1) not in _TEX_ENVIRONMENTS:
            self.unlisted[f"{command}{{{match.group(1)}}}"] = None
        return match.group(1)

    def cell(self, rows: _TexRows) -> None:
        """End a cell at ``&``."""

        self.empty_cells += self.glyphs == rows.cell
        rows.cell = self.glyphs
        self.measure(rows)
        rows.columns += 1
        if rows.columns > _TEX_MAX_COLUMNS:
            self.errors[f"more than {_TEX_MAX_COLUMNS} TeX & in one row are not allowed"] = None

    def row(self, rows: _TexRows | None) -> None:
        """End a row at ``\\\\``. At the top of a formula MathJax 3.2.2 sets
        nothing for one, but it counts toward the formula's rows."""

        self.rows += 1
        if rows is None:
            return
        self.empty_cells += self.glyphs == rows.cell and rows.columns > 0
        self.empty_rows += self.glyphs == rows.row
        rows.cell = rows.row = self.glyphs
        self.measure(rows)
        rows.columns = 0
        rows.rows += 1
        if rows.rows > _TEX_MAX_ROWS:
            self.errors[f"more than {_TEX_MAX_ROWS} TeX \\\\ in one environment are not allowed"] = None

    def end_rows(self, rows: _TexRows) -> None:
        """End the last row of ``rows``, which, empty and after a ``\\\\``,
        MathJax does not set."""

        if rows.columns:
            self.empty_cells += self.glyphs == rows.cell
            self.empty_rows += self.glyphs == rows.row
        self.measure(rows)
        self.spacing = rows.spacing + sum(rows.widths)

    def measure(self, rows: _TexRows) -> None:
        """Count the space the cell just ended spends toward its column's,
        which is as much as its widest cell spends."""

        width = self.spacing - rows.spacing
        if rows.columns < len(rows.widths):
            rows.widths[rows.columns] = max(rows.widths[rows.columns], width)
        else:
            rows.widths.append(width)
        self.spacing = rows.spacing


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
    outside code; no invisible or reordering characters; no math delimiters
    outside the formulas the renderer marked; in those, only the TeX listed in
    :data:`_TESTIMONY_TEX`, well formed, read by :class:`_TexLayout` for
    arguments that show something, spacing that does not overlap symbols, and
    bounded spacing, rows, and cells; and at least one visible letter or digit.
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
    # The rendering is checked as well as the source, so a character the
    # renderer produced would not pass unseen either.
    hidden = _hidden_characters(text + "".join(document.itertext()))
    if hidden:
        errors.append("invisible or reordering characters are not allowed: " + ", ".join(hidden))
    if _longest_combining_run(text) > _MAX_COMBINING_MARKS:
        errors.append(
            f"more than {_MAX_COMBINING_MARKS} combining marks on one character are not allowed: "
            "they stack over the lines around it"
        )
    layout = _TexLayout()
    for piece, kind in pieces:
        if kind == "text":
            matches = _TEX_TEXT_DELIMITER.finditer(piece)
            layout.stray.update(dict.fromkeys(re.sub(r"\s", "", match[1]) for match in matches if match[1]))
        elif kind == "math":
            layout.read(piece.strip()[2:-2])
    errors.extend(layout.messages())
    if any(_TEX_COMMENT.search(piece) for piece, kind in pieces if kind == "math"):
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
    underscores = len(_UNDERSCORE_OPENER.findall(text))
    if underscores > TESTIMONY_MAX_UNDERSCORE_OPENERS:
        errors.append(
            f"testimony has {underscores} underscores that start a word, "
            f"over the limit of {TESTIMONY_MAX_UNDERSCORE_OPENERS}"
        )
    asterisks = text.count("*")
    if asterisks > TESTIMONY_MAX_ASTERISKS:
        errors.append(f"testimony has {asterisks} asterisks, over the limit of {TESTIMONY_MAX_ASTERISKS}")
    backslashes = text.count("\\")
    if backslashes > TESTIMONY_MAX_BACKSLASHES:
        errors.append(f"testimony has {backslashes} backslashes, over the limit of {TESTIMONY_MAX_BACKSLASHES}")
    return tuple(errors)


def _raw_html_errors(text: str, code: Iterable[str] = ()) -> tuple[str, ...]:
    """Name the raw HTML in ``text`` outside the pieces of ``code`` the site's
    converter shows as code: tags, comments, declarations, processing
    instructions, and character references a browser reads, formulas
    included; a space after "<" keeps a formula clear.

    Each is found in the source and excused only by as many occurrences of it
    in code, as :func:`_vault_markup_errors` does. A reference a browser shows
    as typed, such as ``&D;``, which names no character, is no HTML.

    This is the check for Markdown the site's own converter renders, which
    reads HTML. Testimony is checked by :func:`_testimony_errors` instead,
    since its renderer reads none."""

    pieces = list(code)
    errors: list[str] = []
    if text.count("<!--") > sum(piece.count("<!--") for piece in pieces):
        errors.append("HTML comments are not allowed: they hide the text they enclose")
    tags = Counter(_html_names(text))
    for piece in pieces:
        tags.subtract(_html_names(piece))
    names = [name for name, count in tags.items() if count > 0]
    if names:
        errors.append("raw HTML is not allowed: " + ", ".join(names) + "; in a formula, put a space after <")
    # References are counted as the converter read them, cut to a length
    # Python converts, and named as they were written.
    written: dict[str, str] = {}
    references: Counter[str] = Counter()
    for entity in _LOOSE_HTML_ENTITY.findall(text):
        read = _bounded_references(entity)
        written.setdefault(read, entity)
        references[read] += 1
    for piece in pieces:
        references.subtract(_bounded_references(entity) for entity in _LOOSE_HTML_ENTITY.findall(piece))
    entities = [
        _entity_name(written[read]) for read, count in references.items() if count > 0 and _live_reference(read)
    ]
    if entities:
        errors.append(
            "HTML character references are not allowed: " + ", ".join(entities) + "; type the character itself"
        )
    return tuple(errors)


def _html_names(text: str) -> list[str]:
    """The name of each piece of HTML :data:`_HTML_TAG` finds in ``text``.

    A declaration or processing instruction runs, by the pattern, to the
    first ``>``, which a reader of the vault and the site's converter each
    place elsewhere; the tags written inside one are named too, so none goes
    unmentioned."""

    names: list[str] = []
    position = 0
    while (markup := _HTML_TAG.search(text, position)) is not None:
        name = _HTML_NAME.match(text, markup.start()).group()
        if name.startswith(("<!", "<?")):
            names.append(name)
            position = markup.start() + len(name)
        else:
            names.append(name + ">")
            position = markup.end()
    return names


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
    :func:`_raw_html_errors` finds HTML in it outside code, or when the site's
    Markdown renderer would pass any of it through as raw HTML: the two parse
    markup differently, and the renderer's reading is what gets published.

    It is refused, too, for a TeX command that changes formulas other than
    its own, wherever the page's MathJax would read it. The articles on a
    page are typeset with one TeX input, so a definition in one would change
    what the others show.
    """

    visible = _COMPLETE_HTML_COMMENT.sub("", text)
    rendered, passed = _rendered_article(visible)
    reading = _RenderedText()
    reading.feed(rendered)
    reading.close()
    errors = list(_raw_html_errors(visible, reading.code))
    if passed and not errors:
        shown = ", ".join(repr(block[:40]) for block in dict.fromkeys(passed))
        errors.append(f"raw HTML is not allowed: the site would publish {shown} as HTML")
    commands = stateful_commands("".join(reading.text))
    if commands:
        errors.append(
            "TeX commands that change other formulas are not allowed: " + ", ".join(commands)
            + f"; define notation in the vault's {TEX_MACROS} instead, and put a command you only name in code"
        )
    return visible, tuple(errors)


def _rendered_article(text: str) -> tuple[str, list[str]]:
    """The site's rendering of ``text``, and what its renderer passes through
    from ``text`` as raw HTML.

    The renderer stashes raw HTML, character references, and highlighted code
    blocks alike. Code blocks are stashed while fences are read, before any
    raw HTML is, so whatever is stashed after that came from the text itself;
    a reference a browser shows as typed is left out. References by number
    are cut to a length Python converts first, standing for what they did.
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
    rendered = parser.convert(_bounded_references(text))
    passed = [block if isinstance(block, str) else "<element>" for block in parser.htmlStash.rawHtmlBlocks[highlighted:]]
    return rendered, [
        block for block in passed if not (_LOOSE_HTML_ENTITY.fullmatch(block) and not _live_reference(block))
    ]


class _RenderedText(HTMLParser):
    """The text of a rendered article: each piece of code, which shows as
    typed, and the text the page's MathJax reads, which is the rest outside
    the elements it skips.

    MathJax reads text a string at a time, and an element ends one, apart
    from ``<wbr>`` and comments; ``<br>`` stands in it for a line break. So a
    line break stands in :attr:`text` for every tag but ``<wbr>``, and a
    command found in it is one MathJax would read."""

    _SKIPPED = frozenset({"annotation", "annotation-xml", "code", "noscript", "pre", "script", "style", "textarea"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.code: list[str] = []
        self.text: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._end_string(tag)
        if tag in self._SKIPPED:
            if tag in {"code", "pre"} and not self._in_code():
                self.code.append("")
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        self._end_string(tag)
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if not self._open:
            self.text.append(data)
        elif self._in_code():
            self.code[-1] += data

    def _end_string(self, tag: str) -> None:
        if tag != "wbr":
            self.text.append("\n")

    def _in_code(self) -> bool:
        return "code" in self._open or "pre" in self._open


def _bounded_references(text: str) -> str:
    """``text`` with each character reference by number written in at most
    eight digits, standing for what it did: a number past the last code point
    stands for U+FFFD however long it is, and Python refuses to convert one
    of more than 4300 digits."""

    def bounded(reference: re.Match[str]) -> str:
        if reference[1]:
            return f"&#{reference[1]}{reference[2] if len(reference[2]) <= 6 else 'FFFFFFF'}"
        return f"&#{reference[3] if len(reference[3]) <= 7 else '99999999'}"

    return _NUMERIC_REFERENCE.sub(bounded, text)


def _live_reference(entity: str) -> bool:
    """Whether a browser shows ``entity`` as something other than its text:
    any reference by number does, and a name does when HTML defines it or the
    older name it starts with, as ``&notit;`` starts with ``&not``."""

    return entity.startswith("&#") or html.unescape(entity) != entity


def _entity_name(entity: str) -> str:
    """``entity``, cut short when long, and, when it stands for one character,
    that character's code point and name."""

    character = html.unescape(_bounded_references(entity))
    shown = entity if len(entity) <= 40 else f"{entity[:16]}... ({len(entity)} characters)"
    if len(character) != 1:
        return shown
    return f"{shown} (U+{ord(character):04X} {unicodedata.name(character, 'unnamed')})"


def _hidden_characters(text: str) -> list[str]:
    """Name each distinct invisible or reordering character in ``text``."""

    found: dict[str, None] = {}
    for character in text:
        if character in _ALLOWED_CONTROLS or character == " ":
            continue
        category = unicodedata.category(character)
        if (
            category in _HIDDEN_CATEGORIES
            or category == "Zs"
            or ord(character) in _HIDDEN_CODE_POINTS
            or unicodedata.bidirectional(character) in _HIDDEN_BIDI_CLASSES
        ):
            name = unicodedata.name(character, "unnamed")
            found[f"U+{ord(character):04X} {name}"] = None
    return list(found)


def _longest_combining_run(text: str) -> int:
    """The most combining marks in a row in ``text``."""

    longest = run = 0
    for character in text:
        run = run + 1 if unicodedata.category(character) in {"Mn", "Me"} else 0
        longest = max(longest, run)
    return longest


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
    """Refuse, before anything is created or read, where this platform lacks what publication uses.

    Publication opens, creates, renames, and removes files relative to a held
    directory descriptor without following links, and locks that directory.
    """

    required = (
        hasattr(os, "O_DIRECTORY"),
        hasattr(os, "O_NOFOLLOW"),
        os.open in os.supports_dir_fd,
        os.mkdir in os.supports_dir_fd,
        # os.replace takes its directory descriptors exactly as os.rename does;
        # only os.rename is listed.
        os.rename in os.supports_dir_fd,
        os.unlink in os.supports_dir_fd,
        hasattr(os, "fchmod"),
        fcntl is not None,
    )
    if not all(required):
        raise ValueError("this platform cannot safely publish read-back cards")


def _open_card_directory(blueprint: Path, article_id: str, *, create: bool) -> int | None:
    """Open the card directory through held, no-follow directory descriptors.

    With ``create``, missing directories on the way are made. Without it,
    nothing is created, and a missing directory means there is no card yet:
    ``None``.
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
                if not create:
                    return None
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValueError(f"cannot create read-back directory component: {part}") from exc
                try:
                    following = os.open(part, flags, dir_fd=current)
                except OSError as exc:
                    raise ValueError(f"refusing a symlink or unsafe component in the read-back path: {part}") from exc
            except OSError as exc:
                raise ValueError(f"refusing a symlink or unsafe component in the read-back path: {part}") from exc
            # Move on before closing, so an interruption never leaves
            # ``current`` naming a closed descriptor.
            current, previous = following, current
            os.close(previous)
        directory, current = current, -1
        return directory
    finally:
        if current >= 0:
            os.close(current)


def _existing_card_hash(blueprint: Path, article_id: str, path: Path) -> str | None:
    """Hash the card at ``path`` through no-follow descriptors, creating nothing."""

    directory = _open_card_directory(blueprint, article_id, create=False)
    if directory is None:
        return None
    try:
        return _card_hash_at(directory, path.name, path)
    finally:
        os.close(directory)


def _card_directory_identity(blueprint: Path, article_id: str) -> tuple[int, int] | None:
    """The device and inode of the directory the card's path leads to, reached without following a link."""

    try:
        directory = _open_card_directory(blueprint, article_id, create=False)
    except ValueError:
        return None
    if directory is None:
        return None
    try:
        found = os.fstat(directory)
    finally:
        os.close(directory)
    return found.st_dev, found.st_ino


def _card_hash_at(directory: int, filename: str, display_path: Path) -> str | None:
    """The hash of a card's bytes, read relative to a held directory without following links.

    ``None`` if there is no card. Only a regular file is read, and never past
    the card limit; opening never blocks, even on a FIFO. The bytes are hashed
    whatever they hold, so a card that is not UTF-8 can still be named, and
    replaced, by its hash.
    """

    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot safely inspect existing read-back: {display_path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"read-back destination is not a regular file: {display_path}")
        raw = _read_card_bytes(descriptor)
    finally:
        os.close(descriptor)
    if raw is None:
        raise ValueError(
            f"existing read-back is over the {_CARD_MAX_BYTES}-byte limit for a card file: {display_path}; "
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
        return f"read-back changed before replacement: expected {expected_card_hash!r}, found {before!r}"
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
    """Publish a card through a held, locked directory descriptor under compare-and-swap.

    The caller holds the directory's lock, so no other publication acts
    between the compare and the replacement. Content identical to the card's
    is not written again. Otherwise the new card is staged beside the old
    one, flushed to disk, and renamed over it in one step, so the card's name
    holds the old complete card until it holds the new one. A failure or
    interruption before the rename removes the staging file; a crash can
    leave it, and the loader never reads it as a card.
    """

    data = content.encode("utf-8")
    before = _card_hash_at(directory, path.name, path)
    if before == _card_hash(data):
        return
    if conflict := _card_conflict(path, before, expected_card_hash):
        raise ValueError(conflict)
    # Named before it exists, so the cleanup below sees the staging file from
    # the moment it is created. The name is too random to be anyone else's.
    staged_name: str | None = f"{_WORK_PREFIX}{secrets.token_hex(12)}.tmp"
    try:
        try:
            descriptor = os.open(
                staged_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
        except FileExistsError:
            # Not this write's file, so not this write's to remove.
            staged_name = None
            raise
        try:
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(staged_name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        staged_name = None
    except OSError as exc:
        raise ValueError(f"cannot publish read-back: {path}: {exc}") from exc
    finally:
        if staged_name is not None:
            _unlink_quietly(directory, staged_name)
    try:
        os.fsync(directory)
    except OSError as exc:
        # The card is in place and readable; only its surviving a crash of the
        # whole system is in doubt, so this is no reason to report a failure.
        warnings.warn(
            f"read-back {path} was published, but its directory could not be flushed to disk: {exc}",
            RuntimeWarning,
            stacklevel=3,
        )


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
