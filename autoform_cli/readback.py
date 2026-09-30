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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Iterator, Mapping
from urllib.parse import unquote_to_bytes

import html5lib
import markdown as markdown_renderer

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
#: Raw HTML, found before the Markdown renderer runs: open and closing tags in
#: CommonMark's grammar, the openers of declarations and processing
#: instructions, closed or not, and character references in the forms HTML
#: parsers decode. Anything else the renderer reads as HTML is refused after
#: parsing. Autolinks are not tags here; they are refused after parsing, as
#: links.
_HTML_TAG = re.compile(
    r"<\?|<![A-Za-z\[]"
    r"|</?[A-Za-z][A-Za-z0-9-]*"
    r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*\s*/?>"
)
_HTML_NAME = re.compile(r"<[!?](?:\[CDATA\[|[A-Za-z]*)|</?[A-Za-z][A-Za-z0-9-]*")
_HTML_ENTITY = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9A-Fa-f]+;?|[A-Za-z][A-Za-z0-9]*;)")


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
    only when it names that content's hash. Each conflict names the hash to
    pass. Publishing still checks each card, since another writer can act in
    between.
    """

    conflicts: list[str] = []
    for card in cards:
        before = _existing_card_hash(card.blueprint, card.article_id, card.path)
        if before is None or before == evidence_hash_of(card.content):
            continue
        if card.expected_card_hash is None:
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
    directory was moved meanwhile, this raises instead of returning a path that
    does not lead to it. The check walks from the blueprint without following
    links, as the loader does, and runs before the directory's lock is
    released, so no other publication can change the answer.
    """

    parent_descriptor = _open_card_parent(prepared.blueprint, prepared.article_id)
    try:
        _lock_card_directory(parent_descriptor, prepared.path)
        published = _publish_card(
            parent_descriptor,
            prepared.path,
            prepared.content,
            expected_card_hash=prepared.expected_card_hash,
        )
        reported = _card_identity(prepared.blueprint, prepared.article_id, prepared.path)
    finally:
        os.close(parent_descriptor)
    if reported != published:
        raise ValueError(
            f"cannot confirm the published read-back: {prepared.path} no longer names it; "
            "its directory was moved or replaced, or an editor replaced the card, during publication"
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
    They do not need links or embedded content. Parsing with the same relevant
    Markdown extensions catches reference links and attribute-list handlers
    that lexical URL filtering misses.

    A testimony is measured against :data:`TESTIMONY_MAX_BYTES` and the other
    limits first, and one that exceeds any of them is refused unparsed: every
    card in a pull request is validated, so parsing must be bounded before
    anything is known about it. Raw HTML is refused unparsed too. What is
    then checked is what a reader is shown: no invisible or reordering
    characters; in text the typesetter reads, only the TeX listed in
    :data:`_TESTIMONY_TEX`, with arguments that show something and spacing
    that neither overlaps symbols nor pushes them apart; and at least one
    visible letter or digit.
    """

    if limits := _testimony_limit_errors(text):
        return limits
    if markup := _raw_html_errors(text):
        return markup
    parser = markdown_renderer.Markdown(
        extensions=list(SITE_EXTENSIONS),
        extension_configs=SITE_EXTENSION_CONFIGS,
    )
    rendered = parser.convert(text)
    errors: list[str] = []
    if parser.htmlStash.rawHtmlBlocks:
        errors.append("raw HTML is not allowed")
    if parser.references:
        errors.append("Markdown link definitions are not allowed")
    document = html5lib.parseFragment(rendered, namespaceHTMLElements=False)
    for element in document.iter():
        tag = str(element.tag).lower()
        attributes = {str(name).lower() for name in element.attrib}
        classes = set(str(element.attrib.get("class", "")).split())
        if tag in {"a", "img"}:
            errors.append("Markdown links, images, and autolinks are not allowed")
        if tag == "script" and not element.attrib.get("type", "").startswith("math/tex"):
            errors.append("active HTML is not allowed")
        if "style" in attributes or any(name.startswith("on") for name in attributes):
            errors.append("active Markdown attributes are not allowed")
        if "hidden" in attributes or "aria-hidden" in attributes:
            errors.append("visibility-changing Markdown attributes are not allowed")
        if "mermaid" in classes:
            errors.append("active Mermaid blocks are not allowed")
        if attributes and not _renderer_owned_attributes(tag, element.attrib):
            errors.append("user-supplied Markdown attributes are not allowed")
    if re.search(r"^ {0,3}(?:`{3,}|~{3,})[ \t]*mermaid(?:[ \t]|$)", text, re.MULTILINE | re.IGNORECASE):
        errors.append("active Mermaid blocks are not allowed")
    # Entities are decoded in the parsed document, so `&#8203;` is caught here
    # as surely as a typed zero-width space.
    hidden = _hidden_characters(text + "".join(document.itertext()))
    if hidden:
        errors.append("invisible or reordering characters are not allowed: " + ", ".join(hidden))
    typeset = _typeset_text(document)
    errors.extend(_tex_errors(typeset))
    if _TEX_COMMENT.search(typeset):
        errors.append("TeX comments are not allowed: they drop the rest of their line; write \\% for a percent sign")
    if not _shows_letter_or_digit("".join(document.itertext())):
        errors.append("testimony renders no visible text: it must show at least one letter or digit")
    return tuple(dict.fromkeys(errors))


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


def _typeset_text(document: object) -> str:
    """The text MathJax may typeset: everything outside code and preformatted
    blocks, which it skips. Where the Markdown renderer recognized no formula,
    MathJax can still find one, so the checks read all of this text rather
    than only the spans the renderer marked as math."""

    parts: list[str] = []

    def walk(node: object, skipped: bool) -> None:
        tag = getattr(node, "tag", None)
        if not isinstance(tag, str):
            return
        skipped = skipped or tag.lower() in {"code", "pre"}
        if not skipped and getattr(node, "text", None):
            parts.append(node.text)  # type: ignore[attr-defined]
        for child in node:  # type: ignore[attr-defined]
            walk(child, skipped)
            if not skipped and getattr(child, "tail", None):
                parts.append(child.tail)

    walk(document, False)
    return "".join(parts)




def _renderer_owned_attributes(tag: str, attributes: Mapping[str, str]) -> bool:
    """Allow only attributes emitted by the configured Markdown renderer."""

    normalized = {str(name).lower(): str(value) for name, value in attributes.items()}
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return set(normalized) == {"id"}
    if tag == "script":
        return set(normalized) == {"type"} and normalized["type"].startswith("math/tex")
    classes = set(normalized.get("class", "").split())
    if set(normalized) != {"class"}:
        return False
    if tag in {"span", "div"} and classes in ({"arithmatex"}, {"MathJax_Preview"}):
        return True
    if tag == "div" and classes in ({"highlight"}, {"linenodiv"}):
        return True
    if tag == "table" and classes == {"highlighttable"}:
        return True
    if tag == "td" and classes in ({"linenos"}, {"code"}):
        return True
    if tag == "span" and len(classes) == 1:
        token = next(iter(classes))
        return token in {"hll", "normal"} or re.fullmatch(r"[a-z][a-z0-9]{0,3}", token) is not None
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


def _open_card_parent(blueprint: Path, article_id: str) -> int:
    """Open the card directory through held, no-follow directory descriptors."""

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
            os.close(current)
            current = following
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
            os.close(current)
            current = following
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
        return _card_hash_at(directory, path.name, path)
    finally:
        os.close(directory)


def _card_identity(blueprint: Path, article_id: str, path: Path) -> tuple[int, int] | None:
    """The device and inode ``path`` names, reached without following a link."""

    try:
        directory = _open_existing_card_parent(blueprint, article_id)
    except ValueError:
        return None
    if directory is None:
        return None
    try:
        found = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(directory)
    return found.st_dev, found.st_ino


def _card_hash_at(directory: int, filename: str, display_path: Path) -> str | None:
    """Read a card relative to a held directory without following links.

    Only a regular file is read; opening never blocks, even on a FIFO.
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
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            try:
                text = stream.read().decode("utf-8")
            except UnicodeError as exc:
                raise ValueError(f"existing read-back is not UTF-8: {display_path}") from exc
        return evidence_hash_of(text)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _lock_card_directory(directory: int, path: Path) -> None:
    """Take the card directory's exclusive lock, so its publications happen one at a time.

    The lock belongs to the held descriptor: closing it, or the process
    dying, releases it, so a crash never leaves it held.
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


def _publish_card(
    directory: int,
    path: Path,
    content: str,
    *,
    expected_card_hash: str | None,
) -> tuple[int, int]:
    """Publish a card through a held, locked directory descriptor under compare-and-swap.

    The caller holds the directory's lock, so no other publication acts
    between the compare and the swap. The new card is staged beside the old
    one and exchanged with it in one step, so the card's name always holds a
    complete card. The card swapped out must be the one compared: if an
    editor saved over it in between, it is swapped back and the write reports
    a conflict. A first card is renamed into place without replacing a name.
    Writers that work by path (renaming over the card, deleting it, or
    rewriting it through a descriptor opened with truncation) are never lost;
    one that holds the old card open and writes to it after the swap writes to
    the replaced file, which is deleted.
    On any exception, a card still swapped out is swapped back; a card that
    cannot be put back is left under the staging name and named, never
    deleted. A crash can leave that name holding the card just replaced, or
    the unpublished card; the loader never reads it as a card.

    Returns the device and inode of the card now filed under ``path.name``.
    """

    filename = path.name
    content_hash = evidence_hash_of(content)
    staged_name, staged_identity = _stage_card(directory, path, content)
    staged_path = path.with_name(staged_name)
    settled = False
    preserved: str | None = None
    try:
        before = _card_hash_at(directory, filename, path)
        if before == content_hash:
            existing = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            return existing.st_dev, existing.st_ino
        if before is not None and expected_card_hash is None:
            raise ValueError(
                f"read-back already exists with different content: {path}; retry with expected_card_hash={before!r}"
            )
        if before != expected_card_hash:
            raise ValueError(
                f"read-back changed before replacement: expected {expected_card_hash!r}, found {before!r}"
            )
        conflict = f"read-back changed concurrently while writing: {path}"
        staged = os.stat(staged_name, dir_fd=directory, follow_symlinks=False)
        if (staged.st_dev, staged.st_ino) != staged_identity:
            raise ValueError(f"read-back staging file changed before publication: {path}")
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
                preserved = _restore_card(directory, staged_name, staged_identity, content_hash, filename)
                settled = True
                raise ValueError(conflict if preserved else f"{conflict}; the other writer's card was kept")
            try:
                os.unlink(staged_name, dir_fd=directory)
            except OSError as exc:
                warnings.warn(
                    f"could not remove the replaced read-back left at {staged_path}: {exc}",
                    RuntimeWarning,
                    stacklevel=3,
                )
        settled = True
    except BaseException as exc:
        # Decided from what the staging name holds, not from how far the code
        # got: an interruption can land between a rename and the next line.
        if not settled and _holds_staged_card(directory, staged_name, staged_identity, content_hash) is False:
            preserved = _restore_card(directory, staged_name, staged_identity, content_hash, filename)
        if preserved is None:
            raise
        note = f"a card that could not be put back was preserved at {path.with_name(preserved)}"
        if isinstance(exc, ValueError):
            raise ValueError(f"{exc}; {note}") from exc
        warnings.warn(f"read-back publication interrupted: {note}", RuntimeWarning, stacklevel=3)
        raise
    finally:
        if _holds_staged_card(directory, staged_name, staged_identity, content_hash):
            _unlink_quietly(directory, staged_name)
    try:
        os.fsync(directory)
    except OSError:
        pass
    return staged_identity


def _stage_card(directory: int, path: Path, content: str) -> tuple[str, tuple[int, int]]:
    """Write ``content`` to a new, uniquely named file beside the card.

    Returns the staging file's name and its device and inode.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(100):
        name = f"{_WORK_PREFIX}{secrets.token_hex(12)}.tmp"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        except FileExistsError:
            continue
        break
    else:
        raise ValueError(f"cannot allocate a unique staging file for read-back: {path}")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
            staged = os.fstat(stream.fileno())
    except BaseException:
        _unlink_quietly(directory, name)
        raise
    return name, (staged.st_dev, staged.st_ino)


def _card_matches(directory: int, filename: str, display_path: Path, expected: str) -> bool:
    """Whether ``filename`` is a regular UTF-8 card with hash ``expected``."""

    try:
        return _card_hash_at(directory, filename, display_path) == expected
    except ValueError:
        return False


def _holds_staged_card(
    directory: int, staged_name: str, staged_identity: tuple[int, int], content_hash: str
) -> bool | None:
    """Whether the staging name still holds the staged card, unchanged; ``None`` if it holds nothing.

    ``False`` means it holds a card swapped out of the card's name, or the
    staged card after someone edited it while it was published.
    """

    try:
        found = os.stat(staged_name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    if (found.st_dev, found.st_ino) != staged_identity:
        return False
    return _card_matches(directory, staged_name, Path(staged_name), content_hash)


def _restore_card(
    directory: int, staged_name: str, staged_identity: tuple[int, int], content_hash: str, filename: str
) -> str | None:
    """Swap the card a publication replaced back under its name.

    Returns ``None`` once it is back and the staging name again holds the
    unpublished card, or nothing. Otherwise returns the name a card was left
    under: the replaced card, if it could not be put back, or whatever an
    editor saved over the new card meanwhile.
    """

    try:
        atomic_rename(staged_name, filename, directory=directory, exchange=True)
    except FileNotFoundError:
        # The new card was deleted meanwhile; restore the old one unless the
        # name has been taken again.
        try:
            atomic_rename(staged_name, filename, directory=directory)
        except (OSError, NotImplementedError):
            return staged_name
        return None
    except (OSError, NotImplementedError):
        return staged_name
    return None if _holds_staged_card(directory, staged_name, staged_identity, content_hash) else staged_name


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
    "readback_conflicts",
    "readback_findings",
    "readback_path",
    "write_readback",
]
