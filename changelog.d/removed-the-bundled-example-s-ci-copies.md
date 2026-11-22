- **The bundled example's copies of the generated CI files.** The example no
  longer carries `.github/` copies of the verify and Pages workflows, the audit
  helper and the CODEOWNERS example, which GitHub never ran from a subfolder;
  `autoform init` writes the current ones. Its `mkdocs.yml` drops
  `javascripts/blueprint-live.js`, which only its 0.7.0 pin wrote (#863).
