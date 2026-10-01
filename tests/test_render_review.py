from dataclasses import replace
import json
from pathlib import Path
import re

import markdown as markdown_renderer
import pytest

from autoform_cli.readback import (
    _TESTIMONY_TEX,
    TESTIMONY_MAX_BRACKETS,
    Readback,
    _testimony_errors,
    load_readbacks,
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


@pytest.mark.parametrize(
    "shipped",
    [
        "autoform_cli/templates/blueprint/javascripts/mathjax.js",
        "skills/setup/assets/cabannes-thesis-project/blueprint/javascripts/mathjax.js",
    ],
)
def test_shipped_mathjax_configuration_filters_active_math(shipped: str) -> None:
    configuration = (Path(__file__).parents[1] / shipped).read_text(encoding="utf-8")

    assert 'load: ["ui/safe"]' in configuration
    assert 'URLs: "none"' in configuration
    assert 'classes: "none"' in configuration
    assert 'cssIDs: "none"' in configuration
    assert 'styles: "none"' in configuration
    assert 'packages: ["base", "ams", "noundefined"]' in configuration


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
        ("For all $x" + "\u2003" * 2000 + "y$, P.", "U+2003 EM SPACE"),
        ("P" + "\u00a0" * 400 + "and Q.", "U+00A0 NO-BREAK SPACE"),
        ("The bound is \u05d0 > x.", "U+05D0 HEBREW LETTER ALEF"),
        ("The bound is \u0627 > x.", "U+0627 ARABIC LETTER ALEF"),
        ("The sum \u0661 + \u0662 is three.", "U+0661 ARABIC-INDIC DIGIT ONE"),
        ("The claim holds for x" + "\u0301" * 5 + ".", "more than 4 combining marks on one character"),
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
        "The claim holds for x" + "\u0301" * 4 + ".",
        "Vi\u1ec7t, or Vie\u0323\u0302t, names the same place.",
        "The Tibetan stack \u0f66\u0f92\u0fb2\u0f72\u0f7e carries four marks.",
        "The cardinal \u2135 and $\\aleph_0$ are left to right.",
    ],
)
def test_combining_marks_up_to_four_and_left_to_right_letters_are_accepted(testimony: str) -> None:
    assert _testimony_errors(testimony) == ()


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
        (r"$\begin{array}{c} P \end{array}$", r"\begin{array}, \end{array}"),
    ],
)
def test_writer_refuses_tex_outside_the_allowlist_by_name(testimony: str, command: str, tmp_path: Path) -> None:
    """Only the notation statements need is allowed; anything else is named."""

    with pytest.raises(ValueError, match="unsafe read-back testimony") as refused:
        _file(tmp_path, testimony)

    assert "TeX outside the read-back allowlist is not allowed: " + command in str(refused.value)


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
        (r"$P\!Q$ and $P\!\!\,Q$", None),
        (r"$P\!\!Q$", "repeated negative TeX spacing"),
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
        (r"$P\!$ and $\!Q$", None),
        (r"$\frac{a}$ $b$", r"TeX commands missing an argument are not allowed: \frac"),
    ],
)
def test_tex_limits_hold_at_their_exact_values_and_per_formula(testimony: str, reason: str | None) -> None:
    """Every limit admits its value and refuses one more, and none carries
    from one formula to the next. Nesting is capped far below the depth at
    which MathJax overflows its stack, about two hundred."""

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
        (r"$a\mod$", r"TeX commands missing an argument are not allowed: \mod"),
        (r"$\frac{a}$", r"TeX commands missing an argument are not allowed: \frac"),
        (r"$\overset{a}$", r"TeX commands missing an argument are not allowed: \overset"),
        (r"$\sqrt{&}$", r"TeX & and \\ are allowed only between the cells and rows of an environment"),
        (r"$a & b$", r"TeX & and \\ are allowed only between the cells and rows of an environment"),
        (r"$\not 0$", r"\not is allowed only before a relation"),
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
    different symbol, and ``\text{\alpha}``, which it shows as typed."""

    assert any(reason in error for error in _testimony_errors(testimony))


@pytest.mark.parametrize(
    "testimony",
    [
        r"$\left( a \middle| b \right)$ and $\sum\limits_{i} a_i$",
        r"$a \mod n$, $a \not= b \not\in c$, and $x'^a + x_a'$",
        r"$\mathrm{{{x}}}$ and $\text{a \$ b}$",
        r"$a \mathrel{R} b \mathbin{\star} c$ and $P {\scriptscriptstyle \land Q}$",
        r"$\lvert x \rvert$, $\varinjlim_i$, $\textsf{x}$, $\Bbbk$, $\nleftarrow$, and $\circledast$",
        r"$\iint_D f$",
    ],
)
def test_tex_mathjax_sets_cleanly_is_accepted(testimony: str) -> None:
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
    ],
)
def test_testimony_over_a_limit_is_refused_before_it_is_parsed(
    testimony: str, limit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parsing is superlinear in spans and openers, cubic in a backtick run,
    and recursive in nesting: 8,000 brackets took ten seconds, 4,000 backticks
    over two minutes, and a list 512 levels deep overflowed the stack. 256
    brackets before 16,000 escapes took sixteen."""

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
    ],
)
def test_testimony_at_a_limit_is_accepted_and_one_more_is_refused(at_limit: str, over: str, reason: str) -> None:
    assert _testimony_errors(at_limit) == ()
    assert _testimony_errors(over) == (reason,)


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
