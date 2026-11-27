- **Articles' facts in frontmatter.** No command but `migrate links` and
  `links set` reads them. `check` and the other readers warn once, counting
  such articles and naming the first five; `render` refuses, exiting 1 and
  writing nothing, so a Pages build fails and keeps the last good site instead
  of publishing those articles with no facts. Once every agent working on the
  project runs 0.7.0 or later and CI's `AUTOFORM_REF` is 0.7.0 or later, move
  them into `blueprint/links/` with `autoform migrate links blueprint --write`
  in a hand-authored vault, by its generator in a generated one (#843).
