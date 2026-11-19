- **A revised `mathlib: true` article can be retracted.** Step 6 of the revision
  contract retracted a revised article with `statement=retracted --unset proof`,
  which `links set` refuses on a `mathlib: true` article, since `statement:
  retracted` is invalid beside `mathlib: true`. A revision voids the Mathlib
  claim as it does the statement, so step 6 now also unsets `mathlib`,
  `mathlib_declaration` and `mathlib_file`, and on an article with no `lean:` it
  unsets only those, leaving the article unstated. The error for the pair, and
  the warning for an annotation that records both, now name the three keys
  (#802).
