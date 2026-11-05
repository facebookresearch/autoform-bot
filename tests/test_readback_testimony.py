import re
import time
from pathlib import Path

import pytest

from autoform_cli.readback import load_readbacks
from autoform_cli.testimony import (
    _MAX_STACKED_MARKS,
    _TEX_MAX_DEPTH,
    _TEX_MAX_LENGTH,
    TESTIMONY_MAX_BYTES,
    TESTIMONY_MAX_LINES,
    errors_in_testimony,
    formula_problems,
    split_testimony,
    typesetting_warnings,
)
from tests.test_readback import _ARTICLE_ID, _blueprint, _file_card


@pytest.mark.parametrize(
    ("testimony", "pieces"),
    [
        ("Let $x$ be.", [("text", "Let "), ("inline", "x"), ("text", " be.")]),
        ("$x$$y$", [("inline", "x"), ("inline", "y")]),
        ("Then $$\\sum a_i$$ holds.", [("text", "Then "), ("display", "\\sum a_i"), ("text", " holds.")]),
        ("It costs \\$5 and $a\\$b$.", [("text", "It costs \\$5 and "), ("inline", "a\\$b"), ("text", ".")]),
        ("A row ends \\\\$x$.", [("text", "A row ends \\\\"), ("inline", "x"), ("text", ".")]),
        ("$$a $ b$$", [("display", "a $ b")]),
        ("Over\n$x\ny$ lines.", [("text", "Over\n"), ("inline", "x\ny"), ("text", " lines.")]),
        ("Left open $x and $$y", [("text", "Left open "), ("inline", "x and "), ("text", "$y")]),
        ("\\(x\\) and \\[y\\] are text.", [("text", "\\(x\\) and \\[y\\] are text.")]),
    ],
)
def test_dollar_signs_are_taken_in_turn_and_a_backslash_escapes_one(testimony: str, pieces: list) -> None:
    assert split_testimony(testimony) == pieces


@pytest.mark.parametrize(
    "testimony",
    [
        # Markdown, HTML, and links are shown as typed, so none can hide or run anything.
        "<script>alert(1)</script> [click](javascript:alert(1)) ![x](https://e.test/p.png) <https://e.test>",
        "# Heading\n\n```mermaid\ngraph TD\n```\n\n*claim*{onclick=alert(1)} &#8203; <!-- note -->",
        "**Precisely:** for $x < y$, the claim holds.\n\n- No extra hypothesis.",
        r"For every $x \in [0, 1]$ and the set $\{x\}$, the claim holds; 50% of cases, and \(x\) as typed.",
        r"For $0<x<1$, $\frac12 < \sqrt[3]{x}$ and $\lfloor x \rfloor = 0$ in $\mathbb R$.",
        r"Here $\operatorname*{argmax}_x f(x)$ and $\left\langle u, v \right\rangle \le \|u\| \|v\|$ hold.",
        "$$f(x) = \\begin{cases} x^2 & \\text{if } x \\ge 0, \\\\ -x & \\text{otherwise} \\end{cases}$$",
        r"$$A = \begin{pmatrix} a & b \\ c & d \end{pmatrix}, \begin{aligned} x &= y \\ &\le z \end{aligned}$$",
        r"Rows: $\begin{aligned} a \\ [b, c] \end{aligned}$, and $\begin{aligned} {} [t] \end{aligned}$.",
        r"At least $50\%$ of cases, a dollar \$, and $\alpha \ne \beta$.",
        # Up to four marks on a letter, two of them above it and two below, as
        # Vietnamese, polytonic Greek, and Tibetan write them.
        "Vi\u1ec7t, or Vie\u0323\u0302t, and the word \u03b1\u0314\u0301\u0345 carry two marks above.",
        "The letter \u1faf carries three marks once decomposed, and the stack \u0f66\u0f92\u0fb2\u0f72\u0f7e four.",
        "The cardinal \u2135 and $\\aleph_0$ are left to right.",
        "x" * TESTIMONY_MAX_BYTES,
        "x\n" * (TESTIMONY_MAX_LINES - 1) + "x",
        "$" + "{" * _TEX_MAX_DEPTH + "x" + "}" * _TEX_MAX_DEPTH + "$",
        "$" + "x" * _TEX_MAX_LENGTH + "$",
        # One space at a time in text, one empty cell or row at a time, and a
        # bracket after an environment that shows it.
        r"$x > 0 \text{ and } y > 0$, $\mbox{for all } n$, and $\begin{matrix} a & & b \\ \\ c \end{matrix}$.",
        r"$\begin{matrix}[t] a \end{matrix}$ and $\begin{pmatrix} [1, 2] \end{pmatrix}$ show their brackets.",
        r"$\operatorname*{argmax}_x f$, $\left. f \right|_{0}^{1}$, ${}^{2}C$, $\frac{1}{2}$, and $\_{}$ show what they hold.",
        # An environment with a delimiter of its own, and text, show even with nothing else in them.
        r"The empty matrix $\begin{pmatrix}\end{pmatrix}$, $\begin{cases}\end{cases}$, and $\text{^}$ show something.",
    ],
)
def test_plain_text_and_allowlisted_tex_are_accepted_and_typeset(testimony: str) -> None:
    assert errors_in_testimony(testimony) == () and typesetting_warnings(testimony) == ()


_STACKED = "more than 4 combining marks on one character, or more than 2 above or below it, are not allowed"
_BASELESS = "combining marks with no character before them in their text are not allowed"
_UNLISTED = "TeX outside the read-back allowlist: "
_UNBALANCED = "unbalanced braces, \\left and \\right, or \\begin and \\end"
_NOTHING_SHOWN = "testimony shows no letter or digit"


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        # Characters a reader would not see, or that reorder what they see.
        ("\u200b", "U+200B ZERO WIDTH SPACE"),
        ("The claim holds\u2060 for all x.", "U+2060 WORD JOINER"),
        ("The bound is \u202e1 > x\u202c for every x.", "U+202E RIGHT-TO-LEFT OVERRIDE"),
        ("The claim holds\U00016fe4 for all x.", "U+16FE4 KHITAN SMALL SCRIPT FILLER"),
        ("The claim\ufff9 holds.", "U+FFF9 INTERLINEAR ANNOTATION ANCHOR"),
        ("P" + "\u00a0" * 400 + "and Q.", "U+00A0 NO-BREAK SPACE"),
        ("A line\rover another.", "U+000D"),
        ("A lone \ud800 surrogate.", "U+D800"),
        ("The bound is \u05d0 > x.", "U+05D0 HEBREW LETTER ALEF"),
        ("The sum \u0661 + \u0662 is three.", "U+0661 ARABIC-INDIC DIGIT ONE"),
        ("An unassigned \U000e0080 here.", "U+E0080"),
        ("A code point \U0010fffe here.", "characters unassigned in this Python's Unicode data"),
        # Marks stacked over the lines around, also on a letter composed in
        # advance, or with nothing of their own to sit on.
        ("The claim holds for x" + "\u0301" * 5 + ".", _STACKED),
        ("The value x" + "\u0323" * 3 + ".", _STACKED),
        ("The letter \u1eaa\u0301\u0302 for $n$.", _STACKED),
        ("We have $a$ $=$\u0338 $b$ for these.", _BASELESS),
        ("\u20ddStart of the paragraph, and the mark \u0301 alone.", _BASELESS),
        # Nothing to read, or too much to check.
        (r"$\text{ }$", _NOTHING_SHOWN),
        ("$$ $$", _NOTHING_SHOWN),
        (r"$\begin{cases}\end{cases}$", _NOTHING_SHOWN),
        ("( . , ; )", _NOTHING_SHOWN),
        ("x" * (TESTIMONY_MAX_BYTES + 1), f"over the {TESTIMONY_MAX_BYTES}-byte limit"),
        ("x\n" * TESTIMONY_MAX_LINES + "x", f"over the {TESTIMONY_MAX_LINES}-line limit"),
    ],
)
def test_testimony_that_hides_what_it_says_is_refused(testimony: str, reason: str) -> None:
    """A reader must be shown everything the card says, and only that."""

    errors = errors_in_testimony(testimony)
    assert any(reason in error for error in errors), errors


@pytest.mark.parametrize(
    ("formula", "reason"),
    [
        # TeX outside the allowlist, named, with what to write instead.
        (r"P \phantom{\land Q}", _UNLISTED + r"\phantom"),
        (r"P \rlap{\land Q} \kern-2em \color{white}{Q}", _UNLISTED + r"\color, \kern, \rlap"),
        (r"\newcommand{\h}[1]{} P \h{\land Q}", _UNLISTED + r"\h, \newcommand"),
        (r"\require{html}\href{javascript:alert(1)}{x} \style{x}{y} \mmlToken{mi}{x}", r"\href, \mmlToken"),
        (r"\Rule{3em}{1em}{0em} \mathchoice{P}{Q}{P}{P} \tiny Q \displaystyle", r"\Rule, \displaystyle"),
        (r"\int f(x)\,dx \quad \! \;", r"\!, \,, \;, \quad; write the formula without spacing commands"),
        (r"a \not= b", r"\not; write \neq, \notin, or another negated relation for \not"),
        (r"{n \choose k} \over 2", r"\choose, \over; write \binom{n}{k} for {n \choose k}; \frac{a}{b}"),
        (r"\begin{array}{cc} a & b \end{array}", r"\begin{array}; write \begin{matrix} for \begin{array}"),
        (r"\begin{align} a \end{align}", r"write $$...$$ around \begin{aligned} or \begin{gathered}"),
        (r"\(x\)", _UNLISTED + r"\(, \)"),
        # Spaces, comments, options, and blanks MathJax hides or spaces by.
        ("a ~ b", "~, a space"),
        ("P(x) % \\land Q(x)\nR", "a TeX comment"),
        (r"\begin{aligned}[t] a \end{aligned}", "a bracket right after \\begin{aligned}"),
        (r"\begin{gathered} [b] \end{gathered}", "a bracket right after \\begin{gathered}"),
        (r"\begin{aligned} P \\[-2em] Q \end{aligned}", "row spacing after \\\\"),
        (r"\begin{aligned} P \\* Q \end{aligned}", "row spacing after \\\\"),
        (r"a & b \\ c", "& or \\\\ outside the cells and rows of an environment"),
        (r"P \text{and         } Q", "a run of spaces in \\text"),
        ("P \\textrm{a\t\tb} Q", "a run of spaces in \\textrm"),
        (r"P \mbox{ } \mbox{ } Q", "\\mbox with only spaces in it"),
        (r"\begin{matrix} & & Q \end{matrix}", "two empty cells one after the other"),
        (r"\begin{matrix} {} & \mathrm{} & \left. \right. & Q \end{matrix}", "two empty cells one after the other"),
        (r"\begin{matrix} P \\ \\ \\ Q \end{matrix}", "two empty rows one after the other"),
        (r"\begin{matrix} P \\ & \\ & \\ Q \end{matrix}", "two empty rows one after the other"),
        (r"\begin{matrix} \begin{matrix}\end{matrix} & \begin{matrix} \end{matrix} & Q \end{matrix}", "two empty cells"),
        # What shows nothing yet takes room, which a run of turns into a blank.
        (r"P \begin{matrix} ^{} & Q \end{matrix}", "an empty superscript or subscript, which shows a blank"),
        (r"P {}_{ } Q", "an empty superscript or subscript, which shows a blank"),
        (r"P \left. \right. Q", "\\left. and \\right. with nothing between them, which show a blank"),
        (r"P \operatorname*{} Q", "\\operatorname with nothing in it, which shows a blank"),
        (r"P \frac{}{ } Q", "\\frac with nothing in it, which shows a blank"),
        (r"P \stackrel{}{} Q", "\\stackrel with nothing in it, which shows a blank"),
        (r"P \bigl. Q", "\\bigl. with nothing to size, which shows a blank"),
        ("", "nothing in it, which shows a blank"),
        (" \n ", "nothing in it, which shows a blank"),
        (r"{} \mathbf{ }", "nothing in it, which shows a blank"),
        (r"\begin{aligned}\end{aligned}", "nothing in it, which shows a blank"),
        (r"\left. \begin{aligned} \\ \end{aligned} \right.", "nothing in it, which shows a blank"),
        (r"\begin{matrix} \begin{matrix}\\\end{matrix} \\ \begin{matrix}\\\end{matrix} \\ Q \end{matrix}", "two empty rows"),
        (r"\begin{matrix} \begin{matrix}\\\end{matrix} & \begin{matrix}\\\end{matrix} & Q \end{matrix}", "two empty cells"),
        (r"\begin{matrix}\text\\\end{matrix} \text x \left( \text\right)", "\\text without braces"),
        # Formulas that are malformed, too deep or long, or hold what MathJax cannot set.
        ("a $ b", "a dollar sign; write \\$ for one"),
        (r"{x", _UNBALANCED),
        (r"\left( x", _UNBALANCED),
        (r"\begin{cases} a \end{matrix}", _UNBALANCED),
        ("{" * (_TEX_MAX_DEPTH + 1) + "x" + "}" * (_TEX_MAX_DEPTH + 1), "it nests more than 16 deep"),
        ("\U0001d400" * (_TEX_MAX_LENGTH // 2 + 1), "over the limit of 2048"),
        ("\U00020000", "characters MathJax cannot set: U+20000"),
        ("z\u0304", "combining marks, which MathJax sets each as a symbol of its own"),
    ],
)
def test_a_formula_that_cannot_be_typeset_is_shown_as_typed_not_refused(formula: str, reason: str) -> None:
    """A site shows such a formula as typed, and the testimony stays valid."""

    problems = formula_problems(formula)
    assert any(reason in problem for problem in problems), problems
    testimony = f"Then $${formula}$$ holds."
    assert errors_in_testimony(testimony) == ()
    assert typesetting_warnings(testimony)[0].endswith("is shown as typed: " + "; ".join(problems))


def test_a_dollar_sign_no_other_closes_is_shown_as_typed_not_refused() -> None:
    assert errors_in_testimony("It costs $5.") == ()
    assert typesetting_warnings("It costs $5.") == (
        "a dollar sign that no other closes is shown as typed: write \\$ for a dollar sign",
    )


def test_the_writer_refuses_unsafe_testimony_before_anything_is_written(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)

    with pytest.raises(ValueError, match=re.escape("unsafe read-back testimony: invisible or reordering characters")):
        _file_card(blueprint, "Let x\u200b hold.")
    assert not (blueprint / "readbacks").exists()


def test_testimony_with_a_formula_shown_as_typed_is_filed_as_a_valid_card(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)

    _file_card(blueprint, r"Let $\phantom{x}$ hold.")
    card = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert card.valid
    assert typesetting_warnings(card.text) == (
        "the formula $\\phantom{x}$ is shown as typed: " + _UNLISTED + "\\phantom",
    )


def test_a_message_names_at_most_eight_things_and_cuts_long_names() -> None:
    """A message stays short whatever the testimony holds."""

    commands = " ".join(f"\\cmd{c}" for c in "abcdefghij")
    assert _UNLISTED + r"\cmda, \cmdb, \cmdc, \cmdd, \cmde, \cmdf, \cmdg, \cmdh, and 2 more" in formula_problems(commands)
    assert _UNLISTED + "\\" + "a" * 60 + "..." in formula_problems("\\" + "a" * 100)
    hidden = "".join(chr(code) for code in [*range(0x200B, 0x2010), *range(0x2060, 0x2065)])
    assert (
        "invisible or reordering characters are not allowed: U+200B ZERO WIDTH SPACE, "
        "U+200C ZERO WIDTH NON-JOINER, U+200D ZERO WIDTH JOINER, U+200E LEFT-TO-RIGHT MARK, "
        "U+200F RIGHT-TO-LEFT MARK, U+2060 WORD JOINER, "
        "U+2061 FUNCTION APPLICATION, U+2062 INVISIBLE TIMES, and 2 more"
    ) in errors_in_testimony(f"Hidden {hidden} here.")
    warnings = typesetting_warnings(" ".join(f"$\\cmd{c}$" for c in "abcdefghij") + " $" + "a~" * 50 + "$ hold.")
    assert len(warnings) == 9 and warnings[-1] == "and 3 more formulas are shown as typed"
    assert warnings[0] == "the formula $\\cmda$ is shown as typed: " + _UNLISTED + "\\cmda"


@pytest.mark.parametrize(
    "testimony",
    [
        "$" + "{" * 8000 + "$",
        "$x$" * (TESTIMONY_MAX_BYTES // 3),
        ("$" + "\\alpha" * 340 + "$\n") * 15,
        "\u0301" * (TESTIMONY_MAX_BYTES // 2),
        "\\$" * (TESTIMONY_MAX_BYTES // 2),
        ("$" + "\\text{" * 340 + "$\n") * 15,
        ("$\\begin{matrix}" + " & {}" * 400 + "\\end{matrix}$\n") * 15,
        ("$\\begin{matrix}" + "\\begin{matrix} &" * 120 + "$\n") * 15,
        ("$" + "{" * 1000 + "\\begin{matrix}" + "&" * 1000 + "$\n") * 15,
        ("$" + "\\frac{" * 300 + "$\n") * 15,
    ],
)
def test_testimony_at_the_size_limit_is_checked_quickly(testimony: str) -> None:
    """Every card in a pull request is checked before its validity is known."""

    started = time.process_time()
    errors_in_testimony(testimony)
    typesetting_warnings(testimony)
    assert time.process_time() - started < 2


def test_the_read_back_guide_states_the_rules_the_validator_applies() -> None:
    """The guide's rewrites are typeset and differ from what they rewrite,
    which is shown as typed, only in punctuation; the commands it names are
    shown as typed; and its limits are the validator's, so the guide and the
    validator cannot drift apart."""

    guide = " ".join(
        (Path(__file__).resolve().parent.parent / "skills/human-review/references/readback.md")
        .read_text(encoding="utf-8")
        .split()
    )
    spans = list(re.finditer(r"(?<!`)(`+)(?!`) ?(.*?) ?(?<!`)\1(?!`)", guide))
    rewrites = [(one[2], other[2]) for one, other in zip(spans, spans[1:]) if guide[one.end() : other.start()] == ", not "]
    assert len(rewrites) >= 2
    for good, bad in rewrites:
        assert not errors_in_testimony(good) and not typesetting_warnings(good) and typesetting_warnings(bad), (good, bad)
        assert re.sub(r"\W", "", good) == re.sub(r"\W", "", bad), (good, bad)
    shown = re.search(r"shown as typed instead of typeset, including (.*?): MathJax", guide).group(1)
    named = re.findall(r"`(\\[A-Za-z,;]+)`", shown)
    assert len(named) >= 10
    for command in named:
        assert any(problem.startswith(_UNLISTED + command) for problem in formula_problems(f"a {command} b")), command
    for limit in (
        f"over {TESTIMONY_MAX_BYTES // 1024} KiB or {TESTIMONY_MAX_LINES} lines",
        f"more than {('no', 'one', 'two', 'three')[_MAX_STACKED_MARKS]} accents above or below",
    ):
        assert limit in guide, limit
