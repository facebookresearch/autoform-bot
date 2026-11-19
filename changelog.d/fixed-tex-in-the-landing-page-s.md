- **TeX in the landing page's title.** The title atop the landing page showed a
  formula in the README's H1 as TeX source, such as `\(L^p\) spaces`, since
  nothing typeset it there. It now typesets the H1's formulas and code, as a
  statement's title does, and where it can't, as beside a link, reads each
  formula as text, as the browser tab does. A statement's "#" link, whose name
  a screen reader reads, was named only "Link to" for a title that reads as
  spaces, such as `$\,$ $\,$`, and now names the statement's kind, as for an
  empty title (#779).
