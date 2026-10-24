# Autoform repository contracts

Read these contracts when changing coordination state, publication surfaces,
source evidence, generated files, or dependency pins.

## Knowledge-map publication

Full-viewport knowledge maps draw regions only from authored mathematical areas,
never from folders or paths, and reading panels from authored prose. Keep
containment and dependency distinct. Never fit the whole repository. Run
`make test-browser` for any explorer change: `make test` skips the WebKit
suite, which CI runs with `AUTOFORM_REQUIRE_BROWSER_TESTS=1`.

## Coordination and local views

- `refs/autoform-claims/*` is shared cross-machine coordination state. Pushing
  claim refs is intentional when the user authorizes that outward-facing action.
- Keep the live dashboard and publication overlay local-only. Loopback services
  must validate request-level Host and Origin and must not expose secrets or
  local paths.

## Source evidence

- Source indexes, revisions, and links retain the descriptors they read
  through: repeated pathname reads are not a generation boundary, so where
  descriptors cannot be retained (Windows) a render omits code links. Read
  bounded outputs before descendants, keep each marker schema in its owning
  feature, and require links to match the blob at the stable detected commit.
- Treat a rewritten private declaration's safety as fail-closed evidence:
  correlate its official user name to its lexical declaration by source
  coordinates.

## Generated mirrors

The bundled example mirrors only artifacts with explicit equivalence tests:

- generated workflow templates, after substituting the example's concrete
  Autoform source and ref;
- `.github/autoform_audit.py` byte-for-byte, which itself carries
  `autoform_cli/probes/audit_probe.lean` verbatim (`scripts/sync_audit_probe.py`
  rewrites both copies); and
- significant `mkdocs.yml` settings except the example-specific site name and
  repository URL.

Update the template, example, and owning equivalence test together. The root
`.gitignore` is a scaffold input, not a bundled-example mirror; scaffold tests
own its required rules.

## Pins and releases

- GitHub Actions and Autoform workflow refs use full commit SHAs. Autoform uses
  the canonical `https://github.com/facebookresearch/autoform-bot.git` source,
  never an Autoform branch, tag, abbreviated SHA, or personal fork.
- An Autoform SHA is a compatibility lock, not an update channel. When the
  example needs newer behavior, update its source and full SHA together, update
  the substitution assertions, and run the example through that exact pin.
- Lean and Mathlib use tested matching release tags, such as `v4.32.2`; they do
  not follow the Autoform full-SHA rule. For each release, regenerate
  `production_module_roots` from Lake package configs and update the private
  creation bundle, catalog identity, and complete `lake update` manifest
  together (a direct-Mathlib-only manifest is invalid), then run `lake build`.
