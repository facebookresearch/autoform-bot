"""The site's MathJax: one pinned version, configured by the renderer.

MathJax keeps TeX state, the macros, operators, and labels an input has
defined, for every formula that input reads afterwards. So read-back cards are
never typeset with the page: each pass typesets the articles with a new TeX
input, then each card with one of its own that knows only the packages the
testimony validator checks against.

``autoform render`` writes ``javascripts/mathjax.js`` on every build, over the
vault's copy; the script loads the one MathJax version it was written for and
leaves formulas as typed under any other. Project macros come from ``tex-macros.json`` and reach
renderer-marked article formulas only, and the commands that would let one
formula change another, or color a symbol, are turned off. Marked formulas in
links or buttons share their control's keys, never MathJax's menu or explorer.
"""

from __future__ import annotations

import hashlib
import json

from ..markdown import FORMULA_CLASS
from ..tex_macros import NEUTRALISED, TEX_MACROS, _tex_macros
from ._assets import _filled_asset

#: The MathJax release the site loads.
#: :data:`~autoform_cli.tex_macros.NEUTRALISED` and the read-back testimony
#: allowlist were checked against it, so changing it means checking both again.
MATHJAX_VERSION = "3.2.2"
#: Where that release is loaded from, and the component bundle the site uses.
MATHJAX_BASE = f"https://cdn.jsdelivr.net/npm/mathjax@{MATHJAX_VERSION}/es5"
MATHJAX_BUNDLE = f"{MATHJAX_BASE}/tex-mml-chtml.js"
#: The bundle's subresource-integrity hash, so a CDN that serves other bytes
#: for it leaves formulas as typed. The components in :data:`_LOADED`, which
#: MathJax's own loader fetches, are not checked. A new release needs a new one:
#: ``openssl dgst -sha384 -binary tex-mml-chtml.js | openssl base64 -A``.
MATHJAX_INTEGRITY = "sha384-Wuix6BuhrWbjDBs24bXrjf4ZQ5aFeFWBuKkFekO2t8xFU0iNaLQfp2K6/1Nxveei"
#: The script ``autoform render`` writes into the site source.
MATHJAX_SCRIPT = "javascripts/mathjax.js"

#: What article formulas may use: the standard notation, unknown commands in
#: red, the packages authors reach for most, and the project's macros. Of the
#: packages the bundle would load on demand, braket, mhchem and amscd (CD
#: diagrams) are loaded too. The others stay off: cancel, bbox and enclose
#: take a color, as color does; html sets classes, styles and links; unicode
#: picks fonts; action adds tooltips and toggles; newcommand and extpfeil
#: define commands for the formulas after them; verb and bussproofs are not
#: loaded either.
PAGE_PACKAGES = ("base", "ams", "noundefined", "boldsymbol", "mathtools", "braket", "mhchem", "amscd", "configmacros")
#: What a card's formulas may use: exactly the packages the testimony validator
#: checks against. The project's macros are not among them.
CARD_PACKAGES = ("base", "ams", "noundefined")
#: The class of the formulas render writes into a card. Not the articles'
#: :data:`~autoform_cli.markdown.FORMULA_CLASS`, so the articles' input never
#: finds one.
CARD_FORMULA_CLASS = "bp-card-formula"
#: The class of the read-back cards render writes, each typeset by an input
#: of its own.
CARD_CLASS = "bp-readback"
#: The extensions the bundle does not carry, loaded before anything is typeset,
#: and the filter that keeps classes, IDs, styles, and links out of the output.
_LOADED = ("ui/safe", "[tex]/boldsymbol", "[tex]/mathtools", "[tex]/braket", "[tex]/mhchem", "[tex]/amscd")
_SAFE = {"allow": {"URLs": "none", "classes": "none", "cssIDs": "none", "styles": "none"}}

#: The base and ams commands MathJax builds from ``\mmlToken``, which would
#: show in red with it turned off, written as MathJax writes them with
#: ``\mathrm`` in place of ``\mmlToken``. ``\bmod``'s thick spaces on either
#: side become kerns of the same width. A project macro of the same name
#: replaces one.
REBUILT: dict[str, object] = {
    "bmod": "\\kern5mu\\mathrm{mod}\\kern5mu",
    "mod": ["\\mathchoice{\\kern18mu}{\\kern12mu}{\\kern12mu}{\\kern12mu}\\mathrm{mod}\\,\\,#1", 1],
    "pmod": ["\\pod{\\mathrm{mod}\\kern 6mu #1}", 1],
    "varinjlim": "\\mathop{\\underrightarrow{\\mathrm{lim}}}",
    "varliminf": "\\mathop{\\underline{\\mathrm{lim}}}",
    "varlimsup": "\\mathop{\\overline{\\mathrm{lim}}}",
    "varprojlim": "\\mathop{\\underleftarrow{\\mathrm{lim}}}",
}

#: The ``javascripts/mathjax.js`` ``autoform init`` put in vaults before the
#: renderer wrote its own, by SHA-256. An unedited copy is replaced on the site
#: without a word; an edited one is replaced with a warning, since its edits
#: are lost.
_SHIPPED_CONFIGURATIONS = frozenset({"e2fa0fa73dd367cad2f508055c6b6b45c771547aca6c3baae639ecd872112384"})


def mathjax_script(kept: bytes | None, macros: bytes | None) -> tuple[str, list[str]]:
    """The ``javascripts/mathjax.js`` the site gets for a vault, and the
    warnings about it: macros in ``tex-macros.json`` it leaves out, or an
    edited copy of the configuration the vault used to keep, whose edits the
    site loses.

    ``kept`` and ``macros`` are the bytes render captured at those two paths,
    ``None`` where it captured none, so the script is built from the
    ``tex-macros.json`` the site publishes beside it.
    """

    issues: list[str] = []
    if kept is not None:
        digest = hashlib.sha256(kept.replace(b"\r\n", b"\n")).hexdigest()
        if digest not in _SHIPPED_CONFIGURATIONS:
            issues.append(
                f"{MATHJAX_SCRIPT}: autoform render writes the site's MathJax configuration over this "
                f"copy, so its edits are lost; move any tex.macros to {TEX_MACROS} and delete it"
            )
    parsed, macro_issues = _tex_macros(macros)
    return _script(parsed), issues + macro_issues


def _script(macros: dict[str, object]) -> str:
    """The script itself, with everything it is configured by in one object."""

    settings = {
        "version": MATHJAX_VERSION,
        "base": MATHJAX_BASE,
        "bundle": MATHJAX_BUNDLE,
        "integrity": MATHJAX_INTEGRITY,
        "load": list(_LOADED),
        "safe": _SAFE,
        "page": {
            "packages": list(PAGE_PACKAGES),
            "macros": {
                **REBUILT,
                **dict(sorted(macros.items())),
                **{name: "\\notallowed" for name in NEUTRALISED},
            },
            "inlineMath": [["$", "$"], ["\\(", "\\)"]],
            "displayMath": [["$$", "$$"], ["\\[", "\\]"]],
        },
        # Each card formula is one of these, as render writes it.
        "card": {
            "packages": list(CARD_PACKAGES),
            "inlineMath": [["\\(", "\\)"]],
            "displayMath": [["\\[", "\\]"]],
        },
        # The input MathJax makes for itself when it starts, which no pass
        # uses, finds nothing, so the document it starts with never holds a
        # formula, whatever its menu does.
        "startup": {
            "packages": ["base"],
            "inlineMath": [],
            "displayMath": [],
            "processEscapes": False,
            "processEnvironments": False,
            "processRefs": False,
        },
        # The formulas the site's Markdown marks, which are all the articles'
        # input reads; read-back cards and the formulas render marks in them,
        # which are all a card's input reads; and a class pattern nothing
        # matches, so no class in a page makes an input read anything else.
        "formulas": FORMULA_CLASS,
        "cards": CARD_CLASS,
        "cardFormulas": CARD_FORMULA_CLASS,
        "nothing": "(?!)",
    }
    return _filled_asset("mathjax.js", SETTINGS_JSON=json.dumps(settings, indent=2, ensure_ascii=True))


__all__ = [
    "CARD_CLASS",
    "CARD_FORMULA_CLASS",
    "CARD_PACKAGES",
    "MATHJAX_SCRIPT",
    "MATHJAX_VERSION",
    "REBUILT",
    "mathjax_script",
]
