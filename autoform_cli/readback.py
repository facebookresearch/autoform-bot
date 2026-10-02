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

import errno
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
import xml.etree.ElementTree as etree
from collections import Counter
from dataclasses import dataclass, replace
from html.entities import html5 as _NAMED_REFERENCES
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator, Mapping, NamedTuple
from urllib.parse import unquote_to_bytes

import cmarkgfm
import html5lib
from cmarkgfm.cmark import Options
import markdown as markdown_renderer
from markdown.blockprocessors import HashHeaderProcessor
from markdown.extensions.tables import TableProcessor
from markdown.inlinepatterns import BACKTICK_RE, BacktickInlineProcessor
from markdown.treeprocessors import Treeprocessor
from markdown.util import AtomicString

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
#: read more than one byte past it: that byte marks a larger one as an invalid
#: card, whose bytes are never kept.
_CARD_MAX_BYTES = 4 * 1024 * 1024
#: The only errors opening a card that mean there is no card there to read:
#: a link, nothing, or a file where a directory should be. Any other error
#: leaves a card unread, so it is reported rather than skipped.
_NO_CARD_ERRNOS = frozenset({errno.ELOOP, errno.ENOENT, errno.ENOTDIR})
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
#: How many combining marks one character may carry, and how many of those
#: may stack above it or below it, by canonical combining class. The scripts
#: written with them put at most three or four on a letter, and two above or
#: below, as Vietnamese and polytonic Greek do; each more stacks further over
#: the line above or below.
_MAX_COMBINING_MARKS = 4
_MAX_STACKED_MARKS = 2
_MARKS_ABOVE = frozenset({214, 216, 228, 230, 232, 234})
_MARKS_BELOW = frozenset({202, 218, 220, 222, 233})

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
#: the scans for HTML a vault viewer would read are linear, and GitHub's
#: reading leaves HTML out, so "<" needs no limit: a thousand nested tags,
#: which once overflowed the stack, are refused in a hundredth of a second.
#: Brackets cost most, 0.6 s at their limit alone. At every limit at once the
#: slowest testimony found validates in 1.2 s and 5.9 MB, 0.03 s of it in
#: GitHub's reading, on a machine under load.
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
#: The table cells a testimony may hold. Both Markdown readings fill every row
#: out to the header's width, so a wide header over many short lines renders
#: millions of cells from a few kilobytes; counted before parsing.
TESTIMONY_MAX_TABLE_CELLS = 2048
#: The HTML a testimony may render to, a backstop for any other construct the
#: renderer expands, checked before the HTML is parsed. Escaping alone takes
#: a testimony at the byte limit to at most five times its size.
TESTIMONY_MAX_RENDERED_BYTES = 256 * 1024
_MATH_DELIMITER = re.compile(r"\$|\\[()\[\]]")
_BACKTICK_RUN = re.compile(r"`+")
_UNDERSCORE_OPENER = re.compile(r"(?<!\w)_")
#: A line that could be a table's delimiter row, in a block quote or a list or
#: not: Python-Markdown takes any row of pipes, colons, hyphens, and spaces
#: that splits into as many cells as its header, so a pipe is all it needs.
#: And the line that ends a table wherever it is.
_TABLE_DELIMITER_ROW = re.compile(r"(?=[^|]*\|)[\s>|:-]+")
_BLANK_LINE = re.compile(r"\s*")
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
    #: Whether the file at the card's path could not be read as a card at all;
    #: ``validation_errors`` then says why.
    unreadable: bool = False

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
        """Return intrinsic errors and, when given, mismatches with a declaration.

        A card that could not be read has only the reason: every other check
        would restate that none of its fields were read.
        """

        errors = list(self.validation_errors)
        if self.unreadable:
            return tuple(dict.fromkeys(errors))
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
    """Every ``*.md`` entry under ``root``, in any case, with its bytes, read without following a link.

    A card is a file inside the vault; a symlink could point anywhere. The
    candidates come from a listing, which can be out of date by the time a
    file is opened, so a path-level check is not enough. Where the platform
    allows, each candidate is opened through no-follow descriptors walked
    down from ``root``, and a card or directory swapped for a symlink after
    the listing is refused rather than followed. Elsewhere (Windows) the file
    is opened first; then no component of its path may be a link, and the
    open file must be the one a no-follow stat of the path names. Anything
    else that is not a regular file, cannot be read, or holds more than a
    card may comes with no bytes and the reason instead. A directory that
    cannot be listed is refused with a :class:`ValueError`.
    """

    def unlistable(exc: OSError) -> None:
        if exc.errno not in _NO_CARD_ERRNOS:
            raise ValueError(f"cannot list read-back directory {exc.filename}: {exc.strerror}") from exc

    walk = hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
    root_descriptor: int | None = None
    if walk:
        try:
            root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            unlistable(exc)
            return
    elif root.is_symlink() or not root.is_dir():
        return
    try:
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
            read = _read_card_file(root, root_descriptor, path.relative_to(root))
            if read is not None:
                yield path, *read
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _read_card_file(root: Path, root_descriptor: int | None, relative: Path) -> tuple[bytes | None, str | None] | None:
    """The bytes of the regular file at ``root / relative``, or why they could not be read.

    ``None`` if there is nothing there to read: the path leads through or to
    a link, or no longer leads anywhere.
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
                return None, "card path is not a regular file"
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
            if (opened_file.st_dev, opened_file.st_ino) != (named_file.st_dev, named_file.st_ino):
                return None
            if not stat.S_ISREG(opened_file.st_mode):
                return None, "card path is not a regular file"
        try:
            raw = _read_card_bytes(descriptor)
        except OSError as exc:
            return None, f"card cannot be read: {exc.strerror}"
    except OSError as exc:
        if exc.errno in _NO_CARD_ERRNOS:
            return None
        return None, f"card cannot be read: {exc.strerror}"
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
            readback = readback_for(readbacks, article_id, declaration.name)
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
        key
        for node in report.nodes
        for declaration in node.declarations
        for key in readback_keys(identities.get(node.node_id, node.node_id), declaration.name)
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


class _LiteralText(Treeprocessor):
    """Show every ``&`` as typed.

    The testimony renderer reads no HTML, so each ``<``, ``>``, and ``&`` in a
    testimony is text. The serializer escapes ``<`` and ``>`` but leaves an
    ``&`` that starts something shaped like a character reference, so ``&``
    is escaped here first, outside code, whose text the renderer escaped
    already. ``\\$`` comes out as a bare ``$``, as GitHub shows it: MathJax
    reads no text of a card outside the formulas the renderer marked.
    """

    def run(self, root: object) -> None:
        for element in root.iter():  # type: ignore[attr-defined]
            if element.tag != "code" and element.text:
                element.text = self._literal(element.text)
            if element.tail:
                element.tail = self._literal(element.tail)

    @staticmethod
    def _literal(text: str) -> str:
        literal = text.replace("&", "&amp;")
        # Formulas are atomic strings, which the renderer must not read again.
        return type(text)(literal) if literal != text else text


class _HashHeading(HashHeaderProcessor):
    """A ``#`` heading only where CommonMark reads one, with a space or the
    line's end after the hashes, so ``#41755`` starting a line stays text, as
    a viewer of the vault shows it."""

    RE = re.compile(r"(?:^|\n)(?P<level>#{1,6})(?=[ \t\n]|$)(?P<header>(?:\\.|[^\\])*?)#*(?:\n|$)")


class _CodeFormula(BacktickInlineProcessor):
    """Python-Markdown's code spans, and a formula written as code between
    dollar signs, ``$`...`$``, the form GitHub also reads as a formula with
    the code as its TeX, so that neither reads escapes or emphasis in it. The
    code is read as CommonMark reads a code span, line breaks as spaces and
    one space trimmed from each end, and a dollar sign after a letter, digit,
    underscore, or backslash opens no formula, as on GitHub."""

    def handleMatch(self, m: re.Match[str], data: str) -> tuple[etree.Element | str, int, int]:  # type: ignore[override]
        start, end = m.start(0), m.end(0)
        if not (
            m.group(3)
            and data[start - 1 : start] == "$" == data[end : end + 1]
            and not re.match(r"[A-Za-z0-9_\\]", data[max(start - 2, 0) : start - 1])
        ):
            return super().handleMatch(m, data)
        code = m.group(3).replace("\n", " ")
        if len(code) > 2 and code[0] == code[-1] == " " and code.strip(" "):
            code = code[1:-1]
        formula = etree.Element("span", {"class": "arithmatex"})
        formula.text = AtomicString(f"\\({code}\\)")
        return formula, start - 1, end + 1


def _formula_fence(source: str, *args: object, **kwargs: object) -> str:
    """A ```` ```math ```` block, which GitHub shows as a displayed formula
    with the block's text as its TeX: a display formula for MathJax, in the
    ``<p>`` the renderer gives every other one."""

    return '<p class="arithmatex">\\[\n' + html.escape(source, quote=False) + "\n\\]</p>"


class _CountedTable(TableProcessor):
    """Python-Markdown's tables, noting a row with more or fewer cells than
    the header. The renderer, like a CommonMark viewer of the vault, cuts
    such a row to the header's width or fills it out with empty cells, so a
    cell past the header's would not be shown, and a line run on after the
    table would be shown as a row."""

    uneven = False

    def _build_row(self, row: str, parent: object, align: list[str | None]) -> None:  # type: ignore[override]
        self.uneven = self.uneven or len(self._split_row(row)) != len(align)
        super()._build_row(row, parent, align)  # type: ignore[arg-type]


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
            "pymdownx.superfences": {
                "custom_fences": [{"name": "math", "class": "arithmatex", "format": _formula_fence}]
            },
        },
    )
    converter.preprocessors.deregister("html_block")
    converter.inlinePatterns.register(_CodeFormula(BACKTICK_RE), "backtick", 190)
    converter.parser.blockprocessors.register(_HashHeading(converter.parser), "hashheader", 70)
    table = converter.parser.blockprocessors["table"]
    converter.parser.blockprocessors.register(_CountedTable(converter.parser, table.config), "table", 75)
    for pattern in ("html", "entity", "autolink", "automail"):
        converter.inlinePatterns.deregister(pattern)
    converter.treeprocessors.register(_LiteralText(converter), "literal_text", 5)
    return converter


def render_testimony(text: str) -> str:
    """The HTML a testimony is shown as: what the validator inspects and what
    the site embeds, byte for byte."""

    return _render_testimony(text)[0]


def _render_testimony(text: str) -> tuple[str, bool, bool]:
    """The HTML for ``text``, whether it defines Markdown links, and whether
    a table row in it has more or fewer cells than its header.

    The site places the HTML inside its own Markdown page, whose parser takes
    a block-level tag at the start of a line for the start of a block it reads
    again. Each such tag is put on the line before it, where it is left as
    written; the newlines between blocks are not shown.
    """

    converter = _testimony_converter()
    rendered = converter.convert(text)
    names = "|".join(sorted(converter.block_level_elements, key=len, reverse=True))
    table = converter.parser.blockprocessors["table"]
    uneven = isinstance(table, _CountedTable) and table.uneven
    return re.sub(rf"\n(?=<(?:{names})[\s/>])", "", rendered), bool(converter.references), uneven


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
#: The languages a code fence may name. Every viewer shows a fence's first
#: word as the language at most, and GitHub hides the rest of the line, while
#: the site makes classes of a header in braces and drops what it does not
#: use, so a fence names one of these or nothing.
_TESTIMONY_LANGUAGES = frozenset({"lean", "lean4", "text"})
_LANGUAGE_ERROR = (
    "code fences naming anything but lean, lean4, or text are not allowed: a Markdown viewer hides the other "
    "words after a fence; leave the fence bare otherwise"
)
#: How GitHub reads a card file in the vault: cmark-gfm, with the extensions
#: and footnotes github.com reads, marking each block with the lines of the
#: source it comes from. It leaves out the HTML GitHub passes through to be
#: seen, writing a comment in its place: testimony holding HTML GitHub reads
#: is refused, as raw HTML where the site reads it too and as shown otherwise
#: where the site shows its characters, and html5lib takes time quadratic in
#: the depth of nested tags outside a table, 2 s for 28 KB of "<ul>".
_GITHUB_OPTIONS = Options.CMARK_OPT_FOOTNOTES | Options.CMARK_OPT_SOURCEPOS
_GITHUB_EXTENSIONS = ["table", "strikethrough", "autolink", "tasklist"]
#: A line that starts, after block quote and list markers, with what GitHub
#: reads as a link's or a footnote's definition, which it hides or moves to
#: the end of the card, and the first line of a block quote it shows as one
#: of its own alerts in place of the line. A definition is looked for on
#: every line GitHub does not read as code, though it reads one only where a
#: paragraph could start.
_LINK_DEFINITION = re.compile(r"(?:[ \t>]|[-+*](?=[ \t])|\d{1,9}[.)](?=[ \t]))*\[(\^?)[^\n]*\]:")
_ALERT = re.compile(r"[ \t]*\[!(?:note|tip|important|warning|caution)\][ \t]*", re.IGNORECASE)
_FOOTNOTE_ERROR = (
    "footnote definitions are not allowed: GitHub hides them or moves them to the end of the card; write the note "
    "in the text"
)


#: Commands that set a letter, so testimony showing only these still shows one.
_TEX_LETTERS = frozenset(
    r"""
    \alpha \beta \gamma \delta \epsilon \varepsilon \zeta \eta \theta \vartheta \iota \kappa
    \varkappa \lambda \mu \nu \xi \omicron \pi \varpi \rho \varrho \sigma \varsigma \tau \upsilon
    \phi \varphi \chi \psi \omega \digamma \Gamma \Delta \Theta \Lambda \Xi \Pi \Sigma \Upsilon
    \Phi \Psi \Omega \aleph \beth \gimel \daleth \eth \ell \hbar \hslash \imath \jmath \wp \Re
    \Im \Bbbk \varGamma \varDelta \varTheta \varLambda \varXi \varPi \varSigma \varUpsilon \varPhi \varPsi
    \varOmega
    """.split()
)


class _Tex(NamedTuple):
    """How MathJax sets one TeX command.

    ``kind`` is ``"glyph"`` for a command that sets a symbol, ``"operator"``
    for a large or named operator, which ``\\limits`` may follow, ``"space"``
    for horizontal space ``width`` mu wide (an eighteenth of an em; negative
    space pulls the next symbol back), any other kind setting ``width`` mu
    of space around what it sets, ``"style"`` for a command that changes
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
#: symbol whatever it is, but for ``\exists``, which MathJax 3.2.2 sets as
#: ``\nexists`` after it. ``\iff``, ``\implies``, and ``\impliedby`` are not
#: among them: MathJax sets space before their arrow, and ``\not`` strikes
#: through that.
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
    \Longleftarrow \Longleftrightarrow \hookrightarrow \hookleftarrow
    \twoheadrightarrow \uparrow \downarrow \updownarrow \Uparrow \Downarrow \Updownarrow \nearrow
    \searrow \swarrow \nwarrow \nleftarrow \nrightarrow \nLeftarrow \nRightarrow \nleftrightarrow
    \nLeftrightarrow \leftleftarrows \rightrightarrows \upuparrows \downdownarrows
    \circlearrowleft \circlearrowright \curvearrowleft \curvearrowright \Lsh \Rsh \looparrowleft
    \looparrowright \leadsto \rightsquigarrow \leftrightsquigarrow \multimap \rightleftharpoons
    \upharpoonright \restriction \leqq \geqq \lll \llless \ggg \gggtr \lessgtr \gtrless \lesseqgtr
    \gtreqless \lesseqqgtr \gtreqqless \eqslantless \eqslantgtr \nless \ngtr \nleqq \ngeqq \nleqslant
    \ngeqslant \lnsim \gnsim \lnapprox \gnapprox \gvertneqq \curlyeqprec \curlyeqsucc \preccurlyeq
    \succcurlyeq \precnsim \succnsim \precnapprox \succnapprox \circeq \bumpeq \Bumpeq \doteqdot \Doteq
    \eqcirc \fallingdotseq \risingdotseq \thicksim \thickapprox \backepsilon \sqsubset \sqsupset
    \subsetneqq \supsetneqq \varsubsetneqq \varsupsetneq \varsupsetneqq \nsubseteqq \nsupseteqq
    \nshortparallel \Vvdash \Lleftarrow \Rrightarrow \leftrightarrows \rightleftarrows \dashleftarrow
    \dashrightarrow \twoheadleftarrow \leftarrowtail \rightarrowtail \leftharpoonup \leftharpoondown
    \rightharpoonup \rightharpoondown \leftrightharpoons \upharpoonleft \downharpoonleft \downharpoonright
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
        \Game \S \yen \circledR \maltese \circledS \bigcirc \blacklozenge \blacktriangledown
        \blacktriangleleft \blacktriangleright \triangledown \diagup \diagdown \clubsuit \diamondsuit
        \heartsuit \spadesuit \dotsi \dotsm \dotso \notChar \And \smallint \Arrowvert \arrowvert \bracevert
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
        \boxdot \circledcirc \divsymbol \doublecap \doublecup
        """
    ),
    # Commands MathJax 3.2.2 defines as macros that add space: \mod and \pmod
    # 18 mu before ``mod`` in a displayed formula and 6 after it, \pod 18 mu
    # before its parenthesis, \bmod and the arrows 5 mu on either side.
    **_tex(r"\mod \pmod", arguments="ge", width=24),
    r"\pod": _Tex("glyph", "ge", 18),
    **_tex(r"\bmod \iff \implies \impliedby", width=10),
    # Large and named operators.
    **_tex(
        r"""
        \sum \prod \coprod \int \iint \iiint \oint \bigcup \bigcap \bigoplus \bigotimes \bigvee
        \bigwedge \bigsqcup \biguplus \bigodot \lim \liminf \limsup \varinjlim \varprojlim \sup
        \inf \max \min \sin \cos \tan \sec \csc \cot \sinh \cosh \tanh \coth \arcsin \arccos
        \arctan \log \ln \lg \exp \det \dim \ker \deg \gcd \hom \arg \Pr \iiiint \idotsint \intop \injlim
        \projlim \varliminf \varlimsup
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
        \widetilde \overline \underline \overrightarrow \overleftarrow \overleftrightarrow \underleftarrow
        \underrightarrow \overbrace \underbrace
        """,
        arguments="m",
    ),
    # Fonts, classes, and text.
    **_tex(
        r"""
        \mathbb \Bbb \mathcal \mathfrak \mathscr \mathrm \mathbf \mathsf \mathit \mathtt \pmb \mathrel
        \mathbin \mathord \mathopen \mathclose
        """,
        arguments="m",
    ),
    **_tex(r"\text \textrm \textbf \textit \texttt \textsf \textnormal \textup \mbox", arguments="t"),
    **_tex(r"\displaystyle \textstyle \scriptstyle \scriptscriptstyle \rm \bf \it \sf \tt \cal", "style", ""),
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
    r"\hline": _Tex("hline", ""),
    r"\begin": _Tex("begin", ""),
    r"\end": _Tex("end", ""),
}
#: Environments testimony may use, all of them rows of cells but ``equation``,
#: which sets what it holds as a formula does.
_TEX_ENVIRONMENTS = frozenset(
    {"cases", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix", "array"}
    | {"aligned", "gathered", "split", "align", "align*", "gather", "gather*", "equation", "equation*"}
)
#: What to write for commands and environments outside the allowlist that
#: MathJax sets the way one inside it does.
_TEX_INSTEAD = {
    r"\choose": r"\binom{n}{k} for {n \choose k}",
    r"\over": r"\frac{a}{b} for {a \over b}",
    r"\cfrac": r"\dfrac for \cfrac",
    r"\genfrac": r"\frac or \binom for \genfrac",
    r"\hspace": r"\quad or \, for \hspace",
    r"\boxed": r"the boxed formula alone, without \boxed",
    r"\begin{multline}": r"\begin{gathered} for \begin{multline}",
    r"\begin{multline*}": r"\begin{gathered} for \begin{multline*}",
    r"\begin{alignat}": r"\begin{aligned}, without the column count, for \begin{alignat}",
    r"\begin{alignat*}": r"\begin{aligned}, without the column count, for \begin{alignat*}",
    r"\begin{alignedat}": r"\begin{aligned}, without the column count, for \begin{alignedat}",
}
#: The environments MathJax 3.2.2 reads a bracket after, for where their rows
#: sit: it applies ``[t]``, ``[b]``, or ``[c]`` and shows nothing else written
#: there.
_TEX_ALIGNABLE = frozenset({"aligned", "gathered", "array"})
#: The environments MathJax 3.2.2 sets once in a formula: a second, even one
#: nested in the first, gets an error in place of the formula.
_TEX_EQUATIONS = frozenset({"align", "align*", "gather", "gather*", "equation", "equation*"})
#: The columns ``\begin{array}`` may take. MathJax draws ``|`` and ``:`` as
#: rules between columns, one rule for two of them, and at either edge a rule
#: that reads as a bar beside the cells; it drops any other character without
#: showing it.
_TEX_ARRAY_COLUMNS = re.compile(r"\s*\{ *[lcr](?: *(?:[|:] *)?[lcr]){0,31} *\}")
#: Commands MathJax 3.2.2 reads as a function name waiting for what follows,
#: or expands into several items. A superscript or subscript that is one of
#: them alone gets an error in place of the formula, or takes only the first
#: item, so it must be braced, as ``x^{\sin}`` is.
_TEX_BRACED_SCRIPTS = frozenset(
    r"""
    \arcsin \arccos \arctan \arg \cos \cosh \cot \coth \csc \deg \dim \exp \hom \ker \lg \ln \log \sec
    \sin \sinh \tan \tanh \mathop \dots \varinjlim \varprojlim \varliminf \varlimsup \idotsint \iff
    \implies \impliedby \pmb \mod \pmod \pod
    """.split()
)
#: The characters past ASCII MathJax 3.2.2 sets in a formula, outside
#: ``\text``: those in the ranges of its operator dictionary
#: (OperatorDictionary.RANGES), merged here, but for U+20000 to U+2FA1F,
#: which its CommonHTML and SVG output throw for. Any other gets an error in
#: place of the formula.
_TEX_CHARACTER_RANGES = (
    (0x00A0, 0x024F), (0x02B0, 0x1A20), (0x1AB0, 0x209F), (0x2100, 0x23FF), (0x2460, 0x2DE0), (0x2E00, 0x2FDF),
    (0x2FF0, 0xA49F), (0xA4D0, 0xD7FF), (0xF900, 0x1D25F), (0x1D360, 0x1D37F), (0x1D400, 0x1D7FF),
    (0x1DF00, 0x1F9FF),
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
_TEX_CLOSERS = {"group": "}", "substack": "}", "left": r"\right", "environment": r"\end", "equation": r"\end"}
#: What cannot start a script, and the commands MathJax will not take as one.
_TEX_NOT_SCRIPTS = frozenset({"}", "&", "^", "_", "'", "’"})
#: The two characters MathJax sets as a prime.
_TEX_PRIMES = frozenset({"'", "’"})
_TEX_UNSCRIPTED = frozenset({"style", "not", "begin", "limits", "middle", "right", "end", "rows", "hline"})
_TEX_ENVIRONMENT = re.compile(r"\s*\{([^{}\\]{0,64})\}")
#: ``\\[<dimension>]`` spaces rows apart, or with a negative dimension draws
#: one over another; MathJax reads a star, then the bracket, only right after
#: the ``\\``, and shows neither.
_TEX_ROW_SPACING = re.compile(r"[*\[]")
#: The escapes ``\text`` and its kin read, rather than show as typed.
_TEX_TEXT_ESCAPE = re.compile(r"\\[${}\\]")
_TEX_DOUBLE_INTEGRAL = re.compile(r"\\int\s*(?:\\!\s*){2,}\\int")
#: The delimiters javascripts/mathjax.js configures, which in a formula are
#: typeset as one, or end it early.
_TEX_FORMULA_DELIMITERS = frozenset({"$", r"\(", r"\)", r"\[", r"\]"})
_TEX_ENVIRONMENT_NAME = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
_TEX_CONTROL_SEQUENCE = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)

_TEX_BRACES = "unbalanced TeX braces are not allowed"
_TEX_LEFT_RIGHT = "unbalanced \\left and \\right are not allowed"
_TEX_MIDDLE = "\\middle is allowed only between \\left and \\right"
_TEX_LIMITS = "\\limits and \\nolimits are allowed only after a large or named operator"
_TEX_NOT = "\\not is allowed only before a relation such as =, \\in, or \\le, or before \\exists"
_TEX_SCRIPTS = "a second TeX superscript or subscript on one symbol is not allowed: use braces"
_TEX_MISPLACED = "TeX & and \\\\ are allowed only between the cells and rows of an environment"
_TEX_UNBALANCED_ENVIRONMENT = "TeX \\begin and \\end that do not match are not allowed"
_TEX_ARRAY = (
    "TeX \\begin{array} is allowed only with its columns as l, c, and r in braces, such as {lcr}, and one | or : "
    "between two of them for a rule, as in {cc|c}"
)
_TEX_HLINE = (
    "TeX \\hline is allowed only right after \\\\ in an array, once, before a row that shows something: a rule "
    "at the top or bottom of the cells reads as an overline or underline"
)
_TEX_ALIGNMENT = (
    "a bracket after TeX \\begin{aligned}, \\begin{gathered}, or \\begin{array} other than [t], [b], or [c] "
    "is not allowed: MathJax does not show what it holds; write {} before a bracket that starts the first row"
)
_TEX_TEXT = (
    "TeX commands and formulas inside \\text are not allowed: MathJax shows a command there as typed; close "
    "\\text before a formula, as in \\text{if } x > 0, and write \\$, \\{, \\}, or \\\\ for the character itself"
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
    #: For ``\hline``: whether the environment is an array, where the
    #: current row's first token is, and whether a rule is above it.
    array: bool = False
    start: int = 0
    ruled: bool = False


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
    the next, so neither end of a formula may hold negative space, which
    would slide it over what sits beside it; the spacing of every formula
    counts toward the testimony's.
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
        self.overlap = self.double_integral = self.marks = self.edge = False
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
        self.edge = self.edge or self.run < 0
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

        instead = [_TEX_INSTEAD[name] for name in sorted(self.unlisted) if name in _TEX_INSTEAD]
        if any(name.startswith("\\begin") and name not in _TEX_INSTEAD for name in self.unlisted):
            instead.append(
                "one of the environments allowed: equation, align, gather, aligned, gathered, split, cases, array, "
                "or a matrix"
            )
        named = [
            (
                "TeX outside the read-back allowlist is not allowed: ",
                self.unlisted,
                "; write " + "; ".join(instead) if instead else "",
            ),
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
        errors = [prefix + _named(sorted(names)) + suffix for prefix, names, suffix in named if names]
        if self.marks:
            errors.append(
                "combining marks in a formula are not allowed: MathJax sets each as a symbol of its own, beside or "
                "over the one before it; write an accent such as \\acute{x}, or the character already composed"
            )
        if self.total_spacing > _TEX_MAX_TESTIMONY_SPACING:
            errors.append(
                f"TeX spacing over {_TEX_MAX_TESTIMONY_SPACING // 18} em in one testimony is not allowed: "
                "it pushes symbols apart or out of view"
            )
        if self.stray:
            errors.append(
                "math delimiters inside a formula are not allowed: "
                + _named(sorted(self.stray))
                + "; drop them, and keep dollar signs out of formulas"
            )
        if self.overlap:
            errors.append(
                "repeated negative TeX spacing is not allowed: it slides symbols over one another"
                + ("; write \\iint for a double integral" if self.double_integral else "")
            )
        if self.edge:
            errors.append(
                "negative TeX spacing at the start or end of a formula is not allowed: it slides the formula over "
                "what sits beside it; write it between two symbols of one formula"
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
        self.edge = self.edge or (not self.glyphs and self.run < 0)
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
        if alone and (entry.arguments.strip("g") or kind in {"left", "right", "middle", "begin", "end", "rows", "hline"}):
            self.missing[token] = None
        elif kind == "limits":
            if alone or not self.operator:
                self.errors[_TEX_LIMITS] = None
        elif kind == "not":
            if alone or (self.peek() not in _TEX_RELATIONS and self.peek() != r"\exists"):
                self.errors[_TEX_NOT] = None
        elif kind == "rows":
            spacing = _TEX_ROW_SPACING.match(self.tex, self.tokens[self.index - 1][0] + 2)
            if spacing and spacing.group() == "*":
                self.errors[
                    "a * right after TeX \\\\ is not allowed: MathJax reads it as part of the row break and does "
                    "not show it; put a space between them"
                ] = None
            elif spacing:
                self.errors[
                    "TeX row spacing after \\\\ is not allowed: it can draw rows over one another; remove the "
                    "bracket after \\\\, or write {} before a bracket that starts a row"
                ] = None
            if context in {"formula", "environment", "substack", "equation"}:
                self.row(rows)
            else:
                self.errors[_TEX_MISPLACED] = None
            self.new_atom()
        elif kind == "hline":
            if context == "environment" and rows is not None and rows.array and rows.rows and rows.start == self.index - 1:
                rows.ruled = True
            else:
                self.errors[_TEX_HLINE] = None
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
                self.spacing += entry.width
            self.new_atom(kind == "operator")

    def group(self, context: str) -> None:
        rows = _TexRows(self.glyphs, self.glyphs, self.spacing, []) if context == "substack" else None
        self.new_atom()
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
        self.marks = self.marks or any(unicodedata.category(character) in {"Mn", "Me"} for character in content)
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
                    "more than one TeX align, gather, or equation environment in a formula is not allowed: MathJax "
                    "refuses the formula; write aligned or gathered, or leave out the equation environment"
                ] = None
            self.equation = True
        if name in {"equation", "equation*"}:
            self.new_atom()
            if self.read_list("equation", None) is None or self.environment_name(r"\end") != name:
                self.errors[_TEX_UNBALANCED_ENVIRONMENT] = None
            return
        if name in _TEX_ALIGNABLE:
            self.alignment(name)
        if name == "array":
            match = _TEX_ARRAY_COLUMNS.match(self.tex, self.tokens[self.index][0]) if self.index < self.limit else None
            if match is None:
                self.errors[_TEX_ARRAY] = None
            else:
                self.skip_to(match.end())
        rows = _TexRows(self.glyphs, self.glyphs, self.spacing, [], array=name == "array", start=self.index)
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
        if rows.ruled and self.glyphs == rows.row:
            self.errors[_TEX_HLINE] = None
        rows.start, rows.ruled = self.index, False
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
        if rows.ruled and self.glyphs == rows.row:
            self.errors[_TEX_HLINE] = None
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
    outside code, and nothing GitHub, which shows the vault's card files,
    reads as a link, heading, footnote, or alert, or shows otherwise than the
    site does, formulas included; no invisible or reordering characters; no
    math delimiters
    outside the formulas the renderer marked; in those, only the TeX listed in
    :data:`_TESTIMONY_TEX`, well formed, read by :class:`_TexLayout` for
    arguments that show something, spacing that does not overlap symbols, and
    bounded spacing, rows, and cells; and at least one visible letter or digit.
    """

    if limits := _testimony_limit_errors(text):
        return limits
    rendered, defines_links, uneven_table = _render_testimony(text)
    size = len(rendered.encode("utf-8"))
    if size > TESTIMONY_MAX_RENDERED_BYTES:
        return (f"testimony renders to {size} bytes of HTML, over the limit of {TESTIMONY_MAX_RENDERED_BYTES}",)
    document = html5lib.parseFragment(rendered, namespaceHTMLElements=False)
    errors: list[str] = []
    if defines_links:
        errors.append("Markdown link definitions are not allowed")
    if uneven_table:
        errors.append(
            "Markdown table rows with more or fewer cells than the header are not allowed: the renderer drops "
            "cells past the header's and reads a line run on after the table as a row; write \\| for a pipe in "
            "a cell, or \\vert in a formula, and leave a blank line after the table"
        )
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
        language = element.attrib.get("class", "").lower().removeprefix("language-") if tag == "code" else ""
        if language == "mermaid":
            errors.append("active Mermaid blocks are not allowed")
        elif language and language not in _TESTIMONY_LANGUAGES:
            errors.append(_LANGUAGE_ERROR)
    if _MERMAID_FENCE.search(text):
        errors.append("active Mermaid blocks are not allowed")
    pieces = _testimony_pieces(document)
    errors.extend(_vault_markup_errors(text, [piece for piece, kind in pieces if kind == "code"]))
    errors.extend(_github_errors(text, document, compare=not errors))
    # The rendering is checked as well as the source, so a character the
    # renderer produced would not pass unseen either.
    hidden = _hidden_characters(text + "".join(document.itertext()))
    if hidden:
        errors.append("invisible or reordering characters are not allowed: " + _named(hidden))
    # Inline elements end where the marks after them begin, so the marks of
    # adjacent ones fall on one character. Marks are counted on the canonical
    # decomposition, where a letter composed in advance carries its own.
    if _overstacked(unicodedata.normalize("NFD", text + "\n" + "".join(document.itertext()))):
        errors.append(
            f"more than {_MAX_COMBINING_MARKS} combining marks on one character, or more than "
            f"{_MAX_STACKED_MARKS} above or below it, are not allowed: they stack over the lines around it"
        )
    if any(_baseless_mark(piece) for piece, kind in pieces if kind != "math"):
        errors.append(
            "combining marks with no character before them in their text are not allowed: the browser draws them "
            "over the formula, mark, or border before them; write the character already composed, or an accent in "
            "a formula such as \\bar{z}"
        )
    # MathJax reads no text of a card outside the formulas the renderer
    # marked, so TeX left in text shows as typed; where GitHub reads it as a
    # formula, the comparison with GitHub's reading refuses it.
    layout = _TexLayout()
    for piece, kind in pieces:
        if kind == "math":
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
    occurrences of it in code. Formulas are not code there. Tags are counted
    by name, as :func:`_html_names` gives them, so a declaration's opener in
    code is not read on past the code to the next ``>``.
    """

    def outside_code(pattern: re.Pattern[str]) -> list[str]:
        found = Counter(match.group() for match in pattern.finditer(text))
        for piece in code:
            found.subtract(match.group() for match in pattern.finditer(piece))
        return [markup for markup, count in found.items() if count > 0]

    tags = Counter(_html_names(text))
    for piece in code:
        tags.subtract(_html_names(piece))
    entities = [
        _entity_name(entity)
        for entity in outside_code(_HTML_ENTITY)
        if entity[1] == "#" or entity[1:] in _NAMED_REFERENCES
    ]
    errors: list[str] = []
    if outside_code(re.compile("<!--")):
        errors.append("HTML comments are not allowed: Markdown viewers hide the text they enclose")
    if names := [name for name, count in tags.items() if count > 0]:
        errors.append("raw HTML is not allowed: " + _named(names) + "; in a formula, put a space after <")
    if outside_code(_AUTOLINK):
        errors.append("Markdown links, images, and autolinks are not allowed")
    if entities:
        errors.append(
            "HTML character references are not allowed: " + _named(entities) + "; type the character itself"
        )
    return errors


def _github_errors(text: str, document: object, *, compare: bool) -> list[str]:
    """What GitHub, which shows the vault's card files, shows of ``text``
    that the site, whose rendering is ``document``, does not.

    Python-Markdown and GitHub read some Markdown differently: lists, tables,
    code, emphasis, escapes, and formulas that one reads and the other shows
    as text, or reads to a different end. So the testimony is read a second
    time as GitHub reads it, by :func:`_github_html` and
    :func:`_github_formulas`, and is refused if that reading holds a link,
    heading, footnote, alert, or code fence the site would not show, a
    formula GitHub may read otherwise than emulated, or, compared by
    :func:`_shown`, shows anything else otherwise than the site does. The
    reading is compared only if ``compare``, when nothing more specific has
    been found, and the refusal names the line where the two first part and a
    way to write the testimony that both read alike.
    """

    github = html5lib.parseFragment(_github_html(text), namespaceHTMLElements=False)
    lines = text.split("\n")
    errors = _github_formulas(github, lines)
    code: set[int] = set()
    for element in github.iter():
        tag = element.tag.lower() if isinstance(element.tag, str) else ""
        if tag in {"a", "img"}:
            errors.append("Markdown links, images, and autolinks are not allowed")
        elif re.fullmatch("h[1-6]", tag):
            errors.append("Markdown headings are not allowed: they read as the page's own; use **bold** text")
        elif tag == "pre":
            first, last = (int(place.split(":")[0]) for place in element.get("data-sourcepos", "0:0-0:0").split("-"))
            code.update(range(first, last + 1))
            language = _fence_info(lines, element)[1].strip().lower()
            if language.split()[:1] == ["mermaid"]:
                errors.append("active Mermaid blocks are not allowed")
            elif language and language not in _TESTIMONY_LANGUAGES:
                errors.append(_LANGUAGE_ERROR)
        elif tag == "blockquote" and (first := element.find("p")) is not None and first is element[0]:
            if _ALERT.fullmatch((first.text or "").split("\n")[0]):
                errors.append(
                    "GitHub alerts are not allowed: GitHub shows a block quote that opens with [!NOTE] or the like "
                    "as its own notice; write the label as text"
                )
    for number, line in enumerate(lines, 1):
        if number not in code and (definition := _LINK_DEFINITION.match(line)):
            message = _FOOTNOTE_ERROR if definition[1] else "Markdown link definitions are not allowed"
            if message not in errors:
                errors.append(message)
    if errors or not compare:
        return errors
    site, shown = _shown(document), _shown(github)
    at = next(
        (index for index, (one, other) in enumerate(zip(site, shown)) if one[:2] != other[:2]),
        min(len(site), len(shown)),
    )
    if at == len(site) == len(shown):
        return errors
    line = (shown[at] if at < len(shown) else shown[-1] if shown else ("", "", 1))[2]

    def around(tokens: list[tuple[str, str, int]]) -> str:
        return "".join(
            "</p><p>" if kind == "break" else value if len(value) <= 40 else value[:37] + "..."
            for kind, value, _ in tokens[max(0, at - 8) : at + 8]
        )

    errors.append(
        f'testimony GitHub shows differently from the site is not allowed: on line {line} the site shows '
        f'"{around(site)}" where GitHub shows "{around(shown)}"; {_github_hint(site, shown, at)}'
    )
    return errors


def _github_hint(site: list[tuple[str, str, int]], github: list[tuple[str, str, int]], at: int) -> str:
    """How to write what the site and GitHub first show apart, at token
    ``at`` of each of :func:`_shown`, so that both show it alike."""

    one, other = (tokens[at][:2] if at < len(tokens) else ("", "") for tokens in (site, github))
    values = {one[1], other[1]}

    def opened(tokens: list[tuple[str, str, int]], tag: str) -> bool:
        before = [value for kind, value, _ in tokens[:at] if kind == "block"]
        return sum(value.startswith(f"<{tag}") for value in before) > before.count(f"</{tag}>")

    def next_block(tokens: list[tuple[str, str, int]]) -> str:
        return next((value for kind, value, _ in tokens[at:] if kind == "block" and value[:2] != "</"), "")

    in_table = opened(site, "table") or opened(github, "table")
    if "\t" in other[1] and "\t" not in one[1]:
        return "the site reads a tab in a formula or in code as spaces, and GitHub keeps it; write spaces for tabs"
    if one[0] == "math" and other in {("text", "("), ("text", "[")}:
        return (
            "GitHub reads \\( and \\[ as ( and [, not as the start of a formula; write a formula in a line of "
            "text as $`...`$, and a displayed one in a ```math fence"
        )
    if "math" in {one[0], other[0]}:
        if one[0] == "math" and "\n" in one[1]:
            return (
                "GitHub ends a formula at a line break, and may read the next line as a list; keep each "
                "formula in a line of text on one line"
            )
        if one[0] == other[0] and one[1][:2] == other[1][:2]:
            if in_table:
                return "in a table GitHub reads \\| as | even in a formula; write \\vert for | and \\Vert for \\| there"
            if one[1].startswith("\\["):
                return (
                    "GitHub reads Markdown escapes such as \\{ in $$...$$ first; write displayed math in a ```math "
                    "fence, whose TeX GitHub takes as written"
                )
            return (
                "GitHub reads Markdown escapes such as \\{ and \\_ in $...$ first; write the formula as $`...`$, "
                "whose TeX GitHub takes as written"
            )
        if one[:2] == ("text", "$") and other[0] == "math":
            return (
                "GitHub reads \\$ as a dollar sign before it looks for formulas, and pairs it with the next one; "
                "write dollar signs meant as typed in code, as `$x$`"
            )
        return (
            "GitHub reads $...$ as a formula only when the first $ starts a line or follows a space or (, no "
            "letter, digit, or _ follows the last, and it is not in emphasis; elsewhere write $`...`$, as in "
            "$`n`$th, with no letter, digit, _, or \\ just before it"
        )
    if values & {"<del>", "</del>"}:
        return (
            "GitHub strikes through text between tildes; write a space for a ~ that keeps words together, \\sim "
            "in a formula, or ~ in code"
        )
    if "<input>" in values:
        return "GitHub shows [ ], [x], or [X] that starts a list item as a checkbox; write \\[x] there"
    if "<br>" in values:
        return "GitHub reads a backslash at the end of a line as a line break; drop it"
    if next_block(github).startswith("<table") and one[0] == "text":
        return "GitHub reads a table right after a line of text; put a blank line before a table"
    if next_block(site).startswith("<table") and not next_block(github).startswith("<table"):
        return (
            "GitHub reads a table only when each cell of its delimiter row holds a -, with as many cells as the "
            "header; write the row as |---|---|"
        )
    if in_table:
        return (
            "GitHub splits a table row at every | not written \\|, in code and formulas too, and drops the "
            "backslash of \\| there; keep code that holds a | out of tables, and write \\vert in a formula"
        )
    lists = ("<ul>", "<ol ")
    if one[1] == "<li>" and other[1] in {"</ul>", "</ol>"} and github[at + 1 : at + 2] and github[at + 1][1].startswith(lists):
        return (
            "GitHub starts a new list where a bulleted list turns numbered or the other way; put a line of "
            "text between the two lists"
        )
    if one[1].startswith("<ol ") and other[1].startswith("<ol "):
        return "GitHub numbers a list from its first number and the site from 1; number each list from 1"
    if next_block(github).startswith(lists) and one[0] == "text":
        return (
            "GitHub reads a list right after a line of text, and a line that starts with -, +, *, or a number "
            "and . or ) as one; put a blank line before a list, and write \\-, \\+, \\*, or 1\\. where a line "
            "of text starts with one"
        )
    if any(value.startswith(("<ul", "<ol", "<li", "</ul", "</ol", "</li")) for value in values) or "break" in {
        one[0],
        other[0],
    }:
        return (
            "GitHub nests a list or paragraph under an item when it is indented as far as the item's text, and "
            "the site at four spaces; indent nested lists and an item's further paragraphs four spaces"
        )
    if any(value.startswith("<pre>") for value in values) or "code" in {one[0], other[0]}:
        return (
            "GitHub ends a code block at any fence at least as long as the one that opens it, or else at the end "
            "of the testimony, and the site only at a fence like the opening one; close each code block with the "
            "fence it opens with, starting where that one does, and nothing after it"
        )
    if values & {"<em>", "</em>", "<strong>", "</strong>"}:
        return "GitHub and the site read * and _ apart here; write \\* or \\_ for the character itself"
    if one[1] == "\\" and at + 1 < len(site) and site[at + 1][:2] == other:
        return f"GitHub hides a backslash before any punctuation, and the site only before some; drop the one before {other[1]}"
    return "put a blank line before lists, tables, and code, and indent nested lists four spaces"


def _github_html(text: str) -> str:
    """The HTML GitHub makes of ``text`` in a card file, before it marks the
    formulas in it."""

    return cmarkgfm.markdown_to_html_with_extensions(text, options=_GITHUB_OPTIONS, extensions=_GITHUB_EXTENSIONS)


def _fence_info(lines: list[str], pre: object) -> tuple[str, str]:
    """The fence and the info string of the code block ``pre`` GitHub read
    from ``lines``, or two empty strings for an indented code block."""

    line, column = (int(number) for number in pre.get("data-sourcepos", "1:1").split("-")[0].split(":"))  # type: ignore[attr-defined]
    start = lines[line - 1].encode("utf-8")[column - 1 :].decode("utf-8", "replace") if line <= len(lines) else ""
    fence = re.match(r"(`{3,}|~{3,})(.*)", start)
    return (fence[1], fence[2]) if fence else ("", "")


#: What GitHub reads as a displayed formula: a paragraph that is one, its TeX
#: being the paragraph's text after Markdown escapes.
_GITHUB_DISPLAY = re.compile(r"\$\$(.*)\$\$", re.DOTALL)
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]")
_DOLLAR_ADVICE = "keep dollar signs out of formulas"


def _github_formulas(document: object, lines: list[str]) -> list[str]:
    """Mark the formulas GitHub reads in its HTML ``document`` of ``lines``
    as the site marks its own, and name each place GitHub's reading is not
    known well enough to say what it shows.

    GitHub reads formulas from the text after Markdown has been read, with its
    escapes. Each rule here follows what its Markdown API returned for
    synthetic text, recorded in the tests: a paragraph that is only
    ``$$...$$``, at the top or in a block quote, and a ``math`` fence are
    displayed; code with a ``$`` right before and after it, and no letter,
    digit, or ``_`` before that, is a formula of the code's text; and a
    ``$`` that starts a line of text or follows a space, tab, or ``(``, and is
    followed by other than a space, opens a formula that the next ``$`` after
    other than a space or ``\\``, and before other than a letter, digit, or
    ``_``, closes. Formulas do not cross an element or line break, and none is
    read in emphasis. What the API was not seen to read, such as ``$$`` in a
    line or a formula holding ``$``, is named rather than guessed.
    """

    unsure: list[tuple[int, str]] = []

    def doubt(line: int, what: str, advice: str = "") -> None:
        advice = advice or "write a formula in a line of text as $`...`$, and a displayed one in a ```math fence"
        unsure.append((line, f"formulas GitHub may read otherwise are not allowed: on line {line}, {what}; {advice}"))

    def formula(tex: str, display: bool = False) -> object:
        element = etree.Element("p" if display else "span", {"class": "arithmatex"})
        element.text = f"\\[{tex}\\]" if display else f"\\({tex}\\)"
        return element

    def dollars(value: str, line: int) -> list[object]:
        read: list[object] = []
        for number, part in enumerate(value.split("\n")):
            read.append("\n" if number else "")
            start, opener = 0, None
            if "$$" in part:
                doubt(line + number, "two dollar signs together")
                read.append(part)
                continue
            for index, character in enumerate(part):
                if character != "$":
                    continue
                before, after = part[index - 1 : index], part[index + 1 : index + 2]
                if any(space.isspace() and space not in " \t" for space in before + after):
                    doubt(line + number, "a space other than a plain one next to a dollar sign")
                if (
                    opener is not None
                    and index > opener + 1
                    and not before.isspace()
                    and before != "\\"
                    and not _ASCII_WORD.match(after)
                ):
                    if "$" in part[opener + 1 : index]:
                        doubt(line + number, "a formula that holds a dollar sign", _DOLLAR_ADVICE)
                    if after in {"<", ">", "&"}:
                        doubt(
                            line + number,
                            f"{after} right after a formula, which GitHub shows as an HTML escape such as &lt;",
                            "write the formula as $`...`$",
                        )
                    read += [part[start:opener], formula(part[opener + 1 : index])]
                    start, opener = index + 1, None
                elif before in {"", " ", "\t", "("} and after.strip():
                    opener = index
            read.append(part[start:])
        return read

    # Elements are taken from a stack rather than by recursion, since raw HTML
    # nests them as deep as its tags go.
    stack: list[tuple[object, bool, int, bool, int]] = [(document, True, 0, False, 1)]
    while stack:
        node, top, quotes, emphasis, line = stack.pop()
        tag = node.tag.lower() if isinstance(node.tag, str) else ""  # type: ignore[attr-defined]
        if source := node.get("data-sourcepos"):  # type: ignore[attr-defined]
            line = int(source.split(":")[0]) or line
        items: list[object] = [node.text or ""]  # type: ignore[attr-defined]
        for child in list(node):  # type: ignore[attr-defined]
            items += [child, child.tail or ""]
            child.tail = None
            node.remove(child)  # type: ignore[attr-defined]
        at = line
        for index, item in enumerate(items):
            if isinstance(item, str):
                at += item.count("\n")
                continue
            name = item.tag.lower() if isinstance(item.tag, str) else ""
            start = int(item.get("data-sourcepos", "0").split(":")[0]) or at
            code = item.find("code") if name == "pre" else None
            display = _GITHUB_DISPLAY.fullmatch(item.text or "") if name == "p" and len(item) == 0 else None
            if code is not None and "language-math" in code.get("class", "").split():
                fence, info = _fence_info(lines, item)
                if quotes or fence[:1] != "`" or info.strip(" \t") != "math":
                    doubt(start, "a math fence other than ```math alone, outside a block quote")
                items[index] = formula((code.text or "").rstrip("\n"), display=True)
            elif display and (top or quotes == 1 and tag == "blockquote"):
                if "$" in display[1]:
                    doubt(start, "a displayed formula that holds a dollar sign", _DOLLAR_ADVICE)
                elif not display[1].strip():
                    doubt(start, "a displayed formula that holds nothing", "leave it out")
                items[index] = formula(display[1], display=True)
            elif name not in {"code", "pre"}:
                stack.append((item, False, quotes + (name == "blockquote"), emphasis or name == "em", at))
        at = line
        for index, item in enumerate(items):
            if isinstance(item, str):
                at += item.count("\n")
                continue
            before, after = (items[index - 1], items[index + 1]) if 0 < index < len(items) - 1 else ("", "")
            if item.tag != "code" or not (before.endswith("$") and after.startswith("$")):
                continue
            if not _ASCII_WORD.match(before[-2:-1]):
                if emphasis:
                    doubt(at, "a formula in emphasis")
                    continue
                if "$" in (item.text or ""):
                    doubt(at, "a formula that holds a dollar sign", _DOLLAR_ADVICE)
                items[index - 1 : index + 2] = [before[:-1], formula(item.text or ""), after[1:]]
        if not emphasis and tag not in {"code", "pre"}:
            read: list[object] = []
            at = line
            for item in items:
                read += dollars(item, at) if isinstance(item, str) else [item]
                at += item.count("\n") if isinstance(item, str) else 0
            items = read
        last = None
        node.text = ""  # type: ignore[attr-defined]
        for item in items:
            if not isinstance(item, str):
                node.append(item)  # type: ignore[attr-defined]
                last = item
            elif last is None:
                node.text += item  # type: ignore[attr-defined]
            else:
                last.tail = (last.tail or "") + item
    return [message for _, message in sorted(unsure, key=lambda place: place[0])]


#: The elements that start a block of their own, around which spaces show
#: nothing; inline elements and their ends are kept in place in the text.
_SHOWN_BLOCKS = frozenset(
    {"p", "blockquote", "ul", "ol", "li", "table", "tr", "th", "td", "hr", "br", "section", "div"}
    | {f"h{level}" for level in range(1, 7)}
)
_VOID_ELEMENTS = frozenset({"br", "hr", "img", "input"})


def _shown(document: object) -> list[tuple[str, str, int]]:
    """What ``document`` shows, in order, as ``(kind, value, line)``: each
    visible character, one space where any run of them shows, each code span,
    formula, and code block whole, and the start and end of each element,
    with the line of the source each comes from, where the document marks it.

    Two documents compared this way differ only in what a reader cannot tell
    apart. Spaces at the edge of a block, of a code span, or of a formula's
    TeX, and the newlines that end a code block, are dropped; an inline
    element's ends come before the spaces next to them; a paragraph in a list
    item is a break between what the item shows, so tight and loose lists
    alike; an empty paragraph, which HTML makes of the end of one the site
    wrote around a code block, and a table row of empty cells, which the site
    adds to a table that has none, show nothing and are dropped; text HTML
    leaves out of any paragraph after such a code block is framed as the
    paragraph GitHub makes of it, by :func:`_paragraphs`; table
    sections, a code block's language, and the classes and styles by which
    the two set formulas, tables, and code are left out.
    """

    shown: list[tuple[str, str, int]] = []
    line = 1
    pending = False

    def add(kind: str, value: str) -> None:
        nonlocal pending
        if kind in {"block", "break"}:
            pending = False
            if shown and shown[-1][0] == "break":
                shown.pop()
            if kind == "break" and (not shown or shown[-1][0] == "block"):
                return
        elif kind != "mark":
            last = next((item[0] for item in reversed(shown) if item[0] != "mark"), "block")
            if pending and last not in {"block", "break"}:
                shown.append(("text", " ", line))
            pending = False
        shown.append((kind, value, line))

    def text(value: str) -> None:
        nonlocal line, pending
        for character in value:
            if character.isspace():
                pending = True
                line += character == "\n"
            else:
                add("text", character)

    # The document is read from a stack of elements, text, and element ends
    # rather than by recursion, since raw HTML nests elements as deep as its
    # tags go.
    stack: list[object] = [(document, False)]
    while stack:
        entry = stack.pop()
        if isinstance(entry, str):
            text(entry)
            continue
        if len(entry) == 3:  # type: ignore[arg-type]
            add(entry[1], entry[2])  # type: ignore[index]
            continue
        node, in_item = entry  # type: ignore[misc]
        tag = node.tag.lower() if isinstance(node.tag, str) else "!--"  # type: ignore[attr-defined]
        if source := node.get("data-sourcepos"):  # type: ignore[attr-defined]
            line = int(source.split(":")[0]) or line
        if "arithmatex" in node.get("class", "").split():  # type: ignore[attr-defined]
            content = "".join(node.itertext())  # type: ignore[attr-defined]
            formula = content.strip()
            for kind, value in [("block", "<p>")] * (tag == "p") + [
                ("math", formula[:2] + formula[2:-2].strip() + formula[-2:])
            ] + [("block", "</p>")] * (tag == "p"):
                add(kind, value)
            line += content.count("\n")
            continue
        if tag == "pre":
            content = "".join(node.itertext())  # type: ignore[attr-defined]
            add("block", "<pre>" + content.rstrip("\n") + "</pre>")
            line += content.count("\n")
            continue
        if tag == "code":
            content = "".join(node.itertext())  # type: ignore[attr-defined]
            add("code", "<code>" + content.replace("\n", " ").strip(" ") + "</code>")
            continue
        empty = [node] if tag == "p" else list(node) if tag == "tr" else []  # type: ignore[call-overload]
        if empty and all(not (part.text or "").strip() and not len(part) for part in empty):
            continue
        kind, name = ("block" if tag in _SHOWN_BLOCKS else "mark"), tag
        if tag == "ol":
            name = f"ol start={node.get('start', '1')}"  # type: ignore[attr-defined]
        elif tag in {"td", "th"}:
            align = node.get("align") or "".join(re.findall(r"text-align: (\w+)", node.get("style", "")))  # type: ignore[attr-defined]
            name = f"{tag} align={align}" if align else tag
        if tag in {"thead", "tbody", "document_fragment"}:
            kind = ""
        elif tag == "p" and in_item:
            kind = "break"
        if kind:
            add(kind, "" if kind == "break" else f"<{name}>")
        later: list[object] = [node.text or ""]  # type: ignore[attr-defined]
        for child in node:  # type: ignore[attr-defined]
            later += [(child, tag == "li"), child.tail or ""]
        if tag in {"document_fragment", "blockquote"}:
            later = _paragraphs(later)
        if kind and tag not in _VOID_ELEMENTS:
            later.append((None, kind, "" if kind == "break" else f"</{tag}>"))
        stack += reversed(later)
    return shown


def _paragraphs(parts: list[object]) -> list[object]:
    """``parts``, the text and elements of a document or block quote in
    order, with each run of text and inline elements in it set between the
    ends of a paragraph, as the paragraph it shows as: HTML closes the
    paragraph the site writes around a code block at the block, and leaves
    the text after it, which GitHub sets as a paragraph, out of any."""

    framed: list[object] = []
    inline = False
    for part in parts:
        if isinstance(part, str):
            starts = bool(part.strip())
        else:
            child = part[0]  # type: ignore[index]
            name = child.tag.lower() if isinstance(child.tag, str) else ""
            starts = None if not name else name in {"br", "code"} or name not in _SHOWN_BLOCKS | {"pre"}
        if starts is None or starts == inline or not starts and isinstance(part, str):
            framed.append(part)
            continue
        framed += [(None, "block", "<p>"), part] if starts else [(None, "block", "</p>"), part]
        inline = starts
    return framed + [(None, "block", "</p>")] * inline


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
        errors.append(
            f"testimony has {brackets} opening brackets, over the limit of {TESTIMONY_MAX_BRACKETS}; in a formula "
            "write \\lbrack and \\rbrack for [ and ]"
        )
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
    # Every line that could be a delimiter row is taken for one, with its
    # header, the line before it, and the lines after it up to the next blank
    # one, which is as far as a table can run outside a block quote. A table
    # has as many columns as its header has cells, and the header no more
    # than one more than its pipes, less one for each pipe at either end.
    cells = following = 0
    for index in range(len(lines) - 1, 0, -1):
        if _TABLE_DELIMITER_ROW.fullmatch(lines[index]):
            header = lines[index - 1].lstrip(" >").rstrip(" ")
            columns = header.count("|") + 1 - header.startswith("|") - (len(header) > 1 and header.endswith("|"))
            cells += columns * (following + 1)
        following = 0 if _BLANK_LINE.fullmatch(lines[index]) else following + 1
    if cells > TESTIMONY_MAX_TABLE_CELLS:
        errors.append(f"testimony has tables of up to {cells} cells, over the limit of {TESTIMONY_MAX_TABLE_CELLS}")
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


#: How many of the things a message names it lists, and how much of each it
#: shows, so that it stays short whatever the testimony holds.
_MAX_NAMED = 8
_MAX_NAME_LENGTH = 64


def _named(names: Iterable[str]) -> str:
    """``names`` joined by commas: the first :data:`_MAX_NAMED`, each cut to
    :data:`_MAX_NAME_LENGTH` characters, and a count of the rest."""

    names = list(names)
    shown = [name if len(name) <= _MAX_NAME_LENGTH else name[: _MAX_NAME_LENGTH - 3] + "..." for name in names]
    rest = len(names) - _MAX_NAMED
    return ", ".join(shown[:_MAX_NAMED]) + (f", and {rest} more" if rest > 0 else "")


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


def _overstacked(text: str) -> bool:
    """Whether a character in ``text`` carries more combining marks, or more
    above or below it, than :data:`_MAX_COMBINING_MARKS` and
    :data:`_MAX_STACKED_MARKS` allow."""

    total = above = below = 0
    for character in text:
        if unicodedata.category(character) not in {"Mn", "Me"}:
            total = above = below = 0
            continue
        total += 1
        above += unicodedata.combining(character) in _MARKS_ABOVE
        below += unicodedata.combining(character) in _MARKS_BELOW
        if total > _MAX_COMBINING_MARKS or max(above, below) > _MAX_STACKED_MARKS:
            return True
    return False


def _baseless_mark(text: str) -> bool:
    """Whether a combining mark in ``text``, decomposed, starts it or follows
    whitespace, so that it has no character of its own to sit on."""

    previous = " "
    for character in unicodedata.normalize("NFD", text):
        if unicodedata.category(character) not in {"Mn", "Me"}:
            previous = character
        elif previous.isspace():
            return True
    return False


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

    With ``create``, missing directories on the way are made, and each new
    directory's name is flushed into its parent. Without it, nothing is
    created, and a missing directory means there is no card yet: ``None``.
    """

    if not ARTICLE_ID_PATTERN.fullmatch(article_id):
        raise ValueError(f"invalid article_id for a read-back: {article_id!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(blueprint, flags)
    except OSError as exc:
        raise ValueError(f"cannot safely open blueprint directory: {blueprint}: {exc.strerror}") from exc
    reached = blueprint
    try:
        for part in Path(READBACKS_DIR, article_id).parts:
            reached = reached / part
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
                    raise ValueError(f"cannot create read-back directory component: {reached}: {exc.strerror}") from exc
                else:
                    _flush_new_directory(current, reached)
                try:
                    following = os.open(part, flags, dir_fd=current)
                except OSError as exc:
                    raise _unsafe_component(reached, exc) from exc
            except OSError as exc:
                raise _unsafe_component(reached, exc) from exc
            # Move on before closing, so an interruption never leaves
            # ``current`` naming a closed descriptor.
            current, previous = following, current
            os.close(previous)
        directory, current = current, -1
        return directory
    finally:
        if current >= 0:
            os.close(current)


def _unsafe_component(reached: Path, exc: OSError) -> ValueError:
    """Why a directory on the way to a card could not be opened: a link only when it is one."""

    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return ValueError(f"refusing a symlink or unsafe component in the read-back path: {reached}: {exc.strerror}")
    return ValueError(f"cannot open read-back directory component: {reached}: {exc.strerror}")


def _flush_new_directory(parent: int, made: Path) -> None:
    """Flush a directory just made into ``parent``, so its name survives a crash with the card in it."""

    try:
        _flush(parent)
    except OSError as exc:
        # As with the card's own directory, the directory is in place; only
        # its surviving a crash of the whole system is in doubt.
        warnings.warn(
            f"read-back directory {made} was made, but its parent directory could not be flushed to disk: {exc}",
            RuntimeWarning,
            stacklevel=4,
        )


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

    ``None`` if there is no card. Only a regular file is read, and at most one
    byte past the card limit, which marks a card over it; opening never
    blocks, even on a FIFO. The bytes are hashed whatever they hold, so a
    card that is not UTF-8 can still be named, and replaced, by its hash.
    """

    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot safely inspect existing read-back: {display_path}: {exc.strerror}") from exc
    try:
        try:
            regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
            raw = _read_card_bytes(descriptor) if regular else None
        except OSError as exc:
            raise ValueError(f"cannot read existing read-back: {display_path}: {exc.strerror}") from exc
        if not regular:
            raise ValueError(f"read-back destination is not a regular file: {display_path}")
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
    holds the old complete card until it holds the new one. Each flush uses
    ``F_FULLFSYNC`` where the platform has it (macOS), whose plain ``fsync``
    can leave the data in the drive's cache. A failure or interruption before
    the rename removes the staging file. If removing it also fails, the file
    is left and named, by the error or, after an interruption, a warning. A
    second interruption while it is removed, a crash, SIGTERM, or SIGKILL
    can leave it unnamed; the loader never reads it as a card.
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
    left: str | None = None
    try:
        try:
            try:
                descriptor = os.open(
                    staged_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                )
            except OSError:
                # A failed exclusive create makes no file, and a name already
                # taken is not this write's file, so there is nothing to remove.
                staged_name = None
                raise
            try:
                written = 0
                while written < len(data):
                    written += os.write(descriptor, data[written:])
                os.fchmod(descriptor, 0o644)
                _flush(descriptor)
            finally:
                os.close(descriptor)
            os.replace(staged_name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            staged_name = None
        finally:
            if staged_name is not None and not _unlink_quietly(directory, staged_name):
                left = staged_name
    except OSError as exc:
        leftover = "" if left is None else f"; its temporary file {path.with_name(left)} could not be removed"
        raise ValueError(f"cannot publish read-back: {path}: {exc}{leftover}") from exc
    except BaseException:
        # An interruption carries no message to add the file to.
        if left is not None:
            warnings.warn(
                f"read-back {path} was not published, and its temporary file {path.with_name(left)} "
                "could not be removed",
                RuntimeWarning,
                stacklevel=3,
            )
        raise
    try:
        _flush(directory)
    except OSError as exc:
        # The card is in place and readable; only its surviving a crash of the
        # whole system is in doubt, so this is no reason to report a failure.
        warnings.warn(
            f"read-back {path} was published, but its directory could not be flushed to disk: {exc}",
            RuntimeWarning,
            stacklevel=3,
        )


def _unlink_quietly(directory: int, filename: str) -> bool:
    """Remove ``filename`` from ``directory``; whether it is gone."""

    try:
        os.unlink(filename, dir_fd=directory)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _flush(descriptor: int) -> None:
    """Flush a file or directory to stable storage: ``F_FULLFSYNC`` where the platform has it, else ``fsync``."""

    full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
    if full_fsync is not None:
        try:
            fcntl.fcntl(descriptor, full_fsync)
            return
        except OSError as exc:
            # Not every file system takes it; a plain fsync is the next best.
            # Any other failure, such as an I/O error, is the flush's, and a
            # plain fsync that only reaches the drive's cache would hide it.
            if exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL):
                raise
    os.fsync(descriptor)


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
    "readback_for",
    "readback_keys",
    "readback_path",
    "write_readback",
]
