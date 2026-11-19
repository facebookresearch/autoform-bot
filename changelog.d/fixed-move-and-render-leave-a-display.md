- **`move` and render leave a display formula as written.** The site reads a
  block from `$$`, `\[` or `\begin{...}` to its closing `$$`, `\]` or
  `\end{...}` as one formula, whose brackets are TeX, but Autoform read links in
  it. In a formula such as `$$S_f[\alpha, \beta](n) = ...$$`, from an ATLAS
  page, the graph read a link to `n`, and `autoform move` rewrote that `(n)` as
  a path when the page moved, warning only that the page shows no link there.
  Where the delimiters stand on lines of their own, render published such an
  `(n)` rebased, as `(definitions/n)` on a leaf in a subfolder, and cut one that
  named a hidden file down to its text. Such a block now holds no links, as on
  the site, so `move` leaves it with the warning it gives for inline math, which
  now names math among what no reader counts (#817).
