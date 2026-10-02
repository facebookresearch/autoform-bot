from dataclasses import replace
import json
from pathlib import Path
import re

import markdown as markdown_renderer
import pytest

from autoform_cli.readback import (
    _MAX_STACKED_MARKS,
    _TESTIMONY_LANGUAGES,
    _TESTIMONY_TEX,
    _TEX_MAX_SPACING,
    _TEX_MAX_TESTIMONY_SPACING,
    TESTIMONY_MAX_BRACKETS,
    TESTIMONY_MAX_BYTES,
    TESTIMONY_MAX_RENDERED_BYTES,
    Readback,
    _testimony_errors,
    load_readbacks,
    publishable_article,
    render_testimony,
    write_readback,
)
from autoform_cli.render import _mermaid_script, _readback_block, _skeleton_block
from autoform_cli.skeleton import DeclarationSkeleton


def _declaration() -> DeclarationSkeleton:
    statement = "/-- reviewer hint that is absent from the packet -/\ntheorem result : True"
    comment_end = len(statement[: statement.index("-/") + 2].encode("utf-8"))
    return DeclarationSkeleton(
        name="Review.result",
        kind="theorem",
        module="Review",
        path="Review.lean",
        start_line=1,
        end_line=2,
        signature="Review.result : True",
        raw_signature="Review.result : True",
        semantic='{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}}}}',
        lean_version="4.32.2",
        depends=(),
        trusted=(),
        assumed=(),
        assumed_semantics=(),
        boundary_modules=(),
        axioms=(),
        axiom_semantics=(),
        statement=statement,
        statement_comments=((0, comment_end),),
    )


def test_review_displays_the_exact_hashed_blind_packet() -> None:
    declaration = _declaration()

    rendered = "\n".join(_skeleton_block(declaration))

    assert "reviewer hint" not in rendered
    assert "Review.result : True" in rendered
    assert declaration.evidence_hash in rendered


def test_readback_markdown_cannot_inject_raw_html() -> None:
    declaration = _declaration()
    readback = Readback(
        article_id="af_0123456789abcdef01234567",
        declaration=declaration.name,
        skeleton_hash=declaration.hash,
        packet_hash=declaration.evidence_hash,
        model="reviewer",
        text="**Result:** $P$. <script>alert(1)</script>",
        path=Path("card.md"),
        shown_hash=declaration.evidence_hash,
    )

    rendered = "\n".join(_readback_block(declaration, readback))

    assert "**Result:** $P$." in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered

    invalid = "\n".join(
        _readback_block(declaration, replace(readback, validation_errors=("raw HTML is not allowed",)))
    )
    assert "bp-readback-invalid" in invalid
    assert "invalid · raw HTML is not allowed" in invalid


@pytest.mark.parametrize(
    "testimony",
    [
        "<script>alert(1)</script>",
        "[click](javascript:alert(1))",
        "![remote](https://example.test/pixel.png)",
        "<https://example.test/track>",
        "<someone@example.test>",
        "[remote]: https://example.test/track",
        "``` { .lean #claim }\ntheorem t : True\n```",
        '```mermaid\ngraph TD\nclick A "javascript:alert(1)"\n```',
        "``` {.mermaid}\ngraph TD\nA-->B\n```",
        "~~~ MERMAID\ngraph TD\n~~~",
        "- ```mermaid\n  graph TD\n  ```",
        "1. ```mermaid\n   graph TD\n   ```",
        "- ~~~mermaid\n  graph TD\n  ~~~",
        "> ~~~mermaid\n> graph TD\n> ~~~",
        "   ```mermaid\ngraph TD\n```",
        "```mermaid title\ngraph TD\n```",
        r"$\require{html}\href{javascript:alert(1)}{x}$",
        r"$\style{visibility:hidden}{FALSE}$",
        r"$\class{mermaid}{graph TD}$",
        r"$\cssId{hidden}{FALSE}$",
        "$\\re% hidden continuation\nquire{html}$",
        r"$\csname href\endcsname{javascript:alert(1)}{x}$",
    ],
)
def test_writer_rejects_active_testimony(testimony: str, tmp_path: Path) -> None:
    declaration = _declaration()

    with pytest.raises(ValueError, match="unsafe read-back testimony"):
        write_readback(
            tmp_path,
            article_id="af_0123456789abcdef01234567",
            declaration=declaration,
            model="reviewer",
            text=testimony,
            packet_text=declaration.blind_text(),
        )


def test_parser_marks_hand_authored_active_testimony_invalid(tmp_path: Path) -> None:
    declaration = _declaration()
    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text="A plain mathematical statement.",
        packet_text=declaration.blind_text(),
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "A plain mathematical statement.",
            "[run](javascript:alert(1)) ![remote](https://example.test/pixel.png) "
            "<https://example.test/track>",
        ),
        encoding="utf-8",
    )

    card = load_readbacks(tmp_path)[("af_0123456789abcdef01234567", declaration.name)]

    assert not card.valid
    assert "Markdown links, images, and autolinks are not allowed" in card.validation_errors
    rendered = "\n".join(_readback_block(declaration, card))
    published = markdown_renderer.markdown(
        rendered,
        extensions=["attr_list", "md_in_html", "pymdownx.arithmatex", "pymdownx.superfences"],
    )
    assert "bp-readback-invalid" in published
    assert "href=" not in published
    assert "<img" not in published
    assert "<pre><code>" in published


@pytest.mark.parametrize(
    "testimony",
    [
        "*claim*{onclick=alert(1)}",
        "FALSE\n{: hidden=true }",
        "`FALSE`{aria-hidden=true}",
        "FALSE\n{: .bp-visually-hidden}",
        "FALSE\n{: dir=rtl}",
        "FALSE\n{: contenteditable=true}",
        "graph TD\nA-->B\nclick A javascript:alert(1)\n{: .mermaid}",
    ],
)
def test_attribute_lists_are_shown_as_typed(testimony: str, tmp_path: Path) -> None:
    """The testimony renderer reads no attribute lists, so one cannot hide,
    restyle, or activate anything; a reader sees the braces."""

    _file(tmp_path, testimony)
    rendered = render_testimony(testimony)

    assert "{" in rendered
    assert re.findall(r"<[a-z]+\s[^>]*>", rendered) == []
    assert 'querySelectorAll(".mermaid")' in _mermaid_script()


def test_writer_keeps_inert_markdown_and_mathematics(tmp_path: Path) -> None:
    declaration = _declaration()

    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text="**Precisely:** for $x < y$, the claim holds.\n\n- No extra hypothesis.",
        packet_text=declaration.blind_text(),
    )

    assert load_readbacks(tmp_path)[
        ("af_0123456789abcdef01234567", declaration.name)
    ].valid
    assert path.is_file()


def _file(tmp_path: Path, testimony: str) -> Path:
    declaration = _declaration()
    return write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text=testimony,
        packet_text=declaration.blind_text(),
    )


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        ("​", "U+200B ZERO WIDTH SPACE"),
        ("The claim holds⁠ for all x.", "U+2060 WORD JOINER"),
        ("The bound is ‮1 > x‬ for every x.", "U+202E RIGHT-TO-LEFT OVERRIDE"),
        ("The claim holds&#8203; for all x.", "U+200B ZERO WIDTH SPACE"),
        ("The claim holds\U00016fe4 for all x.", "U+16FE4 KHITAN SMALL SCRIPT FILLER"),
        ("P&#x16FE4;Q", "&#x16FE4; (U+16FE4 KHITAN SMALL SCRIPT FILLER)"),
        ("The claim\ufff9 holds.", "U+FFF9 INTERLINEAR ANNOTATION ANCHOR"),
        ("For all $x" + "\u2003" * 2000 + "y$, P.", "U+2003 EM SPACE"),
        ("P" + "\u00a0" * 400 + "and Q.", "U+00A0 NO-BREAK SPACE"),
        ("The bound is \u05d0 > x.", "U+05D0 HEBREW LETTER ALEF"),
        ("The bound is \u0627 > x.", "U+0627 ARABIC LETTER ALEF"),
        ("The sum \u0661 + \u0662 is three.", "U+0661 ARABIC-INDIC DIGIT ONE"),
        ("The claim holds for x" + "\u0301" * 5 + ".", "more than 4 combining marks on one character"),
        ("The claim holds for x" + "\u20dd" * 5 + ".", "more than 4 combining marks on one character"),
        (r"For all $x$, $P(x) \phantom{\land Q(x)}$ holds.", r"\phantom"),
        (r"$P \rlap{\,\land Q}$", r"\rlap"),
        (r"$P \kern-2em \land Q$", r"\kern"),
        (r"$\toggle{P}{P \land Q}\endtoggle$", r"\toggle"),
        (r"$\bbox[black]{P}$", r"\bbox"),
        (r"$\unicode{x200B}$", r"\unicode"),
        (r"$\newcommand{\h}[1]{} P \h{\land Q}$", r"\newcommand"),
        (r"$\def\h#1{} P \h{\land Q}$", r"\def"),
        ("$P(x) % \\land Q(x)\nR$", "TeX comments are not allowed"),
        (r"$P\!\!\!\!\!\!Q$", "repeated negative TeX spacing"),
        ("$ $", "renders no visible text"),
    ],
)
def test_writer_rejects_testimony_that_hides_what_it_says(testimony: str, reason: str, tmp_path: Path) -> None:
    """A reader must be shown everything the card says, and only that."""

    with pytest.raises(ValueError, match="unsafe read-back testimony") as refused:
        _file(tmp_path, testimony)

    assert reason in str(refused.value)


@pytest.mark.parametrize(
    "testimony",
    [
        r"For every $x \in [0, 1]$ and the set $\{x\}$, the claim holds.",
        r"$\alpha \ne \beta$",
        r"Integrate with a thin negative space, $\int\! f$, once.",
        r"At least $50\%$ of cases, or 50\% in prose.",
        "Code such as `a % b` is shown as written.",
        r"For $0<x<1$, $\frac12 < \sqrt[3]{x}$ and $\lfloor x \rfloor = 0$ in $\mathbb R$.",
        r"Here $\operatorname*{arg\,max}_x f(x)$ and $\langle u, v \rangle \le \|u\|\,\|v\|$ hold.",
        r"$$f(x) = \begin{cases} x^2 & \text{if } x \ge 0, \\ -x & \text{otherwise} \end{cases}$$",
        r"$$A = \begin{pmatrix} a & b \\ c & d \end{pmatrix}, \quad \begin{aligned} x &= y \\ &\le z \end{aligned}$$",
        r"For $a < b > c$ and the group $\langle g \rangle$, AT&T and R & D; the claim holds.",
    ],
)
def test_writer_accepts_ordinary_mathematical_testimony(testimony: str, tmp_path: Path) -> None:
    _file(tmp_path, testimony)

    assert all(card.valid for card in load_readbacks(tmp_path).values())


@pytest.mark.parametrize(
    "testimony",
    [
        "The claim holds for x" + "\u0301\u0323" * 2 + ".",
        "Vi\u1ec7t, or Vie\u0323\u0302t, names the same place.",
        "The town Ba\u0302\u0301c and the word \u03b1\u0314\u0301\u0345 carry two marks above.",
        "The Tibetan stack \u0f66\u0f92\u0fb2\u0f72\u0f7e carries four marks.",
        "The cardinal \u2135 and $\\aleph_0$ are left to right.",
    ],
)
def test_combining_marks_up_to_four_and_left_to_right_letters_are_accepted(testimony: str) -> None:
    """Up to four marks on a letter, two of them above it and two below, as
    Vietnamese, polytonic Greek, and Tibetan write them."""

    assert _testimony_errors(testimony) == ()


_STACKED = "more than 4 combining marks on one character, or more than 2 above or below it, are not allowed"


@pytest.mark.parametrize(
    "testimony",
    [
        "The claim holds for x" + "\u0301" * 3 + ".",
        "The value x" + "\u1dd8" * 3 + ".",
        "The value x" + "\u0323" * 3 + ".",
        "The value x" + "\U0001e8d1" * 3 + ".",
        "The value *a\u0301\u0323*_\u0301\u0323_*\u0301\u0323*",
        "The value a\u0301\u0323*\u0301\u0323*\u0301\u0323",
    ],
)
def test_combining_marks_stacked_over_the_lines_around_are_refused(testimony: str) -> None:
    """Each mark above or below a letter stacks on the one before, three of
    them reach over the line above or below; and the marks after an inline
    element's end fall on the letter before it, so they are counted in the
    rendered text as well as in the source."""

    assert any(error.startswith(_STACKED) for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    ("testimony", "command"),
    [
        (r"$P \Rule{3em}{1em}{0em} Q$", r"\Rule"),
        (r"$P \rule{3em}{1em} Q$", r"\rule"),
        (r"$P \Space{3em}{1em}{1em} Q$", r"\Space"),
        (r"$\mathchoice{P}{P \land Q}{P}{P}$", r"\mathchoice"),
        (r"$\toggle{P}{P \land Q}\endtoggle$", r"\endtoggle, \toggle"),
        (r"$\raisebox{2em}{Q}$", r"\raisebox"),
        (r"$P \hbox{and Q}$", r"\hbox"),
        (r"$\unicode{x200B} P$", r"\unicode"),
        (r"$\enclose{box}{Q}$", r"\enclose"),
        (r"$$P \tag{Q}$$", r"\tag"),
        (r"$\color{white}{Q}$", r"\color"),
        (r"$P \cancel{\land Q}$", r"\cancel"),
        (r"$P \Tiny{\land Q}$", r"\Tiny"),
        (r"$P {\tiny \land Q}$", r"\tiny"),
        (r"$\vphantom{Q} P$", r"\vphantom"),
        (r"$P \hphantom{\land Q}$", r"\hphantom"),
        (r"$P\phantom{\land Q}$", r"\phantom"),
        (r"Height is kept with $\mathstrut x$.", r"\mathstrut"),
        (r"$P \negthickspace\negthickspace Q$", r"\negthickspace"),
        (r"$\begin{multline} P \end{multline}$", r"\begin{multline}, \end{multline}"),
    ],
)
def test_writer_refuses_tex_outside_the_allowlist_by_name(testimony: str, command: str, tmp_path: Path) -> None:
    """Only the notation statements need is allowed; anything else is named."""

    with pytest.raises(ValueError, match="unsafe read-back testimony") as refused:
        _file(tmp_path, testimony)

    assert "TeX outside the read-back allowlist is not allowed: " + command in str(refused.value)


@pytest.mark.parametrize(
    "testimony",
    [
        r"$a \leqq b \lessgtr c \preccurlyeq d \twoheadleftarrow e \upharpoonleft f \nleqslant g$",
        r"$\varGamma \varOmega \circledS \clubsuit \bigcirc \dotsi \And \smallint \Arrowvert \divsymbol x$",
        r"$\iiiint_D f$, $\intop\limits_a^b f$, $\injlim_i A_i$, $\varliminf_n x_n$, and $\idotsint$",
        r"$\overleftrightarrow{AB}$, $\underleftarrow{x}$, $\Bbb{R}$, $\textnormal{a b}$, and $\textup{c}$",
        r"${\rm d}x$, ${\cal F}$, ${\bf v}$, ${\it x}$, ${\sf S}$, and ${\tt t}$",
        r"$\begin{smallmatrix} a & b \\ c & d \end{smallmatrix}$",
        r"$\begin{array}{lc} a & b \\ c & d \end{array}$ and $\begin{array}[t]{ r } x \end{array}$",
        r"$\begin{split} a &= b \\ &= c \end{split}$",
        r"$\begin{align} a &= b \end{align}$ and $\begin{gather*} c \end{gather*}$",
        r"$\begin{aligned} \begin{align*} a \end{align*} \end{aligned}$",
        r"$\begin{equation} e^{i\pi} + 1 = 0 \end{equation}$ and $\frac{\begin{equation*} a \\ b \end{equation*}}{c}$",
        r"$\mathopen{]} 0, 1 \mathclose{[}$, $a \equiv b \pod{n}$, and $\mbox{if } x > 0$",
        r"$\left[\begin{array}{cc|c} 1 & 0 & 2 \\ \hline 0 & 1 & 3 \end{array}\right]$",
        r"$\begin{array}{ c : c } a & b \\ \hline c & d \\ \hline e & f \end{array}$",
    ],
)
def test_tex_mathjax_sets_and_the_model_follows_is_allowlisted(testimony: str) -> None:
    """Relations, symbols, operators, fonts, and environments MathJax 3.2.2
    sets with base and ams, and whose layout the model reads."""

    assert _testimony_errors(testimony) == ()


_HLINE = r"TeX \hline is allowed only right after \\ in an array, once, before a row that shows something"


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        (
            r"$\begin{align} a \end{align} \begin{gather} b \end{gather}$",
            "more than one TeX align, gather, or equation environment in a formula is not allowed",
        ),
        (
            r"$\begin{align} a \begin{align*} b \end{align*} \end{align}$",
            "more than one TeX align, gather, or equation environment in a formula is not allowed",
        ),
        (
            r"$\begin{equation} a \end{equation} \begin{equation*} b \end{equation*}$",
            "more than one TeX align, gather, or equation environment in a formula is not allowed",
        ),
        (r"$\begin{equation} a & b \end{equation}$", r"TeX & and \\ are allowed only between the cells and rows"),
        (r"$\begin{array}{|c|} a \end{array}$", r"TeX \begin{array} is allowed only with its columns as l, c, and r"),
        (r"$\begin{array} a \end{array}$", r"TeX \begin{array} is allowed only with its columns as l, c, and r"),
        (r"$\begin{array}{c@{x}c} a & b \end{array}$", r"TeX \begin{array} is allowed only with its columns"),
        (r"$\begin{array}{c||c} a & b \end{array}$", r"TeX \begin{array} is allowed only with its columns"),
        (r"$\begin{array}{c|:c} a & b \end{array}$", r"TeX \begin{array} is allowed only with its columns"),
        (r"$\begin{array}{|cc} a & b \end{array}$", r"TeX \begin{array} is allowed only with its columns"),
        (r"$\begin{array}{c} \hline a \end{array}$", _HLINE),
        (r"$\begin{array}{c} a \\ \hline \end{array}$", _HLINE),
        (r"$\begin{array}{c} a \\ \hline \\ b \end{array}$", _HLINE),
        (r"$\begin{array}{c} a \\ \hline \hline b \end{array}$", _HLINE),
        (r"$\begin{array}{cc} a & \hline b \end{array}$", _HLINE),
        (r"$\begin{array}{c} a \hline \end{array}$", _HLINE),
        (r"$\begin{pmatrix} a \\ \hline b \end{pmatrix}$", _HLINE),
        (r"$\begin{array}[x]{c} a \end{array}$", r"a bracket after TeX \begin{aligned}"),
    ],
)
def test_tex_environments_mathjax_sets_differently_are_refused(testimony: str, reason: str) -> None:
    r"""A second ``align``, ``gather``, or ``equation`` gets an error in
    place of the formula, as does ``&`` in ``equation``; ``array`` takes its
    first argument as its columns, drawing ``|`` as a rule, one rule for
    ``||``, and dropping what else it does not know. A rule at the edge of
    the cells, from ``|`` or ``\hline``, reads as a bar, an overline, or an
    underline."""

    assert any(reason in error for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    ("testimony", "replacement", "rewritten"),
    [
        (r"${n \choose k}$", r"write \binom{n}{k} for {n \choose k}", r"$\binom{n}{k}$"),
        (r"${a \over b}$", r"write \frac{a}{b} for {a \over b}", r"$\frac{a}{b}$"),
        (r"$\cfrac{1}{1 + \cfrac{1}{2}}$", r"write \dfrac for \cfrac", r"$\dfrac{1}{1 + \dfrac{1}{2}}$"),
        (r"$\genfrac{(}{)}{0pt}{}{n}{k}$", r"write \frac or \binom for \genfrac", r"$\binom{n}{k}$ or $\frac{n}{k}$"),
        (r"$a \hspace{1em} b$", r"write \quad or \, for \hspace", r"$a \quad b$ or $a \, b$"),
        (r"$\boxed{x = 1}$", r"write the boxed formula alone, without \boxed", r"$x = 1$"),
    ],
)
def test_tex_commands_outside_the_allowlist_are_refused_with_a_replacement(
    testimony: str, replacement: str, rewritten: str
) -> None:
    """MathJax 3.2.2 sets each of these, and the message names the command
    to write in its place, which is accepted."""

    assert any(replacement in error for error in _testimony_errors(testimony))
    assert _testimony_errors(rewritten) == ()


@pytest.mark.parametrize(
    ("testimony", "replacement", "rewritten"),
    [
        (
            r"$\begin{multline} a + b \\ = c \end{multline}$",
            r"write \begin{gathered} for \begin{multline}",
            r"$\begin{gathered} a + b \\ = c \end{gathered}$",
        ),
        (
            r"$\begin{alignat}{2} a &= b &\quad c &= d \end{alignat}$",
            r"write \begin{aligned}, without the column count, for \begin{alignat}",
            r"$\begin{aligned} a &= b &\quad c &= d \end{aligned}$",
        ),
        (
            r"$\begin{alignedat}{1} a &= b \end{alignedat}$",
            r"write \begin{aligned}, without the column count, for \begin{alignedat}",
            r"$\begin{aligned} a &= b \end{aligned}$",
        ),
        (
            r"$\begin{flalign} a &= b \end{flalign}$",
            "write one of the environments allowed: equation, align, gather, aligned, gathered, split, cases",
            r"$\begin{align} a &= b \end{align}$",
        ),
        (
            r"$\begin{align} \begin{equation} a = b \end{equation} \end{align}$",
            "leave out the equation environment",
            r"$\begin{align} a = b \end{align}$",
        ),
        (
            r"$\begin{gather} \begin{gather*} a \\ b \end{gather*} \end{gather}$",
            "write aligned or gathered",
            r"$\begin{gather} \begin{gathered} a \\ b \end{gathered} \end{gather}$",
        ),
    ],
)
def test_tex_environments_outside_what_is_allowed_are_refused_with_a_replacement(
    testimony: str, replacement: str, rewritten: str
) -> None:
    """MathJax 3.2.2 sets each of these, and the message names what to
    write in its place, which is accepted."""

    assert any(replacement in error for error in _testimony_errors(testimony))
    assert _testimony_errors(rewritten) == ()


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        (r"$\overset{}{P}$", r"TeX arguments that show nothing are not allowed: \overset"),
        (r"$\operatorname{} P$", r"TeX arguments that show nothing are not allowed: \operatorname"),
        (r"$\mathrm{} P$", r"TeX arguments that show nothing are not allowed: \mathrm"),
        (r"$P \text{ } Q$", r"TeX arguments that show nothing are not allowed: \text"),
        (r"$\mathbb{\,} P$", r"TeX arguments that show nothing are not allowed: \mathbb"),
        (r"$\frac{}{2} P$", r"TeX arguments that show nothing are not allowed: \frac"),
        (r"$P\!{}\!Q$", "repeated negative TeX spacing"),
        ("$P" + r"\qquad" * 4 + r"\, Q$", "TeX spacing over 8 em in one formula is not allowed"),
        (r"$\begin{aligned} P \\[-2em] Q \end{aligned}$", "TeX row spacing after"),
    ],
)
def test_writer_refuses_tex_that_shows_nothing_or_overlaps(testimony: str, reason: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe read-back testimony") as refused:
        _file(tmp_path, testimony)

    assert reason in str(refused.value)


_ROW_STAR = "a * right after TeX \\\\ is not allowed: MathJax reads it as part of the row break and does not show it; "
_ROW_SPACING = "TeX row spacing after \\\\ is not allowed: it can draw rows over one another; remove the bracket after "


@pytest.mark.parametrize(
    ("testimony", "reason", "rewritten"),
    [
        (r"$\begin{pmatrix} 1 & 0 \\* & 1 \end{pmatrix}$", _ROW_STAR, r"$\begin{pmatrix} 1 & 0 \\ * & 1 \end{pmatrix}$"),
        (r"$\begin{aligned} x &= 2 \\* 3 \end{aligned}$", _ROW_STAR, r"$\begin{aligned} x &= 2 \\ * 3 \end{aligned}$"),
        (
            r"$\begin{cases} 1 & x > 0 \\[4pt] 0 & \text{otherwise} \end{cases}$",
            _ROW_SPACING,
            r"$\begin{cases} 1 & x > 0 \\ 0 & \text{otherwise} \end{cases}$",
        ),
        (r"$\begin{aligned} a \\[0, 1] \end{aligned}$", _ROW_SPACING, r"$\begin{aligned} a \\ {}[0, 1] \end{aligned}$"),
    ],
)
def test_what_tex_reads_right_after_a_row_break_is_refused_with_a_rewrite(
    testimony: str, reason: str, rewritten: str
) -> None:
    r"""MathJax reads a star, then a bracket, right after ``\\`` as part of
    the row break and shows neither; each message names a rewrite, which is
    accepted."""

    assert any(error.startswith(reason) for error in _testimony_errors(testimony))
    assert _testimony_errors(rewritten) == ()


def _matrix(rows: int, columns: int) -> str:
    """A matrix of ``a`` with ``rows`` row breaks and ``columns`` ``&`` in each row."""

    return r"\begin{matrix} " + r" \\ ".join(" & ".join(["a"] * (columns + 1)) for _ in range(rows + 1)) + r" \end{matrix}"


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        (r"$\mathrm{\displaystyle} P$", r"TeX arguments that show nothing are not allowed: \mathrm"),
        (r"$\hat{\left.\right.} P$", r"TeX arguments that show nothing are not allowed: \hat"),
        (r"$\mathbb{^{}} P$", r"TeX arguments that show nothing are not allowed: \mathbb"),
        (r"$\mathrm{{{}}} P$", r"TeX arguments that show nothing are not allowed: \mathrm"),
        (r"$\frac\, b$", r"TeX arguments that show nothing are not allowed: \frac"),
        (r"$\operatorname*{} x$", r"TeX arguments that show nothing are not allowed: \operatorname"),
        (r"$P\!\displaystyle\!Q$", "repeated negative TeX spacing"),
        (r"$P\!\left.\right.\!Q$", "repeated negative TeX spacing"),
        (r"$P\!^{}\!Q$", "repeated negative TeX spacing"),
        (r"${P\!}\!Q$", "repeated negative TeX spacing"),
        ("$P" + r"\qquad{}" * 5 + "Q$", "TeX spacing over 8 em in one formula is not allowed"),
        ("$P" + r"\qquad\displaystyle" * 5 + "Q$", "TeX spacing over 8 em in one formula is not allowed"),
        ("$" + _matrix(0, 400) + "$", "more than 9 TeX & in one row are not allowed"),
        ("$" + _matrix(300, 0) + "$", r"more than 16 TeX \\ in one environment are not allowed"),
        ("$P" + r" \\" * 300 + " Q$", r"more than 32 TeX \\ in one formula are not allowed"),
    ],
)
def test_tex_that_sets_nothing_hides_nothing_and_ends_no_spacing(testimony: str, reason: str) -> None:
    """Styles, empty delimiters, and empty groups and scripts set nothing: an
    argument made of them shows nothing, and space on either side of them
    adds up. Space, columns, and rows that push the rest out of view are
    bounded per formula."""

    assert any(reason in error for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        ("$P" + r"\qquad" * 4 + " Q$", None),
        ("$P" + r"\qquad" * 4 + r"\, Q$", "TeX spacing over 8 em in one formula is not allowed"),
        ("$a" + "~" * 32 + "b$", None),
        ("$a" + "~" * 33 + "b$", "TeX spacing over 8 em in one formula is not allowed"),
        (r"$\text{a" + " " * 33 + "b}$", None),
        (r"$\text{a" + " " * 34 + "b}$", "TeX spacing over 8 em in one formula is not allowed"),
        ("$" + r"\text{ a}" * 32 + "$", None),
        ("$" + r"\text{ a}" * 33 + "$", "TeX spacing over 8 em in one formula is not allowed"),
        ("$" + r"\text{a }" * 32 + "$", None),
        ("$" + r"\text{a }" * 33 + "$", "TeX spacing over 8 em in one formula is not allowed"),
        ("$" + r"a\!" * 20 + "b" + r"\qquad" * 4 + " c$", None),
        ("$" + r"a\!" * 20 + "b" + r"\qquad" * 4 + r"\, c$", "TeX spacing over 8 em in one formula is not allowed"),
        (r"$P\!Q$ and $P\!\!\,Q$", None),
        (r"$P\!\!Q$", "repeated negative TeX spacing"),
        (r"$P\;\!\!\!Q$", "repeated negative TeX spacing"),
        ("$" + _matrix(0, 9) + "$", None),
        ("$" + _matrix(0, 10) + "$", "more than 9 TeX & in one row are not allowed"),
        ("$" + _matrix(16, 0) + "$", None),
        ("$" + _matrix(17, 0) + "$", r"more than 16 TeX \\ in one environment are not allowed"),
        ("$" + _matrix(16, 0) + _matrix(15, 0) + r" \\ a$", None),
        ("$" + _matrix(16, 0) + _matrix(16, 0) + r" \\ a$", r"more than 32 TeX \\ in one formula are not allowed"),
        (r"$\begin{aligned} " + r" \\ ".join(["& a"] * 16) + r" \end{aligned}$", None),
        (r"$\begin{aligned} " + r" \\ ".join(["& a"] * 17) + r" \end{aligned}$", "more than 16 empty TeX cells"),
        (r"$\begin{matrix} a \\ b \\ \end{matrix}$", None),
        (r"$\begin{matrix} a \\ \\ b \end{matrix}$", "empty TeX rows are not allowed"),
        ("$" + "{" * 16 + "a" + "}" * 16 + "$", None),
        ("$" + "{" * 17 + "a" + "}" * 17 + "$", "TeX nested more than 16 deep is not allowed"),
        ("$" + "{" * 300 + "a" + "}" * 300 + "$", "TeX nested more than 16 deep is not allowed"),
        ("$" + "x^{" * 8 + "x" + "}" * 8 + "$", None),
        ("$" + "x^{" * 9 + "x" + "}" * 9 + "$", "TeX scripts nested more than 8 deep are not allowed"),
        ("$" + "x^{" * 300 + "x" + "}" * 300 + "$", "TeX scripts nested more than 8 deep are not allowed"),
        ("$P" + r"\qquad" * 4 + " Q$ and $P" + r"\qquad" * 4 + " Q$", None),
        (r"$P\!\,$ and $\,\!Q$", None),
        (r"$\frac{a}$ $b$", r"TeX commands missing an argument are not allowed: \frac"),
    ],
)
def test_tex_limits_hold_at_their_exact_values_and_per_formula(testimony: str, reason: str | None) -> None:
    """Every limit admits its value and refuses one more, and none carries
    from one formula to the next. Spacing counts ``~`` and the spaces of
    ``\\text`` as well as commands, and negative space takes none of it
    back. Nesting is capped far below the depth at
    which MathJax overflows its stack, about two hundred."""

    errors = _testimony_errors(testimony)

    assert errors == () if reason is None else any(reason in error for error in errors)


@pytest.mark.parametrize(
    ("testimony", "rewritten"),
    [
        (r"Here $1\!$$\!1$ holds.", r"Here $1\!1$ holds."),
        (r"Take $\!x$ here.", r"Take $y\!x$ here."),
        (r"Take ${}\!\sum_i x$ here.", r"Take $\int\!\sum_i x$ here."),
        (r"Take $x^2\!$ here.", r"Take $x^2\!y$ here."),
    ],
)
def test_negative_tex_spacing_at_either_end_of_a_formula_is_refused(testimony: str, rewritten: str) -> None:
    """Negative space at the edge of a formula slides it over what sits
    beside it, which may be the negative space of the next formula; between
    two symbols of one formula it is counted."""

    assert (
        "negative TeX spacing at the start or end of a formula is not allowed: it slides the formula over what sits "
        "beside it; write it between two symbols of one formula"
    ) in _testimony_errors(testimony)
    assert _testimony_errors(rewritten) == ()


@pytest.mark.parametrize(
    ("command", "width"),
    [
        (r"\pmod{n}", 24),
        (r"\mod n", 24),
        (r"\pod{n}", 18),
        (r"\bmod n", 10),
        (r"\iff b", 10),
        (r"\implies b", 10),
        (r"\impliedby b", 10),
    ],
)
def test_tex_macros_that_add_space_count_it_toward_the_formula(command: str, width: int) -> None:
    r"""MathJax 3.2.2 kerns 18 mu before ``mod`` in a displayed formula and
    6 after it, 18 mu before the parenthesis of ``\pod``, and puts ``\;`` on
    either side of the arrows; with that space a formula stays within 8 em,
    and one more thin space is over."""

    space = r"\qquad" * 3 + r"\," * ((144 - 108 - width) // 3)

    assert _testimony_errors(f"$a{space}{command}$") == ()
    assert any(
        "TeX spacing over 8 em in one formula is not allowed" in error
        for error in _testimony_errors(f"$a{space}\\,{command}$")
    )


_PROOF_ROWS = r"\begin{aligned} " + r" \\ ".join([r"a &= b \quad \text{by } h_i"] * 7) + r" \end{aligned}"


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        ("$" + _PROOF_ROWS + "$", None),
        (r"$\begin{matrix} a\qquad\qquad & b\qquad\qquad & c \end{matrix}$", None),
        (r"$\begin{matrix} a\qquad\qquad & b\qquad\qquad\, & c \end{matrix}$", "TeX spacing over 8 em in one formula"),
        (
            r"$\begin{matrix} a\qquad\qquad\qquad & b \\ c & d\qquad\qquad \end{matrix}$",
            "TeX spacing over 8 em in one formula",
        ),
        (
            r"$\begin{matrix} \begin{matrix} a\qquad\qquad \\ b \end{matrix} \qquad\qquad\, & c \end{matrix}$",
            "TeX spacing over 8 em in one formula",
        ),
        (" ".join(["$P" + r"\qquad" * 4 + " Q$"] * 8), None),
        (" ".join(["$P" + r"\qquad" * 4 + " Q$"] * 8 + [r"$P\, Q$"]), "TeX spacing over 64 em in one testimony"),
        (" ".join(["$P" + r"\qquad" + " Q$"] * 33), "TeX spacing over 64 em in one testimony is not allowed"),
    ],
)
def test_tex_spacing_counts_once_per_column_and_adds_up_across_formulas(testimony: str, reason: str | None) -> None:
    """Rows stack, so a column spends as much space as its widest cell, and
    the columns of an environment add up. Formulas split a testimony's space
    but not its budget."""

    errors = _testimony_errors(testimony)

    assert errors == () if reason is None else any(reason in error for error in errors)


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        ("${a$", "unbalanced TeX braces are not allowed"),
        ("$a}$", "unbalanced TeX braces are not allowed"),
        (r"$\left( x$", r"unbalanced \left and \right are not allowed"),
        (r"$x \right)$", r"unbalanced \left and \right are not allowed"),
        (r"$a \middle| b$", r"\middle is allowed only between \left and \right"),
        (r"$x\limits_a$", r"\limits and \nolimits are allowed only after a large or named operator"),
        (r"$\sum'\limits_a$", r"\limits and \nolimits are allowed only after a large or named operator"),
        (r"$\sum{\limits} x$", r"\limits and \nolimits are allowed only after a large or named operator"),
        (r"$\lim{\nolimits} a_n$", r"\limits and \nolimits are allowed only after a large or named operator"),
        (r"$\sin{\nolimits} x$", r"\limits and \nolimits are allowed only after a large or named operator"),
        (r"$\sum{\limits}_{n} a_n$", r"\limits and \nolimits are allowed only after a large or named operator"),
        (r"$\sum^{\limits}x$", r"\limits and \nolimits are allowed only after a large or named operator"),
        (r"$a\mod$", r"TeX commands missing an argument are not allowed: \mod"),
        (r"$\frac{a}$", r"TeX commands missing an argument are not allowed: \frac"),
        (r"$\overset{a}$", r"TeX commands missing an argument are not allowed: \overset"),
        (r"$\sqrt{&}$", r"TeX & and \\ are allowed only between the cells and rows of an environment"),
        (r"$a & b$", r"TeX & and \\ are allowed only between the cells and rows of an environment"),
        (r"$\not 0$", r"\not is allowed only before a relation"),
        (r"$\not\forall x$", r"\not is allowed only before a relation such as =, \in, or \le, or before \exists"),
        (r"$P \not\implies Q$", r"\not is allowed only before a relation"),
        (r"$x^a^b$", "a second TeX superscript or subscript on one symbol is not allowed"),
        (r"$x^a'$", "a second TeX superscript or subscript on one symbol is not allowed"),
        (r"$\begin{matrix} a$", r"TeX \begin and \end that do not match are not allowed"),
        (r"$\begin{matrix} a \end{pmatrix}$", r"TeX \begin and \end that do not match are not allowed"),
        (r"$\left x \right)$", r"TeX delimiters MathJax does not accept are not allowed after: \left"),
        (r"$\text{\alpha}$", r"TeX commands and formulas inside \text are not allowed"),
    ],
)
def test_tex_mathjax_would_not_set_is_refused(testimony: str, reason: str) -> None:
    r"""MathJax 3.2.2 shows an error in place of each of these formulas,
    except ``\not 0``, which it sets as a struck-out zero that reads as a
    different symbol, ``\not\implies``, which strikes through the space
    before the arrow, and ``\text{\alpha}``, which it shows as typed."""

    assert any(reason in error for error in _testimony_errors(testimony))


_TEXT_FORMULA = "close \\text before a formula, as in \\text{if } x > 0"


@pytest.mark.parametrize(
    ("testimony", "rewritten"),
    [
        (
            "Restated:\n\n$$\nf(x) = \\begin{cases} 1 & \\text{if $x \\in \\mathbb{Q}$} \\\\ 0 & \\text{otherwise} \\end{cases}\n$$",
            "Restated:\n\n$$\nf(x) = \\begin{cases} 1 & \\text{if } x \\in \\mathbb{Q} \\\\ 0 & \\text{otherwise} \\end{cases}\n$$",
        ),
        (r"\(\text{for all $x$}\)", r"\(\text{for all } x\)"),
        (r"$\text{if \alpha > 0}$", r"$\text{if } x > 0$ and $\text{if } \alpha > 0$"),
    ],
)
def test_tex_inside_text_is_refused_with_a_rewrite(testimony: str, rewritten: str) -> None:
    r"""MathJax 3.2.2 shows a command inside ``\text`` as typed. It does set
    a formula there, but the message points to the one way that reads alike
    wherever the formula is shown: close ``\text`` first."""

    assert any(_TEXT_FORMULA in error for error in _testimony_errors(testimony))
    assert _testimony_errors(rewritten) == ()


@pytest.mark.parametrize(
    "testimony",
    [
        r"$\left( a \middle| b \right)$ and $\sum\limits_{i} a_i$",
        r"$a \mod n$, $a \not= b \not\in c$, and $x'^a + x_a'$",
        r"$\not\exists x, P(x)$ and $\not \exists_y Q(y)$",
        r"$\mathrm{{{x}}}$ and $\text{a \$ b}$",
        r"$a \mathrel{R} b \mathbin{\star} c$ and $P {\scriptscriptstyle \land Q}$",
        r"$\lvert x \rvert$, $\varinjlim_i$, $\textsf{x}$, $\Bbbk$, $\nleftarrow$, and $\circledast$",
        r"$\iint_D f$",
    ],
)
def test_tex_mathjax_sets_cleanly_is_accepted(testimony: str) -> None:
    assert _testimony_errors(testimony) == ()


_BRACED = "TeX superscripts and subscripts that are one of these alone are not allowed: "
_UNSET = "characters MathJax cannot set in a formula are not allowed: "


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        (r"$\begin{aligned}[\text{the claim is false}] x &= x \end{aligned}$", r"a bracket after TeX \begin{aligned}"),
        (r"$\begin{gathered}[P] x \end{gathered}$", r"a bracket after TeX \begin{aligned}"),
        (r"$\begin{aligned}[t x &= x \end{aligned}$", r"missing an argument are not allowed: \begin{aligned}"),
        (r"$x^\sin y$", _BRACED + r"\sin"),
        (r"$x_\dots$", _BRACED + r"\dots"),
        (r"$\lim_\mathop{x} y$", _BRACED + r"\mathop"),
        (r"$P^\iff Q$", _BRACED + r"\iff"),
        (r"$x^\pmb{a}$", _BRACED + r"\pmb"),
        (r"$a^\pmod{n}$", _BRACED + r"\pmod"),
        (r"$a^\pod{n}$", _BRACED + r"\pod"),
        ("$x^’$", "TeX commands missing an argument are not allowed: ^"),
        ("$f’^a'$", "a second TeX superscript or subscript on one symbol is not allowed"),
        (r"$\begin{aligned} \sum & \limits_i a \end{aligned}$", r"\limits and \nolimits are allowed only after"),
        (r"$\sum \\ \limits_i a$", r"\limits and \nolimits are allowed only after"),
        (r"$\mathrm{€} x$", _UNSET + "U+20AC EURO SIGN"),
        ("$\\operatorname{ɑ}$", _UNSET + "U+0251 LATIN SMALL LETTER ALPHA"),
        ("$x\U00020000$", _UNSET + "U+20000 CJK UNIFIED IDEOGRAPH-20000"),
        ("$x́$", "combining marks in a formula are not allowed"),
        ("$a⃗$", "combining marks in a formula are not allowed"),
        (r"$\pmb{a + \pmb{x}}$", r"TeX \pmb inside \pmb is not allowed"),
        ("$" + "x" * 2049 + "$", "TeX formulas over 2048 characters are not allowed"),
        ("$" + "\U0001d465" * 1025 + "$", "TeX formulas over 2048 characters are not allowed"),
        (r"$\,$ and $x$", "TeX formulas that show nothing are not allowed"),
        (r"${}$ and $x$", "TeX formulas that show nothing are not allowed"),
    ],
)
def test_tex_mathjax_would_set_differently_is_refused(testimony: str, reason: str) -> None:
    r"""MathJax 3.2.2 shows an error in place of each of these formulas, or
    sets it with part of what is written hidden, out of place, or drawn over
    and over: a bracket after ``\begin{aligned}`` is read as where the rows
    sit, a bare ``^\iff`` raises only the space before the arrow, nested
    ``\pmb`` draws its argument once per level, and a formula longer than its
    buffer allows is refused once ``\pmb`` doubles it."""

    assert any(reason in error for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    ("testimony", "rewritten"),
    [
        ("$x\\mathrel{\\in\\text{\u0338}}A$", "$x\\mathrel{\u2209}A$"),
        ("$x\\in\\!\\text{\u0338}A$", "$x\u2209A$"),
        ("$a=\\text{\u0338}b$", "$a\u2260b$"),
        ("$x\\text{\u0301}$", "$\\acute{x}$"),
        ("$\\text{e\u0301 b}$", "$\\text{\u00e9 b}$"),
    ],
)
def test_combining_marks_in_tex_text_are_refused_with_a_rewrite(testimony: str, rewritten: str) -> None:
    r"""MathJax sets a combining mark in ``\text`` as a symbol of its own,
    drawn over the one before it, so ``\in\text{\u0338}`` shows as a
    struck-out relation the model read as ``\in``."""

    assert any(error.startswith("combining marks in a formula are not allowed") for error in _testimony_errors(testimony))
    assert _testimony_errors(rewritten) == ()


@pytest.mark.parametrize(
    "testimony",
    [
        r"$\begin{aligned}[t] x &= y \end{aligned}$ and $\begin{gathered}[ b ] x \end{gathered}$",
        r"$\begin{aligned}{}[x] &= y \end{aligned}$",
        r"$x^{\sin} + y_{\dots} + a^{\pmb{b}} + P^{\iff}$, $\frac\sin x$, and $x^\operatorname{f}$",
        "$f’(x) = f'(x)$ and $x’^a$",
        r"$\begin{aligned} \sum_i & a \end{aligned}$",
        "$\\text{5€ and ɑ}$, $é + α + ∀$, and $\U0001d465$",
        "$" + "x" * 2048 + "$",
        "$" + r"\pmb{" + "x" * 2042 + "}$",
    ],
)
def test_tex_mathjax_sets_as_written_is_accepted(testimony: str) -> None:
    assert _testimony_errors(testimony) == ()


def test_a_double_integral_spelled_with_negative_space_is_refused_with_a_hint() -> None:
    assert (
        "repeated negative TeX spacing is not allowed: it slides symbols over one another; "
        "write \\iint for a double integral"
    ) in _testimony_errors(r"$\int\!\!\int_D f$")


@pytest.mark.parametrize(
    ("testimony", "delimiter"),
    [
        ("It costs $5 and $x$ more.", "$"),
        ("Both $$b$$ inline.", "$"),
        (r"So a \\( b holds.", "\\("),
        (r"See \begin{equation} a = b \end{equation} here.", "\\begin{"),
        (r"See \ref{x} here.", "\\ref{"),
        (r"See \eqref{x} here.", "\\eqref{"),
        ("A path C:" + "\\" * 4 + "$x and more.", "$"),
        (r"$a \( b$", "\\("),
        (r"$a \] b$", "\\]"),
    ],
)
def test_math_delimiters_outside_a_formula_are_refused(testimony: str, delimiter: str) -> None:
    r"""MathJax reads the page's text for ``$``, ``\(``, ``\[``, and
    environments, so TeX the renderer did not mark as a formula would be
    typeset unchecked. Inside a formula they are not TeX at all."""

    assert any(
        error.startswith("math delimiters the renderer did not read as a formula are not allowed: " + delimiter)
        for error in _testimony_errors(testimony)
    )


def test_a_dollar_sign_is_written_with_a_backslash() -> None:
    r"""The renderer keeps ``\$``, which MathJax shows as a dollar sign."""

    testimony = r"It costs \$5 and $x$ more."

    assert _testimony_errors(testimony) == ()
    assert render_testimony(testimony) == r'<p>It costs \$5 and <span class="arithmatex">\(x\)</span> more.</p>'


def test_formula_delimiters_and_a_tab_are_not_allowlisted_tex() -> None:
    r"""In a formula ``\(`` and the rest show in red, and the renderer turns a
    tab into spaces, so ``\<tab>`` reaches MathJax as a control space."""

    assert not {"\\(", "\\)", "\\[", "\\]", "\\\t"} & set(_TESTIMONY_TEX)
    assert "\t" not in render_testimony("$a\\\tb$")
    assert _testimony_errors("$a\\\tb$") == ()


def test_a_link_definition_is_refused_even_when_no_link_uses_it() -> None:
    assert "Markdown link definitions are not allowed" in _testimony_errors(
        "The claim holds.\n\n[x]: https://example.test"
    )


@pytest.mark.parametrize(
    "testimony",
    [
        r"$\quad$",
        r"$\text{ }$",
        r"$\,$",
        "$$ $$",
        "```\n\n```",
        r"$\mathrm{}$",
        r"$\begin{cases}\end{cases}$",
        "( . , ; )",
    ],
)
def test_writer_refuses_testimony_without_a_letter_or_digit(testimony: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe read-back testimony") as refused:
        _file(tmp_path, testimony)

    assert "renders no visible text: it must show at least one letter or digit" in str(refused.value)


@pytest.mark.parametrize(
    ("testimony", "limit"),
    [
        ("[" * 8000, "opening brackets"),
        ("`" * 4000, "run of 4000 backticks"),
        ("a `b` " * 600, "backticks"),
        ("a $b$ " * 2000, "math delimiters"),
        ("".join("  " * depth + "- a\n" for depth in range(512)), "columns deep"),
        ("word " * 7000, "-byte limit"),
        ("x\n" * 600, "-line limit"),
        ("_a " * 300, "underscores that start a word"),
        ("*a" * 1100, "asterisks"),
        ("\\" * 3000 + "x", "backslashes"),
        ("[" * 256 + "\\*" * 2000, "opening brackets"),
        ("Read back.\n\n" + "|" * 10501 + "\n|" + "-|" * 10500 + "\n" + "a\n" * 490, "cells"),
        ("Read back.\n\n" + "|" * 7901 + "\n|" + ":-|" * 7900 + "\n" + "a\n" * 490, "cells"),
        ("> " + "|" * 2101 + "\n> |" + "-|" * 2100 + "\n" + "> a\n" * 400, "cells"),
    ],
)
def test_testimony_over_a_limit_is_refused_before_it_is_parsed(
    testimony: str, limit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parsing is superlinear in spans and openers, cubic in a backtick run,
    and recursive in nesting: 8,000 brackets took ten seconds, 4,000 backticks
    over two minutes, and a list 512 levels deep overflowed the stack. 256
    brackets before 16,000 escapes took sixteen. A table's rows are filled
    out to its header's width, so a 32 KiB table of 10,500 columns and 490
    lines rendered 46 MB of HTML, and took a minute and 3 GB to check."""

    def unbounded(*args: object, **kwargs: object) -> None:
        raise AssertionError("the Markdown renderer ran on testimony over a limit")

    monkeypatch.setattr("autoform_cli.readback.markdown_renderer.Markdown", unbounded)

    assert any(limit in error for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    ("at_limit", "over", "reason"),
    [
        ("a" * 32768, "a" * 32769, "testimony is 32769 bytes, over the 32768-byte limit"),
        ("a\n" * 499 + "a", "a\n" * 500 + "a", "testimony has 501 lines, over the 500-line limit"),
        ("$a$ " * 512, "$a$ " * 512 + "\\$", "testimony has 1025 math delimiters, over the limit of 1024"),
        ("`a` " * 256, "`a` " * 256 + "\\`", "testimony has 513 backticks, over the limit of 512"),
        ("`" * 16 + "a" + "`" * 16, "`" * 17 + "a" + "`" * 17, "testimony has a run of 17 backticks, over the limit of 16"),
        ("[a] " * 64, "[a] " * 65, "testimony has 65 opening brackets, over the limit of 64"),
        ("> " * 32 + "a", "> " * 32 + " a", "testimony nests blocks 65 columns deep, over the limit of 64"),
        (">\t" * 16 + "a", ">\t" * 16 + " a", "testimony nests blocks 65 columns deep, over the limit of 64"),
        (" _a" * 256, " _a" * 257, "testimony has 257 underscores that start a word, over the limit of 256"),
        ("*a* " * 512, "*a* " * 512 + "\\*", "testimony has 1025 asterisks, over the limit of 1024"),
        ("a" + "\\." * 2048, "a" + "\\." * 2049, "testimony has 2049 backslashes, over the limit of 2048"),
        (
            "|a" * 2048 + "|\n" + "|-" * 2048 + "|",
            "|a" * 2049 + "|\n" + "|-" * 2049 + "|",
            "testimony has tables of up to 2049 cells, over the limit of 2048",
        ),
        (
            "| a " * 8 + "|\n" + "|:-:" * 8 + "|\n" + ("| a " * 8 + "|\n") * 255,
            "| a " * 8 + "|\n" + "|:-:" * 8 + "|\n" + ("| a " * 8 + "|\n") * 256,
            "testimony has tables of up to 2056 cells, over the limit of 2048",
        ),
    ],
)
def test_testimony_at_a_limit_is_accepted_and_one_more_is_refused(at_limit: str, over: str, reason: str) -> None:
    assert _testimony_errors(at_limit) == ()
    assert _testimony_errors(over) == (reason,)


def test_html_over_the_rendered_limit_is_refused_before_it_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backstop for any construct the renderer expands. Escaping takes the
    largest testimony to at most five times its size, well under the limit."""

    assert len(render_testimony("&" * 32767).encode()) < TESTIMONY_MAX_RENDERED_BYTES

    def unbounded(*args: object, **kwargs: object) -> None:
        raise AssertionError("the rendered HTML was parsed over the limit")

    monkeypatch.setattr("autoform_cli.readback.TESTIMONY_MAX_RENDERED_BYTES", 64)
    monkeypatch.setattr("autoform_cli.readback.html5lib.parseFragment", unbounded)

    assert _testimony_errors("a" * 58) == ("testimony renders to 65 bytes of HTML, over the limit of 64",)


def test_tags_are_text_to_the_renderer_and_need_no_limit() -> None:
    """The renderer reads no HTML, so a thousand nested tags, which overflowed
    the stack of a parser that did, are refused by name like one tag, and
    openers that close nothing are text."""

    assert _testimony_errors("<b>" * 1000 + "x") == ("raw HTML is not allowed: <b>; in a formula, put a space after <",)
    assert _testimony_errors("<!--" * 300 + "x") == (
        "HTML comments are not allowed: Markdown viewers hide the text they enclose",
    )
    assert _testimony_errors(("<a " * 10900)[:32000]) == ()


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        ("P <!-- and Q --> holds", "HTML comments are not allowed"),
        ("P <!--\n\nand Q\n\n--> holds", "HTML comments are not allowed"),
        ("<s>and Q</s>", "raw HTML is not allowed: <s>, </s>"),
        ('<span style="display:none">and Q</span> P', "raw HTML is not allowed: <span>, </span>"),
        ("P\n<div markdown=1>\n*and Q*\n</div>", "raw HTML is not allowed: <div>, </div>"),
        ("<!DOCTYPE html>\nP", "raw HTML is not allowed: <!DOCTYPE"),
        ("P <?php echo 1 ?> Q", "raw HTML is not allowed: <?php"),
        ("For $G=<g>$ the claim holds.", "raw HTML is not allowed: <g>; in a formula, put a space after <"),
        ('P *<b x="*">* Q', "raw HTML is not allowed: <b>"),
        ("Write `<b>`, not <b>Q.", "raw HTML is not allowed: <b>"),
        ("P &amp; Q", "HTML character references are not allowed: &amp; (U+0026 AMPERSAND)"),
        ("P &#8203; Q", "HTML character references are not allowed: &#8203; (U+200B ZERO WIDTH SPACE)"),
        ("P &ZeroWidthSpace; Q", "HTML character references are not allowed: &ZeroWidthSpace; (U+200B"),
        ("<https://example.test/track> P", "Markdown links, images, and autolinks are not allowed"),
    ],
)
def test_html_outside_code_is_refused_by_name(testimony: str, reason: str) -> None:
    """The site shows HTML in testimony as typed, but a CommonMark viewer of
    the vault, GitHub's or Obsidian's, reads it outside code, formulas
    included, where it can hide or restyle text or stand for a character a
    reader cannot see."""

    assert any(reason in error for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    ("testimony", "shown"),
    [
        ("Its type is `List<T>`, not `Array<Nat>`.", "<code>List&lt;T&gt;</code>"),
        ("Here `&amp;` is literal.", "<code>&amp;amp;</code>"),
        ("Lean:\n\n    <b>x</b> &amp; y\n", "<code>&lt;b&gt;x&lt;/b&gt; &amp;amp; y</code>"),
        ("```\n<!-- x --> &#8203;\n```", "<code>&lt;!-- x --&gt; &amp;#8203;</code>"),
        ("R&D; is a label, and &notx; is not a reference.", "R&amp;D; is a label, and &amp;notx; is"),
        ("P &#8203 Q is not a reference without its semicolon.", "P &amp;#8203 Q"),
    ],
)
def test_html_in_code_and_lookalikes_are_shown_as_typed(testimony: str, shown: str, tmp_path: Path) -> None:
    """Code is literal in every viewer, and CommonMark decodes a reference only
    with its semicolon and a name HTML defines. Each is escaped exactly once."""

    _file(tmp_path, testimony)

    assert shown in render_testimony(testimony)


@pytest.mark.parametrize(
    ("testimony", "message"),
    [
        (
            "$" + " ".join(f"\\cmd{c}" for c in "abcdefghij") + "$ holds.",
            r"TeX outside the read-back allowlist is not allowed: \cmda, \cmdb, \cmdc, \cmdd, \cmde, \cmdf, \cmdg, "
            r"\cmdh, and 2 more",
        ),
        ("$\\" + "a" * 100 + "$ holds.", "TeX outside the read-back allowlist is not allowed: \\" + "a" * 60 + "..."),
        (
            "Tags " + " ".join(f"<t{i}>" for i in range(10)) + " here.",
            "raw HTML is not allowed: <t0>, <t1>, <t2>, <t3>, <t4>, <t5>, <t6>, <t7>, and 2 more; "
            "in a formula, put a space after <",
        ),
        (
            "A tag <" + "a" * 100 + "> here.",
            "raw HTML is not allowed: <" + "a" * 60 + "...; in a formula, put a space after <",
        ),
        (
            "Hidden " + "".join(chr(code) for code in [*range(0x200B, 0x2010), *range(0x2060, 0x2065)]) + " here.",
            "invisible or reordering characters are not allowed: U+200B ZERO WIDTH SPACE, "
            "U+200C ZERO WIDTH NON-JOINER, U+200D ZERO WIDTH JOINER, U+200E LEFT-TO-RIGHT MARK, "
            "U+200F RIGHT-TO-LEFT MARK, U+2060 WORD JOINER, "
            "U+2061 FUNCTION APPLICATION, U+2062 INVISIBLE TIMES, and 2 more",
        ),
    ],
)
def test_a_message_names_at_most_eight_things_and_cuts_long_names(testimony: str, message: str) -> None:
    """A message stays short whatever the testimony holds."""

    assert message in _testimony_errors(testimony)


def test_the_read_back_guide_states_the_rules_the_validator_applies() -> None:
    """The guide's rewrites pass and show what they rewrite, its limits are the validator's, and the commands it
    names are refused, so the guide and the validator cannot drift apart."""

    guide = " ".join(
        (Path(__file__).resolve().parent.parent / "skills/human-review/references/readback.md")
        .read_text(encoding="utf-8")
        .split()
    )
    rewrites = re.findall(r"`([^`]+)`, not `([^`]+)`", guide)
    assert rewrites
    for good, bad in rewrites:
        assert not _testimony_errors(good) and _testimony_errors(bad), (good, bad)
        assert re.sub(r"\W", "", good) == re.sub(r"\W", "", bad), (good, bad)
    *languages, last = sorted(_TESTIMONY_LANGUAGES)
    for limit in (
        f"over {TESTIMONY_MAX_BYTES // 1024} KiB",
        f"one formula may space {_TEX_MAX_SPACING // 18} em",
        f"a read-back {_TEX_MAX_TESTIMONY_SPACING // 18} em",
        f"more than {('no', 'one', 'two', 'three', 'four')[_MAX_STACKED_MARKS]} accents above or below",
        ", ".join(f"`{language}`" for language in languages) + f", or `{last}`",
    ):
        assert limit in guide, limit
    quads = int(re.search(r"as much as (\d+) `\\quad`", guide).group(1))
    assert not _testimony_errors("$a" + r"\quad" * quads + " b$")
    assert _testimony_errors("$a" + r"\quad" * (quads + 1) + " b$")
    named = re.findall(r"`(\\[A-Za-z]+)`", re.search(r"refused by name, including (.*?) and macro", guide).group(1))
    assert named
    for command in named:
        assert f"TeX outside the read-back allowlist is not allowed: {command}" in _testimony_errors(
            f"$a {command} b$"
        )


@pytest.mark.parametrize(
    "testimony",
    [
        "Compare [the source](https://example.test) with the statement.",
        "Compare ![the source](https://example.test/a.png) with the statement.",
    ],
)
def test_links_and_images_are_refused_by_name(testimony: str) -> None:
    """A link or image is refused as one, not as HTML the renderer does not emit."""

    errors = _testimony_errors(testimony)

    assert errors == ("Markdown links, images, and autolinks are not allowed",)


@pytest.mark.parametrize(
    "header", ["{.lean .mermaid}", "{.lean .x}", "{.lean .language-x}", "{.lean .bp-readback-current}"]
)
def test_a_fence_header_with_a_second_class_is_refused_as_an_attribute(header: str) -> None:
    """The site's diagram script picks out every element of class mermaid, and
    its styles others, so no class from a fence header passes as part of a
    language."""

    errors = _testimony_errors(f"Read back.\n\n```{header}\ngraph TD\nA-->B\n```\n")

    assert "user-supplied Markdown attributes are not allowed" in errors


#: The first and last code point of each Default_Ignorable_Code_Point range in
#: Unicode 17.0's DerivedCoreProperties.txt, and blank or hidden characters of
#: other kinds: a blank Braille pattern, private use, unassigned, and the line
#: and paragraph separators.
_INVISIBLE = (
    0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180F, 0x200B, 0x200F, 0x202A, 0x202E, 0x2060,
    0x206F, 0x3164, 0xFE00, 0xFE0F, 0xFEFF, 0xFFA0, 0xFFF0, 0xFFF8, 0x1BCA0, 0x1BCA3, 0x1D173, 0x1D17A, 0xE0000,
    0xE0100, 0xE0FFF, 0x2800, 0xE000, 0x0378, 0x2028, 0x2029,
)


@pytest.mark.parametrize("code", _INVISIBLE, ids=[f"U+{code:04X}" for code in _INVISIBLE])
def test_invisible_characters_are_refused_by_code_point(code: int) -> None:
    """Each is refused by its code point, four in a row too: four variation
    selectors after a letter fit under the limit on combining marks."""

    errors = _testimony_errors(f"The claim x{chr(code) * 4} holds.")

    assert any(
        error.startswith("invisible or reordering characters are not allowed") and f"U+{code:04X} " in error
        for error in errors
    )


@pytest.mark.parametrize(
    "testimony",
    [
        "## The statement is correct {#result}\n\nFor all $n$.",
        "Approved\n--------\n\nFor all $n$.",
        "# Basics\n\nFor all $n$.",
    ],
)
def test_headings_are_refused(testimony: str) -> None:
    """A heading inside a card reads as one of the page's own."""

    assert any("Markdown headings are not allowed" in error for error in _testimony_errors(testimony))


def test_a_hash_that_starts_a_line_without_a_space_is_text() -> None:
    """CommonMark, and so a viewer of the vault, reads no heading here."""

    testimony = "Open Mathlib PR\n#41755\nproves a matching bound."

    assert _testimony_errors(testimony) == ()
    assert render_testimony(testimony) == "<p>Open Mathlib PR\n#41755\nproves a matching bound.</p>"


_UNEVEN = "Markdown table rows with more or fewer cells than the header are not allowed"


@pytest.mark.parametrize(
    "testimony",
    [
        "| claim | verdict |\n|---|---|\n| $f$ is continuous | faithful | NOT faithful: it assumes $x \\ne 0$ |\n",
        "| W0 | W1 |\n|---|---|\n| W2 |\n",
        "W0 | W1\n---|---\nW2 | W3 | W4\n",
        "| W0 |\n|---|\n| W1 | W2 |\n",
        "| W0 | W1 |\n|---|---|\n| W2 | W3 |\nW4 W5 | W6 | W7\n",
        "| W0 | W1 |\n|---|---|\n| W2 | W3 |\n# Verdict: faithful\n",
        "> | W0 | W1 |\n> |---|---|\n> | W2 | W3 | W4 |\n",
        "- W9\n\n    | W0 | W1 |\n    |---|---|\n    | W2 | W3 | W4 |\n",
        "| W0 | W1 |\n|---|---|\n| $|x|$ | W4 |\n",
    ],
)
def test_table_rows_with_more_or_fewer_cells_than_the_header_are_refused(testimony: str) -> None:
    """The renderer drops the cells past the header's, so a reader would not
    see them, and reads a line run on after a table as one of its rows."""

    assert any(error.startswith(_UNEVEN) for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    "testimony",
    [
        "| claim | verdict |\n|---|---|\n| $f$ is continuous | faithful |\n",
        "| a \\| b | $\\lvert x \\rvert$ |\n|:---|---:|\n| c | d |\n",
        "| only |\n|---|\n",
        "W0 | W1\n---|---\nW2 | W3\n\nAfter the table.",
    ],
)
def test_tables_whose_rows_match_the_header_are_accepted(testimony: str) -> None:
    assert _testimony_errors(testimony) == ()


_VIEWER = "testimony a CommonMark viewer such as GitHub shows differently is not allowed"
_LINKS = "Markdown links, images, and autolinks are not allowed"
_DEFINITIONS = "Markdown link definitions are not allowed"
_FOOTNOTES = "footnote definitions are not allowed"
_LANGUAGES = "code fences naming anything but lean, lean4, or text are not allowed"


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        (
            "The lemma states that $f$ is continuous; see [the faithful verdict][v]. ![Approved][b]\n\n"
            "[v]: /approved 'The statement drops the hypothesis\nx > 0, so it is not faithful'\n"
            "[b]: https://attacker.example/badge.svg 'x\ny'\n",
            _DEFINITIONS,
        ),
        ("W0 [a][v] W1\n\n[v]: /u 'W2\nW3'\n", _LINKS),
        ("W0\r[v]: /u\rW1 [a][v]\n", _DEFINITIONS),
        ("W0 [a\\]][a\\]]\n\n[a\\]]: /u\n", _DEFINITIONS),
        ("W0\n\n> [v]: /u\n", _DEFINITIONS),
        ("- W0\n\n  [v]: /u\n", _DEFINITIONS),
        ("W0\n\n   [v]: /u\n", _DEFINITIONS),
        ("The verdict is $\\text{[faithful](https://attacker.example/approved)}$ for this card.", _LINKS),
        ("Badge: $\\text{![Approved](https://attacker.example/badge.svg)}$ shown.", _LINKS),
        ("The map $f[x](y)$ is continuous.", _LINKS),
        ("W0 the lemma holds for all $x$.\n\n[^1]: W1 not faithful W2\n", _FOOTNOTES),
        ("W0\n[^a]: W1\nW2", _FOOTNOTES),
        ("W0 [^a] W1\n\n[^a]: W2 W3\n\nW4", _FOOTNOTES),
        ("- W0\n\n  [^a]: W1\n", _FOOTNOTES),
        ("> W0\n>\n> [^a]: W1\n", _FOOTNOTES),
        ("W0\n\n```text W1 but the Lean statement assumes x positive W2\ncode\n```\n", _LANGUAGES),
        ("W0\n\n~~~text W1 not faithful W2\ncode\n~~~\n", _LANGUAGES),
        ("W0\n\n```{.lean The Lean statement drops the hypothesis}\ncontinuous f\n```\n", _LANGUAGES),
        ("W0\n\n```{.text W1 W2 W3}\nx\n```\n", _LANGUAGES),
        ("W0\n\n```the.statement.drops.the.hypothesis.so.it.is.NOT.faithful\ncontinuous f\n```\n", _LANGUAGES),
        ("W0\n\n```python\nx = 1\n```\n", _LANGUAGES),
        ("The read-back is\nfaithful and approved\n---\n\nW0", "Markdown headings are not allowed"),
        ("Verdict: the statement is\nfaithful\n===\n", "Markdown headings are not allowed"),
        ("1) # Verdict: faithful\n", "Markdown headings are not allowed"),
        ("> [!IMPORTANT]\n> W1\n", "GitHub alerts are not allowed"),
        ("W0\n\n>  [!note]  \n> W1\n", "GitHub alerts are not allowed"),
        ("| W0 | W1 |\n|---|---|\n| `W2 | W3` | W4 |\n", _VIEWER),
        ("3. W0\n4. W1\n", _VIEWER),
        ("1. W0\n   1. W1\n", _VIEWER),
    ],
)
def test_markdown_a_commonmark_viewer_reads_differently_is_refused(testimony: str, reason: str) -> None:
    """GitHub shows the card files, and it reads Markdown the CommonMark way:
    a link definition, footnote, heading, alert, fence info word, or table
    cell the site shows as text, or hides, it hides, shows, or numbers
    differently, so a reader of either would miss what the other sees."""

    assert any(error.startswith(reason) for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    "testimony",
    [
        "The statement:\n```lean\ntheorem x : True\n```",
        "The statement:\n\n```lean\ntheorem x : True\n```\n\nholds.",
        "```Lean4\ntheorem x : True\n```\n\nThe statement holds.",
        "```text\nx > 0\n```\n\nThe statement holds.",
        "1. W0\n    1. W1\n",
        "- W0\n  - W1\n",
        "Steps:\n1. W0\n2. W1",
        "The interval $[0, 1]$ and the set [x] are fine.",
        "~~~\nplain code\n~~~\n\nThe statement holds.",
    ],
)
def test_markdown_both_readings_show_alike_is_accepted(testimony: str) -> None:
    assert _testimony_errors(testimony) == ()


def test_a_commonmark_reading_that_differs_names_where() -> None:
    errors = _testimony_errors("3. W0\n4. W1\n")

    assert errors == (
        f'{_VIEWER}: the site shows "1 W0 2 W1" where it shows "3 W0 4 W1"; put a blank line before lists, tables, '
        "and code, indent nested lists four spaces, number lists from 1, and write \\| for a pipe in a table cell, "
        "in code too",
    )


@pytest.mark.parametrize(
    ("markup", "reason"),
    [
        ("Some <span style='display:none'>hidden</span> text.", "raw HTML is not allowed: <span>, </span>;"),
        ("Fish &amp; chips.", "HTML character references are not allowed: &amp; (U+0026 AMPERSAND);"),
        ("A zero width&#8203 space.", "HTML character references are not allowed: &#8203 (U+200B ZERO WIDTH SPACE);"),
        ("<!-- a note that never ends", "HTML comments are not allowed"),
        ("For $a < b$ and $b > c$, AT&T.", None),
        ("Code `<b>` is shown as typed.", None),
    ],
)
def test_markdown_the_site_converter_renders_is_checked_for_raw_html(markup: str, reason: str | None) -> None:
    """Markdown the site's own converter renders, which reads HTML, is refused
    for any HTML in it outside code, which it shows as typed, and for a
    character reference without its semicolon, which that converter completes."""

    errors = publishable_article(markup)[1]

    assert errors == () if reason is None else any(error.startswith(reason) for error in errors)


def test_a_card_over_a_limit_is_invalid_without_being_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every card in a pull request is read before its validity is known."""

    path = _file(tmp_path, "A plain mathematical statement.")
    path.write_text(
        path.read_text(encoding="utf-8").replace("A plain mathematical statement.", "[" * 8000),
        encoding="utf-8",
    )

    def unbounded(*args: object, **kwargs: object) -> None:
        raise AssertionError("the Markdown renderer ran on a card over a limit")

    monkeypatch.setattr("autoform_cli.readback.markdown_renderer.Markdown", unbounded)
    card = load_readbacks(tmp_path)[("af_0123456789abcdef01234567", _declaration().name)]

    assert not card.valid
    assert f"testimony has 8000 opening brackets, over the limit of {TESTIMONY_MAX_BRACKETS}" in card.validation_errors


def test_card_frontmatter_round_trips_quoted_names_and_models(tmp_path: Path) -> None:
    declaration = replace(_declaration(), name="Review.«name: quoted»")
    model = 'reviewer: "strict" # literal'

    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model=model,
        text="A plain mathematical statement.",
        packet_text=declaration.blind_text(),
    )
    source = path.read_text(encoding="utf-8")
    card = load_readbacks(tmp_path)[
        ("af_0123456789abcdef01234567", declaration.name)
    ]

    assert f"declaration: {json.dumps(declaration.name, ensure_ascii=False)}" in source
    assert f"model: {json.dumps(model, ensure_ascii=False)}" in source
    assert card.declaration == declaration.name
    assert card.model == model
    assert card.valid


def test_parser_rejects_bare_identity_frontmatter(tmp_path: Path) -> None:
    declaration = _declaration()
    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text="A plain mathematical statement.",
        packet_text=declaration.blind_text(),
    )
    source = path.read_text(encoding="utf-8")
    path.write_text(source.replace('model: "reviewer"', "model: reviewer"), encoding="utf-8")

    card = load_readbacks(tmp_path)[
        ("af_0123456789abcdef01234567", declaration.name)
    ]

    assert not card.valid
    assert "frontmatter field 'model' must be a JSON double-quoted string" in card.validation_errors


@pytest.mark.parametrize("model", ["line\nbreak", "tab\tlabel", "control\x00label"])
def test_writer_rejects_control_characters_in_model_labels(model: str, tmp_path: Path) -> None:
    declaration = _declaration()

    with pytest.raises(ValueError, match="printable, single-line"):
        write_readback(
            tmp_path,
            article_id="af_0123456789abcdef01234567",
            declaration=declaration,
            model=model,
            text="A plain mathematical statement.",
            packet_text=declaration.blind_text(),
        )
