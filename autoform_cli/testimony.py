"""Testimony: the text a read-back holds, and the checks it must pass.

Testimony is plain text with TeX formulas between dollar signs. Every reader
shows the text as typed: GitHub and a Markdown viewer of the vault inside the
card's text block, the site escaped. :func:`split_testimony` is the one
reading of where the formulas are, and :func:`formula_problems` says whether
each can be typeset: the site typesets those it accepts, with a TeX input of
the card's own, and shows the others as typed. No formula makes testimony
invalid; :func:`errors_in_testimony` says what does. ``\\$`` is a dollar sign
that opens nothing.

:mod:`autoform_cli.readback` reads and writes the cards testimony is filed in.
The user-facing rules are in the CLI reference's read-back cards section and
in ``skills/human-review/references/readback.md``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

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
#: The TeX a typeset formula may use: the notation statements need, out of
#: what the site's pinned MathJax 3.2.2 defines in the base and ams packages,
#: the only ones a card's formulas are typeset with. Nothing listed spaces,
#: moves, hides, colours, boxes, resizes, labels, or numbers content, and
#: nothing defines a macro. A formula with any other command is shown as typed,
#: so extending this is a deliberate edit, to be checked against that MathJax
#: version: b27a900e found that a list of what to refuse left such commands
#: through.
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
#: The environments that draw a delimiter around their rows, a parenthesis,
#: bracket, bar or brace, which shows even when they hold nothing.
_TEX_FENCED_ENVIRONMENTS = frozenset({"cases", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix"})
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
_TEX_TOKEN = re.compile(r"\\(?:[A-Za-z]+|.)|[{}&~%$^_]", re.DOTALL)
_TEX_ENVIRONMENT = re.compile(r"\s*\{([^{}\\]{0,64})\}")
#: ``\begin{aligned}[t]`` and ``\begin{gathered}[t]`` read a bracket after
#: any space for where the rows sit, and show nothing written there; the other
#: environments show it. A star or bracket right after ``\\`` spaces rows
#: apart, or with a negative dimension draws one over another.
_TEX_POSITIONED = frozenset({"aligned", "gathered"})
_TEX_BRACKET_AFTER = re.compile(r"\s*\[")
_TEX_ROW_OPTION = re.compile(r"[*\[]")
#: The commands whose argument MathJax 3.2.2 shows as typed text.
_TEX_TEXT_COMMANDS = frozenset(r"\text \textrm \textbf \textit \texttt \textsf \textnormal \textup \mbox".split())
_TEX_SPACE_RUN = re.compile(r"[ \t]{2,}")
#: Commands that draw nothing of their own, so a cell holding only them, with
#: nothing to set, shows nothing; and the null delimiter ``.`` they may take.
_TEX_DRAWS_NOTHING = _TEX_TEXT_COMMANDS | frozenset(
    r"""
    \mathbb \Bbb \mathcal \mathfrak \mathscr \mathrm \mathbf \mathsf \mathit \mathtt \operatorname
    \frac \dfrac \tfrac \overset \underset \stackrel \limits \nolimits \left \right \middle \big \Big
    \bigg \Bigg \bigl \bigr \Bigl \Bigr \biggl \biggr \Biggl \Biggr
    """.split()
)
_TEX_NULL_DELIMITER = re.compile(r"\\(?:left|right|middle|[bB]igg?[lr]?)\s*\.")
#: Commands, with how many arguments each takes, that draw nothing when every
#: argument is empty but still take room, so that a run of them is a blank;
#: and the sized delimiters, whose null delimiter ``.`` does the same. Other
#: commands with nothing in them take no room, such as a font, or draw
#: something, such as ``\\sqrt`` or ``\\overline``.
_TEX_SPACED_WHEN_EMPTY = {
    r"\operatorname": 1,
    **dict.fromkeys((r"\frac", r"\dfrac", r"\tfrac", r"\overset", r"\underset", r"\stackrel"), 2),
}
_TEX_SIZED = frozenset(r"\middle \big \Big \bigg \Bigg \bigl \bigr \Bigl \Bigr \biggl \biggr \Biggl \Biggr".split())
_TEX_NULL_AFTER = re.compile(r"\s*\.")
_TEX_EMPTY_PAIR_AFTER = re.compile(r"\s*\.[\s{}]*\\right\s*\.")
_TEX_EMPTY_GROUP = re.compile(r"\s*\{[\s{}]*\}")
_TEX_OPERATORNAME_STAR = re.compile(r"(?<=\\operatorname)\s*\*")
_TEX_ENVIRONMENT_NAME = re.compile(r"\\(?:begin|end)\s*\{([^{}]*)\}")
_TEX_CONTROL_SEQUENCE = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)
#: A dollar sign, or a backslash and the character it escapes, which is never
#: a delimiter: ``\$`` is a dollar sign in a formula and shown as typed in text.
_DOLLAR = re.compile(r"\\.|\$\$?", re.DOTALL)


def split_testimony(text: str) -> list[tuple[str, str]]:
    """``text`` split into ``("text", ...)``, ``("inline", ...)``, and ``("display", ...)`` pieces, in order.

    Dollar signs are taken in turn: ``$$`` opens a displayed formula that the
    next ``$$`` closes, and a single ``$`` a formula in the line that the
    next ``$`` closes. A backslash and the character after it are never a
    delimiter. This is the only reading of where formulas are, and
    :func:`formula_problems` judges each formula piece. A dollar sign no
    other closes is left in the text, which :func:`typesetting_warnings` names.
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


def errors_in_testimony(text: str, *, writing: bool = True) -> tuple[str, ...]:
    """Reject testimony that says more or other than a reader sees, or is too large to check.

    The text is shown as typed, so what a reader could be kept from seeing is
    in its characters: invisible and reordering ones, marks stacked over the
    lines around, and marks with nothing to sit on. It must show at least one
    letter or digit. A formula is never a reason to refuse testimony:
    :func:`formula_problems` decides whether it is typeset or shown as typed.

    Code points this Python's Unicode data leaves unassigned are refused only
    when ``writing`` a card: a later Python may assign them, so a filed card
    loads the same whichever Python reads it.
    """

    size = len(text.encode("utf-8", "surrogatepass"))
    if size > TESTIMONY_MAX_BYTES:
        return (f"testimony is {size} bytes, over the {TESTIMONY_MAX_BYTES}-byte limit",)
    if (lines := text.count("\n") + 1) > TESTIMONY_MAX_LINES:
        return (f"testimony has {lines} lines, over the {TESTIMONY_MAX_LINES}-line limit",)
    errors: list[str] = []
    if hidden := hidden_characters(text):
        errors.append("invisible or reordering characters are not allowed: " + _named(hidden))
    if writing and (unassigned := _unassigned_characters(text)):
        errors.append(
            f"characters unassigned in this Python's Unicode data, version {unicodedata.unidata_version}, are not "
            "allowed: " + _named(unassigned)
        )
    # Marks are counted on the canonical decomposition, where a letter
    # composed in advance carries its own.
    if overstacked(unicodedata.normalize("NFD", text)):
        errors.append(
            f"more than {_MAX_COMBINING_MARKS} combining marks on one character, or more than "
            f"{_MAX_STACKED_MARKS} above or below it, are not allowed: they stack over the lines around it"
        )
    pieces = split_testimony(text)
    if any(baseless_mark(piece) for kind, piece in pieces if kind == "text"):
        errors.append(
            "combining marks with no character before them in their text are not allowed: the browser draws them "
            "over the formula or mark before them; write the character already composed, or an accent in a "
            "formula such as \\bar{z}"
        )
    if not _shows_letter_or_digit(pieces):
        errors.append("testimony shows no letter or digit")
    return tuple(dict.fromkeys(errors))


def typesetting_warnings(text: str) -> tuple[str, ...]:
    """What of ``text`` a site shows as typed instead of typesetting, and why:
    each formula :func:`formula_problems` finds a problem in, and a dollar
    sign no other closes. The testimony is still filed and valid; whoever
    files it can ask for a read-back without these, and the first
    :data:`MAX_NAMED` formulas are named, with a count of the rest."""

    pieces = split_testimony(text)
    shown = [(kind, tex, problems) for kind, tex in pieces if kind != "text" and (problems := formula_problems(tex))]
    warnings = []
    for kind, tex, problems in shown[:MAX_NAMED]:
        delimiter = "$$" if kind == "display" else "$"
        warnings.append(f"the formula {quoted(delimiter + tex + delimiter)} is shown as typed: " + "; ".join(problems))
    if len(shown) > MAX_NAMED:
        warnings.append(f"and {len(shown) - MAX_NAMED} more formulas are shown as typed")
    if any(kind == "text" and any(token[0][0] == "$" for token in _DOLLAR.finditer(piece)) for kind, piece in pieces):
        warnings.append("a dollar sign that no other closes is shown as typed: write \\$ for a dollar sign")
    return tuple(warnings)


def formula_problems(tex: str) -> tuple[str, ...]:
    """Why a site shows the formula ``tex`` as typed instead of typesetting it;
    empty when it typesets it.

    A formula is typeset only when it uses the TeX in :data:`_TESTIMONY_TEX`,
    balanced and nested at most :data:`_TEX_MAX_DEPTH` deep, with characters
    MathJax 3.2.2 can set, and nothing that spaces it out or hides part of
    it: no ``~``, comment, or row spacing; no bracket MathJax reads after
    ``\\begin{aligned}`` or ``\\begin{gathered}``; no run of spaces in
    ``\\text`` and its kin, which keep every space, nor an argument of spaces
    alone; nothing MathJax spaces though it shows nothing, such as an empty
    ``\\frac``, script, or ``\\left.`` and ``\\right.`` pair, or a whole
    formula with nothing in it; and no two empty cells, or two empty rows, one
    after the other. Each check is a linear scan of the formula.
    """

    if (length := len(tex.encode("utf-16-le", "surrogatepass")) // 2) > _TEX_MAX_LENGTH:
        return (f"it is {length} characters long, over the limit of {_TEX_MAX_LENGTH}",)
    problems: list[str] = []
    unlisted: dict[str, None] = {}
    opened: list[str] = []
    # For each open environment: where its current cell starts, how many
    # empty cells and empty rows end right before it, and whether its row
    # has shown nothing yet.
    cells: list[list] = []
    deepest = 0
    text_until = 0
    for token in _TEX_TOKEN.finditer(tex):
        name = token.group()
        if name in ("{", r"\left"):
            opened.append(name)
            if name == r"\left" and _TEX_EMPTY_PAIR_AFTER.match(tex, token.end()):
                problems.append("\\left. and \\right. with nothing between them, which show a blank; leave them out")
        elif name in ("}", r"\right"):
            if not opened or opened.pop() != {"}": "{", r"\right": r"\left"}[name]:
                problems.append(_TEX_UNBALANCED)
        elif name in (r"\begin", r"\end"):
            environment = _TEX_ENVIRONMENT.match(tex, token.end())
            environment_name = environment.group(1) if environment else ""
            if name == r"\begin":
                opened.append(environment_name)
                if environment_name not in _TEX_ENVIRONMENTS:
                    unlisted[f"\\begin{{{environment_name}}}"] = None
                else:
                    cells.append([environment.end(), 0, 0, True])
                    if environment_name in _TEX_POSITIONED and _TEX_BRACKET_AFTER.match(tex, environment.end()):
                        problems.append(
                            f"a bracket right after \\begin{{{environment_name}}}, which MathJax reads and does not "
                            "show; write {} before a bracket that starts the first row"
                        )
            elif not opened or opened.pop() != environment_name:
                problems.append(_TEX_UNBALANCED)
            elif environment_name in _TEX_ENVIRONMENTS and cells:
                _end_cell(cells[-1], tex[cells[-1][0] : token.start()], name, problems)
                cells.pop()
        elif name in ("&", "\\\\"):
            if not cells:
                problems.append("& or \\\\ outside the cells and rows of an environment")
            else:
                _end_cell(cells[-1], tex[cells[-1][0] : token.start()], name, problems)
                cells[-1][0] = token.end()
                if name == "\\\\" and _TEX_ROW_OPTION.match(tex, token.end()):
                    problems.append(
                        "row spacing after \\\\, which can draw one row over another; put a space between \\\\ "
                        "and a [ or * that starts the next row"
                    )
        elif name == "~":
            problems.append("~, a space; MathJax spaces a formula itself")
        elif name == "%":
            problems.append("a TeX comment, which drops the rest of its line; write \\% for a percent sign")
        elif name == "$":
            problems.append("a dollar sign; write \\$ for one")
        elif name in ("^", "_"):
            if _TEX_EMPTY_GROUP.match(tex, token.end()):
                problems.append("an empty superscript or subscript, which shows a blank; leave it out")
        elif name not in _TESTIMONY_TEX:
            unlisted[name] = None
        elif name in _TEX_SIZED:
            if _TEX_NULL_AFTER.match(tex, token.end()):
                problems.append(f"{name}. with nothing to size, which shows a blank; leave it out")
        elif name in _TEX_SPACED_WHEN_EMPTY:
            if _empty_arguments(tex, token.end(), _TEX_SPACED_WHEN_EMPTY[name], star=name == r"\operatorname"):
                problems.append(f"{name} with nothing in it, which shows a blank; leave it out")
        elif name in _TEX_TEXT_COMMANDS and token.start() >= text_until:
            # Text mode shows its argument as typed, commands and all, so a
            # text command inside it starts nothing.
            text_until, blank, _ = _text_argument(tex, token.end())
            if blank:
                problems.append(blank.format(command=name))
        deepest = max(deepest, len(opened))
    if opened:
        problems.append(_TEX_UNBALANCED)
    if deepest > _TEX_MAX_DEPTH:
        problems.append(f"it nests more than {_TEX_MAX_DEPTH} deep")
    if unlisted:
        instead = [_TEX_INSTEAD[name] for name in sorted(unlisted) if name in _TEX_INSTEAD]
        problems.append(
            "TeX outside the read-back allowlist: "
            + _named(sorted(unlisted))
            + ("; write " + "; ".join(dict.fromkeys(instead)) if instead else "")
        )
    if unset := [character for character in tex if not _settable(character)]:
        problems.append(
            "characters MathJax cannot set: "
            + _named(dict.fromkeys(f"U+{ord(character):04X}" for character in unset))
            + "; write them outside the formula"
        )
    if any(unicodedata.category(character) in {"Mn", "Me"} for character in tex):
        problems.append(
            "combining marks, which MathJax sets each as a symbol of its own, over the one before it; write an "
            "accent such as \\acute{x}, or the character already composed"
        )
    # A displayed formula with nothing in it still takes a line.
    if not problems and _shows_nothing(tex):
        problems.append("nothing in it, which shows a blank; leave it out")
    return tuple(dict.fromkeys(problems))


_TEX_UNBALANCED = "unbalanced braces, \\left and \\right, or \\begin and \\end"


def _end_cell(environment: list, cell: str, separator: str, problems: list[str]) -> None:
    """Count the ``cell`` that ``separator`` ends in an open ``environment``:
    an empty cell right after another, or a row of empty cells right after
    another, is a problem. ``\\end`` ends a cell but no row: MathJax drops an
    empty last row."""

    empty = _shows_nothing(cell)
    environment[1] = environment[1] + 1 if empty else 0
    if environment[1] == 2:
        problems.append("two empty cells one after the other, which MathJax spaces apart like full ones")
    environment[3] = environment[3] and empty
    if separator == "\\\\":
        environment[2] = environment[2] + 1 if environment[3] else 0
        if environment[2] == 2:
            problems.append("two empty rows one after the other, which MathJax spaces apart like full ones")
        environment[1], environment[3] = 0, True


def _shows_nothing(cell: str) -> bool:
    """Whether a cell, or a whole formula, shows nothing: it holds only
    spaces, braces, the separators of empty cells and rows, null delimiters
    such as ``\\left.``, environments with nothing in them that draw no
    delimiter of their own, and commands that draw nothing of their own, such
    as a font with nothing to set. A text command shows its argument as
    typed, so one with more than spaces in it shows something."""

    text_until = 0
    for command in _TEX_CONTROL_SEQUENCE.finditer(cell):
        if command.start() >= text_until and command.group() in _TEX_TEXT_COMMANDS:
            text_until, _, shown = _text_argument(cell, command.end())
            if shown.strip(" \t"):
                return False
    cell = _TEX_ENVIRONMENT_NAME.sub(
        lambda name: "x" if name.group(1) in _TEX_FENCED_ENVIRONMENTS else "", _TEX_OPERATORNAME_STAR.sub("", cell)
    )
    cell = _TEX_CONTROL_SEQUENCE.sub(
        lambda command: "" if command.group() in _TEX_DRAWS_NOTHING or command.group() == "\\\\" else "x",
        _TEX_NULL_DELIMITER.sub("", cell),
    )
    # Script marks and separators are left only where they have nothing to set.
    return not cell.strip(" \t\n{}^_&")


def _text_argument(tex: str, index: int) -> tuple[int, str, str]:
    """Where the argument of the text command that ends at ``index`` ends,
    why a site shows it as typed, if it does, and what it shows, if it is
    braced.

    MathJax skips spaces before the argument, shows a braced one as typed, and
    drops its line ends but keeps every space and tab, the first and last as
    no-break spaces. Without braces it takes the one character or command
    after it, which can be one that closes a group or ends an environment.
    """

    while index < len(tex) and tex[index] in " \t\n":
        index += 1
    if index == len(tex) or tex[index] != "{":
        return index, "{command} without braces, which MathJax reads as the one thing after it; write {command}{{...}}", ""
    depth, position = 0, index
    while position < len(tex):
        if tex[position] == "\\":
            position += 2
            continue
        depth += {"{": 1, "}": -1}.get(tex[position], 0)
        if depth == 0:
            break
        position += 1
    shown = tex[index + 1 : position].replace("\n", "")
    if _TEX_SPACE_RUN.search(shown):
        return position + 1, "a run of spaces in {command}, which keeps every space; write one space at a time", shown
    if shown and not shown.strip(" \t"):
        return position + 1, "{command} with only spaces in it, which shows a blank; leave it out", shown
    return position + 1, "", shown


def _empty_arguments(tex: str, index: int, count: int, *, star: bool = False) -> bool:
    """Whether the ``count`` arguments after ``index`` are all braced groups
    with nothing but spaces and braces in them, after a ``*`` when ``star``.
    Only spaces and braces are read, so the scan stops at the first
    character that shows."""

    if star and tex.startswith("*", index):
        index += 1
    for _ in range(count):
        while index < len(tex) and tex[index] in " \t\n":
            index += 1
        if index == len(tex) or tex[index] != "{":
            return False
        depth = 0
        while index < len(tex):
            depth += {"{": 1, "}": -1}.get(tex[index], 0)
            if depth == 0:
                break
            if tex[index] not in " \t\n{}":
                return False
            index += 1
        else:
            return False
        index += 1
    return True


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
MAX_NAMED = 8
_MAX_NAME_LENGTH = 64


def _named(names: Iterable[str]) -> str:
    """``names`` joined by commas: the first :data:`MAX_NAMED`, each shown by
    :func:`quoted`, and a count of the rest."""

    names = list(names)
    rest = len(names) - MAX_NAMED
    return ", ".join(quoted(name) for name in names[:MAX_NAMED]) + (f", and {rest} more" if rest > 0 else "")


def quoted(value: str) -> str:
    """``value``, which a card may hold, as a message quotes it: each character
    a reader could not see as typed escaped, and cut to :data:`_MAX_NAME_LENGTH`."""

    escaped = "".join(
        character if character.isprintable() and not hidden_characters(character) else f"\\u{ord(character):04x}"
        for character in value[: _MAX_NAME_LENGTH * 2]
    )
    return escaped if len(escaped) <= _MAX_NAME_LENGTH else escaped[: _MAX_NAME_LENGTH - 3] + "..."


def hidden_characters(text: str) -> list[str]:
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


def overstacked(text: str) -> bool:
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


def baseless_mark(text: str) -> bool:
    """Whether a combining mark in ``text``, decomposed, starts it or follows
    whitespace, so that it has no character of its own to sit on."""

    previous = " "
    for character in unicodedata.normalize("NFD", text):
        if unicodedata.category(character) not in {"Mn", "Me"}:
            previous = character
        elif previous.isspace():
            return True
    return False


__all__ = [
    "MAX_NAMED",
    "TESTIMONY_MAX_BYTES",
    "TESTIMONY_MAX_LINES",
    "baseless_mark",
    "errors_in_testimony",
    "formula_problems",
    "hidden_characters",
    "overstacked",
    "quoted",
    "split_testimony",
    "typesetting_warnings",
]
