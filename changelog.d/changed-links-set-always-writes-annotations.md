- **`links set` always writes annotations.** In a project without
  `blueprint/links/roadmap/_defaults.json`, an article it edits moves into its
  annotation with the facts its frontmatter holds, and the first such move
  writes that file; it no longer edits frontmatter, and no longer refuses an
  annotated article there. Autoform 0.7.x reads what it writes, so a fleet that
  mixes both versions keeps working (#843).
