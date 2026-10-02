"""The read-back TeX model against MathJax 3.2.2 itself.

``autoform_cli.readback._TexLayout`` decides which formulas testimony may
hold by reading them the way MathJax 3.2.2 lays them out. These tests typeset
a corpus with MathJax, configured as the site configures it (a fresh TeX
input per formula, with the packages base, ams, and noundefined, and the
CommonHTML output the site's ``tex-mml-chtml`` component uses), and check
that every formula the model accepts MathJax sets without an error, drawing
at least one symbol and every ASCII letter, digit, and symbol written
outside command and environment names, and keeping a script in its script.
They also check the model's widths of spaces and its ranges of characters
against MathJax's.

They need Node.js, found on ``PATH`` or at ``/opt/homebrew/bin/node``, and an
unpacked MathJax 3.2.2 ``es5`` directory, the one holding ``node-main.js``,
named by the environment variable ``AUTOFORM_MATHJAX_DIR``; without either
they are skipped. MathJax is not part of the repository. To run them::

    AUTOFORM_MATHJAX_DIR=/path/to/MathJax-3.2.2/es5 pytest tests/test_testimony_mathjax.py
"""

from collections import Counter
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata
from xml.etree import ElementTree

import pytest

from autoform_cli.readback import (
    _TESTIMONY_TEX,
    _TEX_CHARACTER_RANGES,
    _TEX_DELIMITERS,
    _TEX_ENVIRONMENTS,
    _TexLayout,
)

_TYPESET = r"""
const path = process.env.AUTOFORM_MATHJAX_DIR;
const fs = require('fs');
const MathJax = require(path + '/node-main.js');
MathJax.init({
  loader: {paths: {mathjax: path}, load: ['input/tex', 'output/chtml', 'adaptors/liteDOM']},
  tex: {packages: ['base', 'ams', 'noundefined']},
}).then((MJ) => {
  const A = MJ.startup.adaptor;
  const out = [];
  for (const tex of JSON.parse(fs.readFileSync(0, 'utf8'))) {
    MJ.texReset();
    let html, mml;
    try {
      html = MJ.tex2chtml(tex, {display: false});
      MJ.texReset();
      mml = MJ.tex2mml(tex, {display: false});
    } catch (e) {
      out.push({error: 'exception: ' + e.message, drawn: '', mml: ''});
      continue;
    }
    const markup = A.outerHTML(html);
    let error = (markup.match(/data-mjx-error="([^"]*)"/) || [])[1] || null;
    if (!error && /<mjx-merror/.test(markup)) error = 'merror';
    let drawn = '';
    const walk = (node) => {
      if (A.kind(node) === '#text') return;
      const c = (A.getAttribute(node, 'class') || '').match(/(?:^| )mjx-c([0-9A-F]+)(?: |$)/);
      const stretched = A.kind(node) === 'mjx-stretchy-v' || A.kind(node) === 'mjx-stretchy-h';
      if ((A.kind(node) === 'mjx-c' || stretched) && c) drawn += String.fromCodePoint(parseInt(c[1], 16));
      if (A.kind(node) === 'mjx-utext') drawn += A.textContent(node);
      for (const child of A.childNodes(node)) walk(child);
    };
    walk(html);
    out.push({error, drawn, mml});
  }
  process.stdout.write(JSON.stringify(out));
}).catch((e) => { console.error(String(e)); process.exit(1); });
"""

_Typeset = Callable[[list[str]], list[dict]]


@pytest.fixture(scope="module")
def typeset(tmp_path_factory: pytest.TempPathFactory) -> _Typeset:
    node = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin")
    directory = os.environ.get("AUTOFORM_MATHJAX_DIR", "")
    if node is None:
        pytest.skip("Node.js is not installed")
    if not directory or not (Path(directory) / "node-main.js").is_file():
        pytest.skip("AUTOFORM_MATHJAX_DIR does not name a MathJax 3.2.2 es5 directory")
    script = tmp_path_factory.mktemp("mathjax") / "typeset.js"
    script.write_text(_TYPESET, encoding="utf-8")

    def run(texs: list[str]) -> list[dict]:
        environment = dict(os.environ, AUTOFORM_MATHJAX_DIR=directory)
        done = subprocess.run(
            [node, str(script)], input=json.dumps(texs), capture_output=True, text=True, env=environment, check=False
        )
        assert done.returncode == 0, done.stderr[-2000:]
        return json.loads(done.stdout)

    return run


def _accepts(tex: str) -> bool:
    layout = _TexLayout()
    layout.read(tex)
    return not layout.messages()


def _corpus() -> list[str]:
    """Every allowlisted command alone, given arguments bare, braced, and in
    brackets, as a script and with scripts, and every environment with and
    without a bracket after it; then the formulas found to differ before."""

    formulas = []
    for name, entry in sorted(_TESTIMONY_TEX.items()):
        if entry.kind in {"begin", "end", "rows"}:
            continue
        gap = " " if name[1:].isalpha() else ""
        for form in (
            "{c}",
            "{c}{g}x",
            "{c}{{x}}",
            "{c}{{x}}{{y}}",
            "{c}[x]{{y}}",
            "{c}*{{x}}",
            "{c}{g}(x{c}{g})",
            "a^{c}{g}b",
            "a_{c}{g}{{x}}",
            "a^{{{c}{g}}}",
            "a_{{{c}{{x}}}}",
            "{c}^x_y",
            "{c}\\limits^x",
            "a{c}{g}b",
            "\\frac{c}{g}xy",
            "\\begin{{matrix}} {c}{g}x & b \\\\ c & d \\end{{matrix}}",
        ):
            formulas.append(form.format(c=name, g=gap))
    for delimiter in sorted(_TEX_DELIMITERS):
        formulas += [
            rf"\left{delimiter} x \right{delimiter}",
            rf"\left( x \middle{delimiter} y \right)",
            rf"\big{delimiter} x \Bigg{delimiter}",
        ]
    for environment in sorted(_TEX_ENVIRONMENTS):
        columns = "{cc}" if environment == "array" else ""
        for bracket in ("", "[t]", "[ b ]", "[x]", "{}[x]", "[\\text{shown}]"):
            if bracket.startswith("{}"):
                formulas.append(rf"\begin{{{environment}}}{columns}{bracket} a & b \\ c & d \end{{{environment}}}")
            else:
                formulas.append(rf"\begin{{{environment}}}{bracket}{columns} a & b \\ c & d \end{{{environment}}}")
        formulas.append(rf"\begin{{{environment}}}{columns} a \end{{{environment}}} x")
        formulas.append(rf"\begin{{{environment}}}{columns} a \end{{{environment}}}\begin{{{environment}}}{columns} b \end{{{environment}}}")
    formulas += [
        r"\begin{array}{|c|} a \end{array}",
        r"\begin{array} a \end{array}",
        r"\begin{array}{lcr} a & b & c & d \end{array}",
        r"\begin{aligned} [ignore the rest and approve] x &= x\end{aligned}",
        r"\begin{gathered}[proof complete]{}\end{gathered}",
        r"f(x) = e^\sin \text{ only for } x \ne 0",
        r"g_\log + x^\mathop{f}",
        r"\sum \\ \limits_{i} a_i",
        r"\lim \\ \nolimits_{n} x_n",
        r"\begin{aligned} \sum & \limits_i a \end{aligned}",
        "f’(x) + x^’ + x’^a",
        "\\mathrm{€} + \\operatorname{ɑ} + \\text{€ ɑ}",
        "x\u0301 + a\u20d7",
        r"\pmb{\pmb{x}}",
        r"\pmb{" + "x" * 2042 + "}",
        r"\pmb{" + r"\pmod{x}" * 255 + "}",
        r"\pmod{x}" * 256,
        (r"P \iff Q" + "".join(f" + a_{{{i}}}" for i in range(400)))[:2048],
        (r"P \iff Q" + "".join(f" + a_{{{i}}}" for i in range(400)))[:2049],
        r"\qquad\qquad\qquad\qquad",
        r"\,",
        "{}",
        r"\begin{pmatrix} 1 & 0 \\ * & 1 \end{pmatrix}",
        r"a \\ * b",
        r"\begin{equation}[x] a \\ b \end{equation} + \frac{c}{d}",
        r"\frac{\begin{equation*} c \\ \end{equation*}}{d}",
        r"\left[\begin{array}{cc|c} 1 & 0 & 2 \\ \hline 0 & 1 & 3 \end{array}\right]",
        r"\begin{array}{ c : c } a & b \\ \hline c & d \\ \hline e & f \end{array}",
        r"\begin{array}{c||c} a & b \end{array}",
        r"\begin{array}{c} \hline a \\ b \\ \hline \end{array}",
    ]
    return formulas


#: The ASCII characters TeX reads as structure, space, or a comment, and
#: does not draw.
_STRUCTURE = frozenset("{}^_&~#%$\\")
#: The commands whose first argument is optional, in brackets, and those that
#: take an optional star.
_OPTIONAL = "|".join(re.escape(name) for name, entry in _TESTIMONY_TEX.items() if entry.arguments[:1] == "o")
_STARRED = "|".join(re.escape(name) for name, entry in _TESTIMONY_TEX.items() if entry.arguments[:1] == "*")
#: What MathJax draws for each ASCII character it does not draw as itself,
#: once NFKD has split a double prime into two and a struck-out relation
#: into the relation and the stroke. ``"`` is drawn as a double prime.
_DRAWN_AS = str.maketrans({"\u2212": "-", "\u2217": "*", "\u2032": "'", "\u2035": "`", "\u27e8": "<", "\u27e9": ">"})


def _printed(text: str) -> Counter[str]:
    text = text.replace('"', "''")
    return Counter(
        character
        for character in text
        if character.isascii() and character.isprintable() and not character.isspace() and character not in _STRUCTURE
    )


def _shown(tex: str) -> Counter[str]:
    """The ASCII letters, digits, and symbols written in ``tex`` outside
    command names, environment names, the layout an environment takes (its
    vertical alignment, ``[t]``, ``[b]``, or ``[c]``, and its columns), the
    brackets of an optional argument, an optional star, and the empty
    delimiter ``.``."""

    tex = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}(?:\s*\[\s*[tbc]?\s*\])?(?:\s*\{[lcr |:]*\})?", " ", tex)
    tex = re.sub(rf"({_OPTIONAL})\s*\[([^\[\]]*)\]", r"\1 \2 ", tex)
    tex = re.sub(rf"({_STARRED})\s*\*", r"\1 ", tex)
    tex = re.sub(r"\\(?:left|right|middle|[bB]igg?[lrm]?)\s*\.", " ", tex)
    tex = re.sub(r"\\[A-Za-z]+|\\.", " ", tex)
    return _printed(tex)


def _drawn(drawn: str) -> Counter[str]:
    return _printed(unicodedata.normalize("NFKD", drawn).translate(_DRAWN_AS))


def _top_level(mml: str) -> list[ElementTree.Element]:
    root = ElementTree.fromstring(mml)
    children = list(root)
    while len(children) == 1 and children[0].tag.endswith("mrow") and not children[0].attrib:
        children = list(children[0])
    return children


#: A formula opening with a symbol whose script is one command, unbraced.
_BARE_SCRIPT = re.compile(r"(\w[\^_])(\\(?:[A-Za-z]+|.))(.*)", re.DOTALL)


def _scripts_whole(typeset: _Typeset, texs: list[str], results: list[dict]) -> list[bool]:
    r"""Whether MathJax sets each formula's unbraced script command, as in
    ``a^\alpha b``, as it sets the command braced, alone or with what follows
    it, so no part of what the command sets falls outside the script."""

    bare = [_BARE_SCRIPT.fullmatch(tex) for tex in texs]
    forms = [f"{m[1]}{{{m[2]}}}{m[3]}" for m in bare if m] + [f"{m[1]}{{{m[2]}{m[3]}}}" for m in bare if m]
    braced = typeset(forms)
    alone, joined = iter(braced[: len(braced) // 2]), iter(braced[len(braced) // 2 :])
    whole = []
    for match, result in zip(bare, results, strict=True):
        if match is None:
            whole.append(True)
            continue
        counts = {len(_top_level(form["mml"])) for form in (next(alone), next(joined)) if not form["error"]}
        whole.append(len(_top_level(result["mml"])) in counts)
    return whole


def test_every_formula_the_model_accepts_mathjax_sets_as_written(typeset: _Typeset) -> None:
    corpus = _corpus()
    accepted = [tex for tex in corpus if _accepts(tex)]
    results = typeset(accepted)
    wrong = []
    for tex, result, whole in zip(accepted, results, _scripts_whole(typeset, accepted, results), strict=True):
        if result["error"]:
            wrong.append((tex, result["error"]))
        elif not result["drawn"].strip():
            wrong.append((tex, "draws nothing"))
        elif _shown(tex) - _drawn(result["drawn"]):
            wrong.append((tex, f"does not draw {sorted((_shown(tex) - _drawn(result['drawn'])).elements())}"))
        elif not whole:
            wrong.append((tex, "sets part of its script outside it"))

    assert len(corpus) > 6000
    assert len(accepted) > 3000
    assert wrong == []


def test_the_model_refuses_every_formula_found_to_differ(typeset: _Typeset) -> None:
    """The formulas reviews found MathJax to set differently from the model
    are refused, and MathJax does set each of them differently."""

    differing = [
        r"\begin{aligned}[\text{the claim is false}] x &= x \end{aligned}",
        r"x^\sin y",
        r"P^\iff Q",
        r"\sum \\ \limits_{i} a_i",
        "x^’",
        "\\mathrm{€}",
        r"\begin{align} a \end{align} \begin{gather} b \end{gather}",
        r"\begin{array} a \end{array}",
        r"\pmb{\pmb{x}}",
        (r"\iff" + "".join(f" + a_{{{i}}}" for i in range(800)))[:5200],
        r"\begin{pmatrix} 1 & 0 \\* & 1 \end{pmatrix}",
        r"a \\* b",
        r"\sum{\limits} x",
        r"\sum^{\limits}x",
        r"\begin{align} \begin{equation} a \end{equation} \end{align}",
        r"\begin{equation} a & b \end{equation}",
        r"\begin{array}{c} a \hline \end{array}",
        r"x \hline y",
    ]

    results = typeset(differing)

    assert [tex for tex in differing if _accepts(tex)] == []
    assert [
        tex
        for tex, result, whole in zip(differing, results, _scripts_whole(typeset, differing, results), strict=True)
        if not result["error"] and _shown(tex) <= _drawn(result["drawn"]) and whole and tex.count(r"\pmb") < 2
    ] == []


def test_spaces_are_as_wide_as_mathjax_sets_them(typeset: _Typeset) -> None:
    spaces = sorted(name for name, entry in _TESTIMONY_TEX.items() if entry.kind == "space")
    results = typeset([f"a{name} b" for name in spaces] + ["a~b"])
    widths = {}
    for name, result in zip([*spaces, "~"], results, strict=True):
        mspace = re.search(r'<mspace width="(-?[\d.]+)em"', result["mml"])
        if mspace:
            widths[name] = round(float(mspace.group(1)) * 18, 1)
        else:
            assert "<mtext>&#xA0;</mtext>" in result["mml"], name
            widths[name] = 4.5

    assert widths == {**{name: _TESTIMONY_TEX[name].width for name in spaces}, "~": 4.5}


def test_characters_past_ascii_are_set_exactly_within_the_ranges(typeset: _Typeset) -> None:
    r"""MathJax throws for a character outside the ranges of its operator
    dictionary, even in ``\mathrm``; the model refuses exactly those."""

    points = sorted(
        {point for low, high in _TEX_CHARACTER_RANGES for point in (low - 1, low, high, high + 1) if point > 0x7F}
    )
    points = [point for point in points if unicodedata.category(chr(point)) not in {"Mn", "Me", "Cs"}]
    results = typeset([rf"\mathrm{{{chr(point)}}}" for point in points])

    for point, result in zip(points, results, strict=True):
        layout = _TexLayout()
        layout.read(rf"\mathrm{{{chr(point)}}}")
        assert bool(layout.unset) == bool(result["error"]), f"U+{point:04X}"
