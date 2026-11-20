- **A reference link to a file the site leaves out keeps only its text.** A
  reference link or image whose definition named a file the site leaves out,
  such as a hidden file, one in a `published: false` folder, one of a type the
  site does not publish, or a `sources/` file the verified commit lacks, showed
  its brackets once render dropped the definition. One whose definition named a
  file outside the blueprint, or `atlas.json`, kept it, as an inline image of
  either kept its target, and a strict build fails on such a target. Each now
  keeps only its text, as an inline link does, and a definition that goes takes
  a title on its next line with it (#724).
