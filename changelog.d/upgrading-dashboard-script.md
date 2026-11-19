- **Dashboard script.** Delete `javascripts/blueprint-live.js` from
  `extra_javascript` in your project's `mkdocs.yml`. Until you do, browsers
  get a 404 for it, and `site check` reports it once, with the number of other
  pages that load it, without failing the Pages build (#782).
