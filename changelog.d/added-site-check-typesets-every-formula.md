- **`site check` typesets every formula.** With `--mathjax DIR`, an unpacked
  MathJax 3.2.2 package, `site check` typesets each article formula of the built
  site in Node.js, as the site's own `javascripts/mathjax.js` sets MathJax up.
  It warns about each formula MathJax rejects or shows as written, once with how
  many other pages have it, and about each command MathJax does not know, once
  with its first formula and page, how many formulas and pages use it, and the
  fix: a macro in `tex-macros.json`. The commands `check` names are left to it.
  Without Node.js, a usable package or the site's settings, or past a time limit
  that grows with the formulas, it says so once, and the exit status never
  changes. The generated `blueprint-pages.yml` fetches MathJax 3.2.2 by checksum
  before the build and passes `--mathjax` where its `AUTOFORM_REF`'s site check
  takes it; a project set up before this release gets both steps by copying them
  from the template (#815).
