"""A formula's TeX as plain text, for what render writes where nothing typesets: a page's ``<title>``, its MkDocs
header and sidebar entry, and accessible names.

A port of ``assets/tex-plain.js``, the explorer's and the map's reducer, since a title should read the same on the
page and in the browser. Both give the text ``tests/fixtures/tex-plain-cases.json`` records for each case, and the same
text for each entry of their tables. One difference is the port's own: braces nested past Python's stack read as
typed. Unknown commands read as their names.
"""

from __future__ import annotations

import re


def _table(keys: str, values: str) -> dict[str, str]:
    return dict(zip(keys.split(" "), values, strict=True))


_DOUBLE = _table("A B C D E F G H I J K L M N O P Q R S T U V W X Y Z", "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ")
_SUP = _table(
    "0 1 2 3 4 5 6 7 8 9 + - = ( ) a b c d e f g h i j k l m n o p r s t u v w x y z ′",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ′",
)
_SUB = _table("0 1 2 3 4 5 6 7 8 9 + - = ( ) a e h i j k l m n o p r s t u v x", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω", "ell": "ℓ", "hbar": "ℏ",
    "aleph": "ℵ", "wp": "℘", "Re": "ℜ", "Im": "ℑ", "infty": "∞", "emptyset": "∅", "varnothing": "∅", "partial": "∂",
    "nabla": "∇", "forall": "∀", "exists": "∃", "neg": "¬", "lnot": "¬", "sum": "∑", "prod": "∏", "coprod": "∐",
    "int": "∫", "oint": "∮", "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⨁", "bigotimes": "⨂", "circ": "∘", "pm": "±",
    "mp": "∓", "prime": "′", "ast": "∗", "star": "⋆", "bullet": "•", "langle": "⟨", "rangle": "⟩",
    "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "vert": "|", "lvert": "|", "rvert": "|", "Vert": "‖",
    "lVert": "‖", "rVert": "‖", "dots": "…", "ldots": "…", "cdots": "⋯", "dotsc": "…", "dotsb": "⋯", "vdots": "⋮",
    "quad": " ", "qquad": " ", "colon": ": ", "bmod": " mod ", "mod": " mod ",
    # Relations, arrows and binary operators keep a space each side; runs of spaces collapse.
    "to": " → ", "rightarrow": " → ", "leftarrow": " ← ", "longrightarrow": " → ", "Rightarrow": " ⇒ ",
    "Leftarrow": " ⇐ ", "implies": " ⇒ ", "Leftrightarrow": " ⇔ ", "iff": " ⇔ ", "leftrightarrow": " ↔ ",
    "mapsto": " ↦ ", "hookrightarrow": " ↪ ", "twoheadrightarrow": " ↠ ", "le": " ≤ ", "leq": " ≤ ", "ge": " ≥ ",
    "geq": " ≥ ", "ne": " ≠ ", "neq": " ≠ ", "ll": " ≪ ", "gg": " ≫ ", "approx": " ≈ ", "sim": " ∼ ", "simeq": " ≃ ",
    "cong": " ≅ ", "equiv": " ≡ ", "propto": " ∝ ", "in": " ∈ ", "notin": " ∉ ", "ni": " ∋ ", "subset": " ⊂ ",
    "subseteq": " ⊆ ", "subsetneq": " ⊊ ", "supset": " ⊃ ", "supseteq": " ⊇ ", "cup": " ∪ ", "cap": " ∩ ",
    "setminus": " ∖ ", "times": " × ", "cdot": " · ", "otimes": " ⊗ ", "oplus": " ⊕ ", "wedge": " ∧ ", "land": " ∧ ",
    "vee": " ∨ ", "lor": " ∨ ", "mid": " ∣ ", "nmid": " ∤ ", "parallel": " ∥ ", "perp": " ⊥ ", "vdash": " ⊢ ",
    "models": " ⊨ ", "lhd": " ◁ ", "unlhd": " ⊴ ", "trianglelefteq": " ⊴ ", "triangleleft": " ◁ ",
}
# After \not, a relation's negated sign.
_NEGATED = {"∈": "∉", "∋": "∌", "=": "≠", "≤": "≰", "≥": "≱", "⊂": "⊄", "⊆": "⊈", "⊃": "⊅", "⊇": "⊉", "∣": "∤",
            "∥": "∦", "≡": "≢", "∼": "≁", "≃": "≄", "≅": "≇", "≈": "≉", "⊢": "⊬", "⊨": "⊭"}
_ESCAPES = {",": " ", ";": " ", ":": " ", " ": " ", "!": "", "|": "‖", "\\": " "}
_OPERATORS = frozenset(
    "arccos arcsin arctan arg cos cosh cot coth csc deg det dim exp gcd hom inf ker lcm lg lim liminf limsup ln log max "
    "min Pr sec sin sinh sup tan tanh".split()
)
_FONTS = frozenset(
    "mathrm mathit mathbf mathsf mathtt mathcal mathscr mathfrak text textrm textit textbf textsf "
    "boldsymbol bm".split()
)
_ACCENTS = {"bar": "\u0304", "overline": "\u0305", "hat": "\u0302", "widehat": "\u0302", "tilde": "\u0303",
            "widetilde": "\u0303", "vec": "\u20d7", "dot": "\u0307", "ddot": "\u0308"}
_SIZES = frozenset("left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr".split())
_STYLES = frozenset("limits nolimits displaystyle textstyle scriptstyle scriptscriptstyle".split())


# JavaScript's \s and String.prototype.trim(), so the port collapses and trims the characters tex-plain.js does.
_SPACE = "[\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"


def _trim(text: str) -> str:
    return re.sub(f"^{_SPACE}+|{_SPACE}+\\Z", "", text)


def _atomic(text: str) -> bool:
    """Whether ``text`` needs no parentheses beside a "/" or a script: one character, letters and digits, or one
    parenthesis."""

    if len(text) <= 1 or re.fullmatch(r"[A-Za-z0-9]+", text):
        return True
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for index, character in enumerate(text):
        depth += 1 if character == "(" else -1 if character == ")" else 0
        if not depth and index < len(text) - 1:
            return False
    return True


def _paren(text: str) -> str:
    return text if _atomic(text) else f"({text})"


def _script(text: str, mark: str) -> str:
    """A script in Unicode's raised or lowered characters when it has one for each, else after its ^ or _."""

    characters, lifted = text.replace(" ", ""), _SUP if mark == "^" else _SUB
    if not characters:
        return ""
    if all(character in lifted for character in characters):
        return "".join(lifted[character] for character in characters)
    return mark + _paren(characters)


def math(tex: str) -> str:
    """The plain text of the formula ``tex``, written without its delimiters."""

    at = 0

    def char(index: int) -> str:
        return tex[index] if index < len(tex) else ""

    def spaced(word: str) -> str:
        # An operator name, with a space when a letter or a command follows it.
        return word + (" " if re.match(r"[A-Za-z\\]", char(at)) else "")

    def argument() -> str:
        # One argument: a braced group, a command or a character.
        nonlocal at
        while char(at) == " ":
            at += 1
        if char(at) == "{":
            at += 1
            return group(True)
        if char(at) == "\\":
            return command()
        at += 1
        return char(at - 1)

    def group(braced: bool) -> str:
        # Text up to the brace that closes the group, or to the end.
        nonlocal at
        out = ""
        while at < len(tex):
            character = tex[at]
            at += 1
            if character == "}":
                if braced:
                    return out
            elif character == "{":
                out += group(True)
            elif character == "\\":
                at -= 1
                out += command()
            elif character in "^_":
                out = out.removesuffix(" ") + _script(argument(), character)
            else:
                out += " " if character in "~&" else character
        return out

    def command() -> str:
        nonlocal at
        at += 1
        name = re.match(r"[A-Za-z]+", tex[at:])
        if name is None:
            at += 1
            return _ESCAPES.get(char(at - 1), char(at - 1))
        word = name.group()
        at += len(word)
        if char(at) == "*":
            at += 1
        if word in {"frac", "dfrac", "tfrac"}:
            top = argument()
            return _paren(top) + "/" + _paren(argument())
        if word == "binom":
            top = argument()
            return f"({top} choose {argument()})"
        if word == "sqrt":
            index = ""
            if char(at) == "[":
                end = tex.find("]", at)
                if end < 0:
                    end = len(tex)
                index = _script(math(tex[at + 1 : end]), "^")
                at = end + 1
            return index + "√" + _paren(argument())
        if word == "mathbb":
            return "".join(_DOUBLE.get(character, character) for character in argument())
        if word == "operatorname":
            return spaced(argument())
        if word in _FONTS:
            return argument()
        if word in _ACCENTS:
            return "".join(character + _ACCENTS[word] for character in argument())
        if word == "pmod":
            return f" (mod {argument()})"
        if word in {"begin", "end"}:
            argument()
            return " "
        if word == "not":
            sign = argument()
            bare = _trim(sign)
            return sign.replace(bare, _NEGATED.get(bare) or bare + "\u0338", 1)
        if word in _SIZES:
            if char(at) == ".":
                at += 1
            return ""
        if word in _STYLES:
            return ""
        if word in _OPERATORS:
            return spaced(word)
        return _SYMBOLS.get(word, word)

    try:
        text = group(False)
    except RecursionError:
        # Braces nested past Python's stack: the TeX as typed, which a reader can still follow.
        return tex
    # Runs of spaces collapse, and none stays inside a bracket or before a comma.
    text = re.sub(f"{_SPACE}+", " ", text)
    text = re.sub(r"([(\[{⟨]) ", r"\1", text)
    return _trim(re.sub(r" ([)\]}⟩,])", r"\1", text))


def visible(text: str) -> str:
    """``text``, a title as a reader sees it once published, with each ``\\( \\)`` formula as plain text."""

    return re.sub(r"\\\(([\s\S]*?)\\\)", lambda formula: math(formula.group(1)), text)
