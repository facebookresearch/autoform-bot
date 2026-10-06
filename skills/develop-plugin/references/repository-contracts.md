# Autoform repository contracts

Read these contracts when changing coordination state, publication surfaces,
generated files, or dependency pins.

## Coordination and local views

- `refs/autoform-claims/*` is shared cross-machine coordination state. Pushing
  claim refs is intentional when the user authorizes that outward-facing action.
- Keep the live dashboard and publication overlay local-only. Loopback services
  must validate request-level Host and Origin and must not expose secrets or
  local paths.

## Generated mirrors

The bundled example mirrors only artifacts with explicit equivalence tests:

- generated workflow templates, after substituting the example's concrete
  Autoform source and ref;
- `.github/autoform_audit.py` byte-for-byte; and
- significant `mkdocs.yml` settings except the example-specific site name and
  repository URL.

Update the template, example, and owning equivalence test together. The root
`.gitignore` is a scaffold input, not a bundled-example mirror; scaffold tests
own its required rules.

## Pins and releases

- GitHub Actions and Autoform workflow refs use full commit SHAs. Autoform uses
  the canonical `https://github.com/facebookresearch/autoform-bot.git` source,
  never an Autoform branch, tag, abbreviated SHA, or personal fork.
- An Autoform SHA is a compatibility lock, not an update channel. It remains
  reproducible while reachable but becomes feature-stale as `main` advances.
  When the example needs newer behavior, update its source and full SHA
  together, update the substitution assertions, and run the example through
  that exact pin.
- Lean and Mathlib use tested matching release tags, such as `v4.32.2`; they do
  not follow the Autoform full-SHA rule. Update the creation bundle, catalog,
  complete `lake update` manifest, and module roots together, then run
  `lake build`.
