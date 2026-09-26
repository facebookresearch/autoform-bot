window.MathJax = {
  loader: {load: ["ui/safe"]},
  options: {
    safeOptions: {
      allow: {URLs: "none", classes: "none", cssIDs: "none", styles: "none"}
    }
  },
  tex: {
    packages: {"[-]": ["require"]},
    inlineMath: [["$", "$"], ["\\(", "\\)"]],
    displayMath: [["$$", "$$"], ["\\[", "\\]"]]
  }
};
