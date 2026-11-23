- **The bundled example's copies of the generated CI files.** The example no
  longer carries `.github/` copies of the verify and Pages workflows, the audit
  helper and the CODEOWNERS example, which GitHub never ran from a subfolder.
  `autoform init` and `autoform project new` write their current versions, the
  workflows and helper only when they can determine an Autoform pin or are
  given one ([workflow pins](autoform_cli/reference/project-setup.md#workflow-pins)).
  Its `mkdocs.yml` drops `javascripts/blueprint-live.js`, which its old pin's
  render still wrote (#863).
