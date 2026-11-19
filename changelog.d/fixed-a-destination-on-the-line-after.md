- **A destination on the line after its label.** The site reads a reference
  definition whose target starts on the next line (`[n]:`, then the path), and
  an inline link or image whose destination does (`[text](`, then the path), as
  it reads one on a single line, in a quote or a list item too. Render left both
  as written, so a page could link a file the site leaves out, such as a hidden
  one or one outside the blueprint, and fail a strict build, with no warning
  from render or check. Render now moves or drops each as it does one on a
  single line, with the same warning. Check and `autoform move` read such a
  definition where they read one on a single line, not in a quote or on a list
  item's marker line, so a reference link through it counts and moves with its
  target; as for a wrapped link, a target that does not resolve warns rather
  than fails (#808).
