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
records the statement hash of the skeleton it testifies about, which a proof
edit leaves unchanged, and the evidence hash of the packet text it was written
from. When either no longer matches the prepared declaration, validation
reports the card invalid, rather than silently presenting it as current.

Testimony is plain text with TeX formulas between dollar signs. Every reader
shows the text as typed: GitHub and a Markdown viewer of the vault inside the
card's text block, the site escaped. Only the site typesets the formulas, and
:func:`split_testimony` is the one reading of where they are, so the site
typesets what the validator checked.
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
from pathlib import Path
from typing import Iterable, Iterator, Mapping
from urllib.parse import unquote_to_bytes

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows, which cannot publish cards
    fcntl = None  # type: ignore[assignment]

from .graph import ARTICLE_ID_PATTERN
from .skeleton import (
    DeclarationSkeleton,
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
#: The card body: the packet in a Lean block, then the testimony in a text
#: block, each fenced one backtick longer than the longest run in it, so
#: nothing in either can close its block.
_CARD_BODY = re.compile(
    rf"\n{SKELETON_HEADING}\n\n(?P<packet_fence>`{{3,}})lean\n(?P<packet>.*?)(?P=packet_fence)\n\n"
    rf"{READBACK_HEADING}\n\n(?P<testimony_fence>`{{3,}})text\n(?P<testimony>.*)\n(?P=testimony_fence)\n?\Z",
    re.DOTALL,
)
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

#: Characters that render as nothing, or reorder the text around them, so a
#: card could say more or other than a reader sees. Unicode's general
#: categories, in the Unicode data of the Python that runs the check
#: (``unicodedata.unidata_version``), catch most: controls, format characters
#: such as zero-width spaces and bidirectional overrides, separators, and
#: private-use code points. Spaces other than the ASCII one are blank too, and
#: a browser does not collapse them, so a run of them pushes text aside or off
#: the card. Letters and digits of the right-to-left bidirectional classes
#: reorder the characters around them. The rest are listed by code point: the
#: Default_Ignorable_Code_Point ranges, copied from Unicode 17.0's
#: DerivedCoreProperties.txt because unicodedata does not expose that property,
#: which renderers show as nothing whether or not a code point in them is
#: assigned, and blank letters and symbols outside them. Code points this
#: Python's Unicode data leaves unassigned (category Cn) are refused apart, by
#: :func:`_unassigned_characters`: whether one is visible depends on the
#: Unicode version of the reader's fonts, not on anything this check knows.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})
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
#: Line ends are written as ``\n``, so a tab is the only other control.
_ALLOWED_CONTROLS = frozenset("\t\n")
#: How many combining marks one character may carry, and how many of those
#: may stack above it or below it, by canonical combining class. The scripts
#: written with them put at most three or four on a letter, and two above or
#: below, as Vietnamese and polytonic Greek do; each more stacks further over
#: the line above or below.
_MAX_COMBINING_MARKS = 4
_MAX_STACKED_MARKS = 2
_MARKS_ABOVE = frozenset({214, 216, 228, 230, 232, 234})
_MARKS_BELOW = frozenset({202, 218, 220, 222, 233})

#: How large a testimony may be. Every card in a pull request is validated
#: before anything is known about it, and every check is a linear scan, so the
#: bytes bound the work; the lines bound how far blank lines can push text
#: down the card. Both are several times what 120 read-backs of a real-analysis
#: textbook use (at most 5.9 KB and 62 lines).
TESTIMONY_MAX_BYTES = 32 * 1024
TESTIMONY_MAX_LINES = 500

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
#: The TeX testimony may use: the notation statements need, out of what the
#: site's pinned MathJax 3.2.2 defines in the base and ams packages, the only
#: ones a card's formulas are typeset with. Nothing listed spaces, moves,
#: hides, colours, boxes, resizes, labels, or numbers content, and nothing
#: defines a macro. Any other command is refused by name, so extending this is
#: a deliberate edit, to be checked against that MathJax version: b27a900e
#: found that a list of what to refuse left such commands through.
_TESTIMONY_TEX = _TEX_LETTERS | frozenset(
    r"""
    \{ \} \| \# \$ \% \& \_ \\
    \lt \gt \le \leq \ge \geq \ne \neq \ll \gg \leqslant \geqslant \nleq \ngeq \lneq \gneq
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
    \Longleftarrow \Longleftrightarrow \hookrightarrow \hookleftarrow \iff \implies \impliedby
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
    \infty \partial \nabla \emptyset \varnothing \prime \backprime \forall \exists \nexists
    \neg \lnot \top \bot \angle \measuredangle \sphericalangle \triangle \vartriangle
    \blacktriangle \bigtriangleup \bigtriangledown \backslash \complement \therefore \because
    \colon \cdotp \ldotp \ldots \cdots \vdots \ddots \dots \dotsc \dotsb \dotsi \dotsm \dotso \Box
    \square \blacksquare \Diamond \lozenge \bigstar \checkmark \flat \sharp \natural \surd \mho \Finv
    \Game \S \yen \circledR \maltese \circledS \bigcirc \blacklozenge \blacktriangledown
    \blacktriangleleft \blacktriangleright \triangledown \diagup \diagdown \clubsuit \diamondsuit
    \heartsuit \spadesuit \And \smallint
    \pm \mp \times \div \cdot \centerdot \ast \star \circ \bullet \cap \cup \Cap \Cup \setminus
    \smallsetminus \wedge \vee \land \lor \barwedge \veebar \doublebarwedge \curlywedge
    \curlyvee \oplus \ominus \otimes \oslash \odot \circledast \circleddash \dotplus \sqcap
    \sqcup \uplus \amalg \dagger \ddagger \diamond \intercal \wr \divideontimes \ltimes \rtimes
    \leftthreetimes \rightthreetimes \lhd \rhd \unlhd \unrhd \boxplus \boxminus \boxtimes
    \boxdot \circledcirc \divsymbol \doublecap \doublecup \bmod \mod \pmod
    \sum \prod \coprod \int \iint \iiint \oint \bigcup \bigcap \bigoplus \bigotimes \bigvee
    \bigwedge \bigsqcup \biguplus \bigodot \lim \liminf \limsup \varinjlim \varprojlim \sup
    \inf \max \min \sin \cos \tan \sec \csc \cot \sinh \cosh \tanh \coth \arcsin \arccos
    \arctan \log \ln \lg \exp \det \dim \ker \deg \gcd \hom \arg \Pr \iiiint \injlim
    \projlim \varliminf \varlimsup \operatorname \limits \nolimits
    \langle \rangle \lfloor \rfloor \lceil \rceil \vert \Vert \lvert \rvert \lVert \rVert
    \lbrace \rbrace \lbrack \rbrack \lgroup \rgroup \ulcorner \urcorner \llcorner \lrcorner
    \left \right \middle \big \Big \bigg \Bigg \bigl \bigr \Bigl \Bigr \biggl \biggr \Biggl \Biggr
    \frac \dfrac \tfrac \binom \dbinom \tbinom \overset \underset \stackrel \sqrt \xrightarrow \xleftarrow
    \hat \bar \tilde \vec \dot \ddot \check \breve \acute \grave \mathring \widehat
    \widetilde \overline \underline \overrightarrow \overleftarrow \overleftrightarrow \underleftarrow
    \underrightarrow \overbrace \underbrace
    \mathbb \Bbb \mathcal \mathfrak \mathscr \mathrm \mathbf \mathsf \mathit \mathtt
    \text \textrm \textbf \textit \texttt \textsf \textnormal \textup \mbox
    \begin \end
    """.split()
)
#: The environments testimony may use, all of them rows of cells.
_TEX_ENVIRONMENTS = frozenset(
    {"cases", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix", "aligned", "gathered"}
)
#: What to write for commands and environments outside the allowlist.
_TEX_INSTEAD = {
    r"\choose": r"\binom{n}{k} for {n \choose k}",
    r"\over": r"\frac{a}{b} for {a \over b}",
    r"\cfrac": r"\dfrac for \cfrac",
    r"\genfrac": r"\frac or \binom for \genfrac",
    r"\boxed": r"the boxed formula alone, without \boxed",
    r"\not": r"\neq, \notin, or another negated relation for \not",
    **dict.fromkeys(
        (r"\,", r"\:", r"\;", r"\!", r"\quad", r"\qquad", "\\ ", r"\hspace", r"\enspace"),
        "the formula without spacing commands, which MathJax spaces itself",
    ),
    r"\begin{array}": r"\begin{matrix} for \begin{array}",
    **dict.fromkeys(
        (r"\begin{equation}", r"\begin{align}", r"\begin{gather}", r"\begin{multline}"),
        r"$$...$$ around \begin{aligned} or \begin{gathered}",
    ),
}
#: The characters past ASCII MathJax 3.2.2 sets in a formula: those in the
#: ranges of its operator dictionary (OperatorDictionary.RANGES), merged here,
#: but for U+20000 to U+2FA1F, which its CommonHTML and SVG output throw for.
#: Any other gets an error in place of the formula, or stops the typesetting.
_TEX_CHARACTER_RANGES = (
    (0x00A0, 0x024F), (0x02B0, 0x1A20), (0x1AB0, 0x209F), (0x2100, 0x23FF), (0x2460, 0x2DE0), (0x2E00, 0x2FDF),
    (0x2FF0, 0xA49F), (0xA4D0, 0xD7FF), (0xF900, 0x1D25F), (0x1D360, 0x1D37F), (0x1D400, 0x1D7FF),
    (0x1DF00, 0x1F9FF),
)
#: How long a formula may be, in UTF-16 code units, so a character past the
#: Basic Multilingual Plane counts twice. MathJax 3.2.2 refuses a formula once
#: expanding a command makes what is left to read longer than 5120 (its
#: ``maxBuffer``), so 2048 keeps every formula under it with room to spare.
_TEX_MAX_LENGTH = 2048
#: How deep one formula may nest groups, ``\left``, and environments: more than
#: twice what statements use, and far below the depth near 200 at which
#: MathJax 3.2.2 overflows its stack.
_TEX_MAX_DEPTH = 16
#: What a formula is read as: control sequences, and the characters that open,
#: close, or end something, space, comment, or end the formula.
_TEX_TOKEN = re.compile(r"\\(?:[A-Za-z]+|.)|[{}&~%$]", re.DOTALL)
_TEX_ENVIRONMENT = re.compile(r"\s*\{([^{}\\]{0,64})\}")
#: ``\begin{aligned}[t]`` and its kin read a bracket after any space for
#: where the rows sit, and show nothing written there; a star or bracket right
#: after ``\\`` spaces rows apart, or with a negative dimension draws one
#: over another.
_TEX_BRACKET_AFTER = re.compile(r"\s*\[")
_TEX_ROW_OPTION = re.compile(r"[*\[]")
_TEX_ENVIRONMENT_NAME = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
_TEX_CONTROL_SEQUENCE = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)
#: A dollar sign, or a backslash and the character it escapes, which is never
#: a delimiter: ``\$`` is a dollar sign in a formula and shown as typed in text.
_DOLLAR = re.compile(r"\\.|\$\$?", re.DOTALL)


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

    def validate(self, expected: DeclarationSkeleton | None = None) -> tuple[str, ...]:
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
        elif all(isinstance(value, str) for value in (
            self.article_id, self.declaration, self.skeleton_hash, self.packet_hash, self.model, self.shown_text,
            self.text,
        )):
            canonical = _card_content(
                article_id=self.article_id,
                declaration=self.declaration,
                skeleton_hash=self.skeleton_hash,
                packet_hash=self.packet_hash,
                model=self.model,
                packet_text=self.shown_text,
                testimony=self.text,
            )
            if self.file_hash != evidence_hash_of(canonical):
                errors.append("whole-file hash does not match the canonical card bytes")
        if expected is not None:
            if self.declaration != expected.name:
                errors.append(f"card identity is {self.declaration!r}, expected {expected.name!r}")
            if self.skeleton_hash != expected.statement_hash:
                errors.append(f"skeleton hash is {self.skeleton_hash!r}, expected {expected.statement_hash}")
            if self.packet_hash != expected.evidence_hash:
                errors.append(f"packet hash is {self.packet_hash!r}, expected {expected.evidence_hash}")
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
        skeleton_hash, packet_hash = (_hash_or_none(metadata.get(field)) for field in ("skeleton", "packet"))
        for field, recorded in (("skeleton", skeleton_hash), ("packet", packet_hash)):
            if recorded is None:
                errors.append(f"card records {'no' if metadata.get(field) is None else 'a malformed'} {field} hash")
        model = metadata.get("model")
        if model is None or not model.strip():
            errors.append("card records no nonempty model label")
        shown_text, testimony = None, ""
        if (match := _CARD_BODY.fullmatch(body)) is None:
            errors.append("card body must be one skeleton block followed by one read-back block")
        else:
            shown_text, testimony = match["packet"], match["testimony"]
            if testimony.strip():
                errors.extend(_testimony_errors(testimony))
            else:
                errors.append("card contains no nonempty read-back testimony")
        shown_hash = evidence_hash_of(shown_text) if shown_text is not None else None
        if shown_hash is not None and packet_hash is not None and shown_hash != packet_hash:
            errors.append(_ALTERED_PACKET)
        if None not in (recorded_article_id, recorded_declaration, skeleton_hash, packet_hash, model, shown_text):
            canonical = _card_content(
                article_id=recorded_article_id,
                declaration=recorded_declaration,
                skeleton_hash=skeleton_hash,
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
    if path.is_symlink():  # where there is no O_NOFOLLOW
        return None
    try:
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
    skeleton_hash: str,
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
    declaration's is for :meth:`Readback.validate` to judge, against a fresh
    extraction. Line ends in the testimony are written as ``\\n``.
    """

    blueprint_path = Path(blueprint).expanduser().resolve()
    path = readback_path(blueprint_path, article_id, declaration)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not model.strip():
        raise ValueError("a read-back requires a nonempty model label")
    if not _safe_model_label(model):
        raise ValueError("a read-back model label must be printable, single-line text")
    if not text.strip():
        raise ValueError("a read-back requires nonempty testimony")
    if testimony_errors := _testimony_errors(text):
        raise ValueError("unsafe read-back testimony: " + "; ".join(testimony_errors))
    if expected_card_hash is not None and _hash_or_none(expected_card_hash) is None:
        raise ValueError(f"invalid expected card hash: {expected_card_hash!r}")
    if _hash_or_none(skeleton_hash) is None:
        raise ValueError(f"invalid skeleton hash: {skeleton_hash!r}")
    if _hash_or_none(packet_hash) is None or evidence_hash_of(packet_text) != packet_hash:
        raise ValueError(f"read-back packet does not match its packet hash for {declaration}")
    return PreparedReadback(
        blueprint=blueprint_path,
        article_id=article_id,
        declaration=declaration,
        path=path,
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
    path = prepared.path
    directory = _open_card_directory(prepared.blueprint, prepared.article_id)
    try:
        _lock_card_directory(directory, path)
        _publish_card(path, prepared.content, expected_card_hash=prepared.expected_card_hash)
    finally:
        _release_card_directory(directory)
    return path


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


# --------------------------------------------------------------------------- #
# Testimony
# --------------------------------------------------------------------------- #


def split_testimony(text: str) -> list[tuple[str, str]]:
    """``text`` split into ``("text", ...)``, ``("inline", ...)``, and ``("display", ...)`` pieces, in order.

    Dollar signs are taken in turn: ``$$`` opens a displayed formula that the
    next ``$$`` closes, and a single ``$`` a formula in the line that the
    next ``$`` closes. A backslash and the character after it are never a
    delimiter. The site typesets each formula piece and shows the text as
    typed, so this is the only reading of where formulas are. A dollar sign no
    other closes is left in the text, which :func:`_testimony_errors` refuses.
    """

    pieces: list[tuple[str, str]] = []
    start = position = 0
    opened: str | None = None
    while (token := _DOLLAR.search(text, position)) is not None:
        position = token.end()
        delimiter = token.group()
        if delimiter.startswith("\\"):
            continue
        if opened is None:
            pieces.append(("text", text[start : token.start()]))
            opened, start = delimiter, token.end()
        elif delimiter.startswith(opened):
            pieces.append(("display" if opened == "$$" else "inline", text[start : token.start()]))
            # A formula in the line ends at the first of two dollar signs; the
            # second opens the next.
            opened, start = None, token.start() + len(opened)
            position = start
    pieces.append(("text", text[start - len(opened or "") :]))
    return [(kind, piece) for kind, piece in pieces if piece or kind != "text"]


def _testimony_errors(text: str) -> tuple[str, ...]:
    """Reject testimony that says more or other than a reader sees, or is too large to check.

    The text is shown as typed, so what a reader could be kept from seeing is
    in its characters: invisible and reordering ones, marks stacked over the
    lines around, and marks with nothing to sit on. The formulas are
    typeset, so they may use only the TeX in :data:`_TESTIMONY_TEX`, and
    must show at least one letter or digit between them and the text.
    """

    size = len(text.encode("utf-8", "surrogatepass"))
    if size > TESTIMONY_MAX_BYTES:
        return (f"testimony is {size} bytes, over the {TESTIMONY_MAX_BYTES}-byte limit",)
    if (lines := text.count("\n") + 1) > TESTIMONY_MAX_LINES:
        return (f"testimony has {lines} lines, over the {TESTIMONY_MAX_LINES}-line limit",)
    errors: list[str] = []
    if hidden := _hidden_characters(text):
        errors.append("invisible or reordering characters are not allowed: " + _named(hidden))
    if unassigned := _unassigned_characters(text):
        errors.append(
            f"characters unassigned in this Python's Unicode data, version {unicodedata.unidata_version}, are not "
            "allowed: " + _named(unassigned)
        )
    # Marks are counted on the canonical decomposition, where a letter
    # composed in advance carries its own.
    if _overstacked(unicodedata.normalize("NFD", text)):
        errors.append(
            f"more than {_MAX_COMBINING_MARKS} combining marks on one character, or more than "
            f"{_MAX_STACKED_MARKS} above or below it, are not allowed: they stack over the lines around it"
        )
    pieces = split_testimony(text)
    if any(_baseless_mark(piece) for kind, piece in pieces if kind == "text"):
        errors.append(
            "combining marks with no character before them in their text are not allowed: the browser draws them "
            "over the formula or mark before them; write the character already composed, or an accent in a "
            "formula such as \\bar{z}"
        )
    if any(kind == "text" and any(token[0][0] == "$" for token in _DOLLAR.finditer(piece)) for kind, piece in pieces):
        errors.append("a dollar sign that no other closes is not allowed: write \\$ for a dollar sign meant as typed")
    unlisted: dict[str, None] = {}
    for kind, tex in pieces:
        if kind != "text":
            errors.extend(_formula_errors(tex, unlisted))
    if unlisted:
        instead = [_TEX_INSTEAD[name] for name in sorted(unlisted) if name in _TEX_INSTEAD]
        errors.append(
            "TeX outside the read-back allowlist is not allowed: "
            + _named(sorted(unlisted))
            + ("; write " + "; ".join(dict.fromkeys(instead)) if instead else "")
        )
    if not _shows_letter_or_digit(pieces):
        errors.append("testimony shows no letter or digit")
    return tuple(dict.fromkeys(errors))


def _formula_errors(tex: str, unlisted: dict[str, None]) -> list[str]:
    """What one formula holds that is not allowed; commands outside the allowlist go into ``unlisted``."""

    if (length := len(tex.encode("utf-16-le", "surrogatepass")) // 2) > _TEX_MAX_LENGTH:
        return [f"a TeX formula of {length} characters is over the limit of {_TEX_MAX_LENGTH}"]
    errors: list[str] = []
    opened: list[str] = []
    deepest = 0
    for token in _TEX_TOKEN.finditer(tex):
        name = token.group()
        if name in ("{", r"\left"):
            opened.append(name)
        elif name in ("}", r"\right"):
            if not opened or opened.pop() != {"}": "{", r"\right": r"\left"}[name]:
                errors.append(_TEX_UNBALANCED)
        elif name in (r"\begin", r"\end"):
            environment = _TEX_ENVIRONMENT.match(tex, token.end())
            environment_name = environment.group(1) if environment else ""
            if name == r"\begin":
                opened.append(environment_name)
                if environment_name not in _TEX_ENVIRONMENTS:
                    unlisted[f"\\begin{{{environment_name}}}"] = None
                elif _TEX_BRACKET_AFTER.match(tex, environment.end()):
                    errors.append(
                        "a bracket right after TeX \\begin is not allowed: MathJax does not show what it holds; "
                        "write {} before a bracket that starts the first row"
                    )
            elif not opened or opened.pop() != environment_name:
                errors.append(_TEX_UNBALANCED)
        elif name in ("&", "\\\\"):
            if not any(kind in _TEX_ENVIRONMENTS for kind in opened):
                errors.append("TeX & and \\\\ are allowed only between the cells and rows of an environment")
            elif name == "\\\\" and _TEX_ROW_OPTION.match(tex, token.end()):
                errors.append(
                    "TeX row spacing after \\\\ is not allowed: it can draw one row over another; put a space "
                    "between \\\\ and a [ or * that starts the next row"
                )
        elif name == "~":
            errors.append("~ in a formula is not allowed: it is a space; MathJax spaces a formula itself")
        elif name == "%":
            errors.append("TeX comments are not allowed: they drop the rest of their line; write \\% for a percent sign")
        elif name == "$":
            errors.append("a dollar sign inside a formula is not allowed: write \\$ for one")
        elif name not in _TESTIMONY_TEX:
            unlisted[name] = None
        deepest = max(deepest, len(opened))
    if opened:
        errors.append(_TEX_UNBALANCED)
    if deepest > _TEX_MAX_DEPTH:
        errors.append(f"TeX nested more than {_TEX_MAX_DEPTH} deep is not allowed")
    if unset := [character for character in tex if not _settable(character)]:
        errors.append(
            "characters MathJax cannot set in a formula are not allowed: "
            + _named(dict.fromkeys(f"U+{ord(character):04X}" for character in unset))
            + "; write them outside the formula"
        )
    if any(unicodedata.category(character) in {"Mn", "Me"} for character in tex):
        errors.append(
            "combining marks in a formula are not allowed: MathJax sets each as a symbol of its own, over the one "
            "before it; write an accent such as \\acute{x}, or the character already composed"
        )
    return errors


_TEX_UNBALANCED = "unbalanced TeX braces, \\left and \\right, or \\begin and \\end are not allowed"


def _settable(character: str) -> bool:
    """Whether MathJax 3.2.2 sets ``character`` in a formula."""

    code = ord(character)
    return code < 0x80 or any(first <= code <= last for first, last in _TEX_CHARACTER_RANGES)


def _shows_letter_or_digit(pieces: list[tuple[str, str]]) -> bool:
    """Whether the testimony shows a letter or digit. In formulas, TeX
    commands and environment names are not shown as written, so they are set
    aside, except commands that set a letter."""

    for kind, piece in pieces:
        if kind != "text":
            piece = _TEX_ENVIRONMENT_NAME.sub(" ", piece)
            piece = _TEX_CONTROL_SEQUENCE.sub(lambda command: "a" if command.group() in _TEX_LETTERS else " ", piece)
        if any(character.isalnum() for character in piece):
            return True
    return False


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


def _unassigned_characters(text: str) -> list[str]:
    """Name each distinct code point in ``text`` that this Python's Unicode
    data leaves unassigned, other than those :data:`_HIDDEN_CODE_POINTS`
    refuses as invisible whatever their category."""

    found: dict[str, None] = {}
    for character in text:
        if unicodedata.category(character) == "Cn" and ord(character) not in _HIDDEN_CODE_POINTS:
            found[f"U+{ord(character):04X}"] = None
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


# --------------------------------------------------------------------------- #
# The card file
# --------------------------------------------------------------------------- #


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
    packet_fence = _fence(packet_text)
    testimony = testimony.strip("\n")
    testimony_fence = _fence(testimony)
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


def _split(text: str) -> tuple[dict[str, str], str, tuple[str, ...]]:
    """Parse the deliberately small, versioned read-back frontmatter schema."""

    lines = text.split("\n")
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
    and the last is opened without following a link.
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
    try:
        return os.open(reached, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"refusing a symlink or unsafe component in the read-back path: {reached}") from exc
        raise ValueError(f"cannot open read-back directory: {reached}: {exc.strerror}") from exc


def _card_hash_at(path: Path) -> str | None:
    """The hash of the card's bytes, read without following a link; ``None`` if there is no card.

    Only a regular file is read, and at most one byte past the card limit,
    which marks a card over it; opening never blocks, even on a FIFO. The
    bytes are hashed whatever they hold, so a card that is not UTF-8 can
    still be named, and replaced, by its hash.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


def _publish_card(path: Path, content: str, *, expected_card_hash: str | None) -> None:
    """Publish a card under compare-and-swap, while the caller holds its directory's lock.

    No other publication acts between the compare and the replacement.
    Content identical to the card's is not written again. Otherwise the new
    card is staged beside the old one, flushed to disk, and renamed over it
    in one step, so the card's name holds the old complete card until it
    holds the new one. A failure or interruption before the rename removes
    the staging file if it can; a crash, SIGTERM, or SIGKILL can leave it,
    and the loader never reads it as a card.
    """

    data = content.encode("utf-8")
    before = _card_hash_at(path)
    if before == _card_hash(data):
        return
    if conflict := _card_conflict(path, before, expected_card_hash):
        raise ValueError(conflict)
    # Named before it exists, so the cleanup below sees the staging file from
    # the moment it is created. The name is too random to be anyone else's.
    staged: Path | None = path.with_name(f"{_WORK_PREFIX}{secrets.token_hex(12)}.tmp")
    try:
        try:
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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
        os.replace(staged, path)
        staged = None
    except OSError as exc:
        raise ValueError(f"cannot publish read-back: {path}: {exc}") from exc
    finally:
        if staged is not None:
            with contextlib.suppress(OSError):
                os.unlink(staged)


__all__ = [
    "READBACKS_DIR",
    "READBACK_HEADING",
    "READBACK_SCHEMA",
    "TESTIMONY_MAX_BYTES",
    "PreparedReadback",
    "Readback",
    "load_readbacks",
    "prepare_readback",
    "publish_readback",
    "readback_for",
    "readback_keys",
    "readback_path",
    "split_testimony",
]
