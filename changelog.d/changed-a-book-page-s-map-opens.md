- **A book page's map opens on its part without reading its search file.** A
  book page below the roadmap now carries the map's chain of parts down to the
  part the map opens for it, with each part's file, and the map's first view
  reads those files at once instead of first reading the map's search file to
  find the chain. The map checks the chain as it walks it, and takes its usual
  path when the page and the map's files disagree. Book pages also ask early,
  at low priority, for the map's script, stylesheet and index (#636, #819).
