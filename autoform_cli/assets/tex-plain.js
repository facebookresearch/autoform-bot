/* A formula's TeX as plain text, for places that cannot typeset one: a tab title, an accessible name, a tooltip, an
 * SVG label. The explorer and the map share this reducer, and tests/fixtures/tex-plain-cases.json pins its text for
 * each case. Unknown commands read as their names.
 */
(function () {
  "use strict";
  if (window.AutoformTexPlain) return;

  function table(keys, values) {
    var out = {}, chars = Array.from(values || "");
    keys.split(" ").forEach(function (key, index) { out[key] = values ? chars[index] : true; });
    return out;
  }
  var DOUBLE = table("A B C D E F G H I J K L M N O P Q R S T U V W X Y Z", "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ");
  var SUP = table("0 1 2 3 4 5 6 7 8 9 + - = ( ) a b c d e f g h i j k l m n o p r s t u v w x y z ′",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ′");
  var SUB = table("0 1 2 3 4 5 6 7 8 9 + - = ( ) a e h i j k l m n o p r s t u v x", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ");
  var SYMBOLS = {
    alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε", zeta: "ζ", eta: "η", theta: "θ",
    vartheta: "ϑ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π", varpi: "ϖ", rho: "ρ",
    varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ", upsilon: "υ", phi: "φ", varphi: "φ", chi: "χ", psi: "ψ",
    omega: "ω", Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ", Upsilon: "Υ",
    Phi: "Φ", Psi: "Ψ", Omega: "Ω", ell: "ℓ", hbar: "ℏ", aleph: "ℵ", wp: "℘", Re: "ℜ", Im: "ℑ", infty: "∞",
    emptyset: "∅", varnothing: "∅", partial: "∂", nabla: "∇", forall: "∀", exists: "∃", neg: "¬", lnot: "¬",
    sum: "∑", prod: "∏", coprod: "∐", int: "∫", oint: "∮", bigcup: "⋃", bigcap: "⋂", bigoplus: "⨁",
    bigotimes: "⨂", circ: "∘", pm: "±", mp: "∓", prime: "′", ast: "∗", star: "⋆", bullet: "•",
    langle: "⟨", rangle: "⟩", lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉", vert: "|", lvert: "|", rvert: "|",
    Vert: "‖", lVert: "‖", rVert: "‖", dots: "…", ldots: "…", cdots: "⋯", dotsc: "…", dotsb: "⋯", vdots: "⋮",
    quad: " ", qquad: " ", colon: ": ", bmod: " mod ", mod: " mod ",
    // Relations, arrows and binary operators keep a space each side; runs of spaces collapse.
    to: " → ", rightarrow: " → ", leftarrow: " ← ", longrightarrow: " → ", Rightarrow: " ⇒ ", Leftarrow: " ⇐ ",
    implies: " ⇒ ", Leftrightarrow: " ⇔ ", iff: " ⇔ ", leftrightarrow: " ↔ ", mapsto: " ↦ ", hookrightarrow: " ↪ ",
    twoheadrightarrow: " ↠ ", le: " ≤ ", leq: " ≤ ", ge: " ≥ ", geq: " ≥ ", ne: " ≠ ", neq: " ≠ ", ll: " ≪ ",
    gg: " ≫ ", approx: " ≈ ", sim: " ∼ ", simeq: " ≃ ", cong: " ≅ ", equiv: " ≡ ", propto: " ∝ ", in: " ∈ ",
    notin: " ∉ ", ni: " ∋ ", subset: " ⊂ ", subseteq: " ⊆ ", subsetneq: " ⊊ ", supset: " ⊃ ", supseteq: " ⊇ ",
    cup: " ∪ ", cap: " ∩ ", setminus: " ∖ ", times: " × ", cdot: " · ", otimes: " ⊗ ", oplus: " ⊕ ", wedge: " ∧ ",
    land: " ∧ ", vee: " ∨ ", lor: " ∨ ", mid: " ∣ ", nmid: " ∤ ", parallel: " ∥ ", perp: " ⊥ ", vdash: " ⊢ ",
    models: " ⊨ ", lhd: " ◁ ", unlhd: " ⊴ ", trianglelefteq: " ⊴ ", triangleleft: " ◁ "
  };
  // After \not, a relation's negated sign.
  var NEGATED = {"∈": "∉", "∋": "∌", "=": "≠", "≤": "≰", "≥": "≱", "⊂": "⊄", "⊆": "⊈", "⊃": "⊅", "⊇": "⊉", "∣": "∤",
    "∥": "∦", "≡": "≢", "∼": "≁", "≃": "≄", "≅": "≇", "≈": "≉", "⊢": "⊬", "⊨": "⊭"};
  var ESCAPES = {",": " ", ";": " ", ":": " ", " ": " ", "!": "", "|": "‖", "\\": " "};
  var OPERATORS = table("arccos arcsin arctan arg cos cosh cot coth csc deg det dim exp gcd hom inf ker lcm lg lim liminf " +
    "limsup ln log max min Pr sec sin sinh sup tan tanh");
  var FONTS = table("mathrm mathit mathbf mathsf mathtt mathcal mathscr mathfrak operatorname text textrm textit textbf " +
    "textsf boldsymbol bm");
  var ACCENTS = {bar: "\u0304", overline: "\u0305", hat: "\u0302", widehat: "\u0302", tilde: "\u0303",
    widetilde: "\u0303", vec: "\u20d7", dot: "\u0307", ddot: "\u0308"};
  var SIZES = table("left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr");

  // Whether s needs no parentheses beside a "/" or a script: one character, letters and digits, or one parenthesis.
  function atomic(s) {
    if (Array.from(s).length <= 1 || /^[A-Za-z0-9]+$/.test(s)) return true;
    if (s.charAt(0) !== "(" || s.charAt(s.length - 1) !== ")") return false;
    for (var depth = 0, index = 0; index < s.length; index++) {
      depth += s.charAt(index) === "(" ? 1 : s.charAt(index) === ")" ? -1 : 0;
      if (!depth && index < s.length - 1) return false;
    }
    return true;
  }
  function paren(s) { return atomic(s) ? s : "(" + s + ")"; }
  // A script in Unicode's raised or lowered characters when it has one for each, else after its ^ or _.
  function script(s, mark) {
    var chars = Array.from(s.replace(/ /g, "")), lifted = mark === "^" ? SUP : SUB;
    if (!chars.length) return "";
    var up = chars.map(function (c) { return lifted[c]; });
    return up.every(Boolean) ? up.join("") : mark + paren(chars.join(""));
  }

  function math(tex) {
    tex = String(tex || "");
    var at = 0;
    // One argument: a braced group, a command or a character.
    function argument() {
      while (tex.charAt(at) === " ") at++;
      if (tex.charAt(at) === "{") { at++; return group(true); }
      if (tex.charAt(at) === "\\") return command();
      return tex.charAt(at++);
    }
    // Text up to the brace that closes the group, or to the end.
    function group(braced) {
      var out = "";
      while (at < tex.length) {
        var c = tex.charAt(at++);
        if (c === "}") { if (braced) return out; }
        else if (c === "{") out += group(true);
        else if (c === "\\") { at--; out += command(); }
        else if (c === "^" || c === "_") out = out.replace(/ $/, "") + script(argument(), c);
        else out += c === "~" || c === "&" ? " " : c;
      }
      return out;
    }
    function command() {
      var name = /^[A-Za-z]+/.exec(tex.slice(++at));
      if (!name) { var c = tex.charAt(at++); return ESCAPES[c] !== undefined ? ESCAPES[c] : c; }
      name = name[0]; at += name.length;
      if (tex.charAt(at) === "*") at++;
      if (/^[dt]?frac$/.test(name)) { var top = argument(); return paren(top) + "/" + paren(argument()); }
      if (name === "binom") { var n = argument(); return "(" + n + " choose " + argument() + ")"; }
      if (name === "sqrt") {
        var index = "";
        if (tex.charAt(at) === "[") {
          var end = tex.indexOf("]", at); if (end < 0) end = tex.length;
          index = script(math(tex.slice(at + 1, end)), "^"); at = end + 1;
        }
        return index + "√" + paren(argument());
      }
      if (name === "mathbb") return Array.from(argument()).map(function (c) { return DOUBLE[c] || c; }).join("");
      if (FONTS[name]) return argument();
      if (ACCENTS[name]) return Array.from(argument()).map(function (c) { return c + ACCENTS[name]; }).join("");
      if (name === "pmod") return " (mod " + argument() + ")";
      if (name === "begin" || name === "end") { argument(); return " "; }
      if (name === "not") { var sign = argument(), bare = sign.trim(); return sign.replace(bare, NEGATED[bare] || bare + "\u0338"); }
      if (SIZES[name]) { if (tex.charAt(at) === ".") at++; return ""; }
      if (OPERATORS[name]) return name + (/[A-Za-z\\]/.test(tex.charAt(at)) ? " " : "");
      return SYMBOLS[name] !== undefined ? SYMBOLS[name] : name;
    }
    // Runs of spaces collapse, and none stays inside a bracket or before a comma.
    return group(false).replace(/\s+/g, " ").replace(/([(\[{⟨]) /g, "$1").replace(/ ([)\]}⟩,])/g, "$1").trim();
  }

  window.AutoformTexPlain = {math: math};
})();
