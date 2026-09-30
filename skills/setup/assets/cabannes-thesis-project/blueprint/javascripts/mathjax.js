window.MathJax = {
  loader: {load: ["ui/safe"]},
  options: {
    safeOptions: {
      allow: {URLs: "none", classes: "none", cssIDs: "none", styles: "none"}
    }
  },
  tex: {
    // Only the standard notation statements need. Leaving out require,
    // autoload, newcommand, and configmacros keeps \require, macro
    // definitions, and the packages MathJax would load on demand (html,
    // color, bbox, action, unicode, enclose, cancel, and others) out of
    // reach; noundefined shows an unknown command in red instead of an error.
    packages: ["base", "ams", "noundefined"],
    inlineMath: [["$", "$"], ["\\(", "\\)"]],
    displayMath: [["$$", "$$"], ["\\[", "\\]"]]
  }
};
