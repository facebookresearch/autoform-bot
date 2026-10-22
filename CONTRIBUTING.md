# Contributing to autoform-bot
We want to make contributing to this project as easy and transparent as
possible.

## Our Development Process
Development happens in this repository: changes reach `main` through pull
requests. You need Python 3.10 or newer, [`uv`](https://docs.astral.sh/uv/),
Git, and Make. From a clone:

```bash
make setup          # uv sync --extra dev --extra repl
make lint           # uv run ruff check autoform_cli servers tests
make test           # uv run pytest -q
make check-example  # validate, render, and build the bundled example site
```

CI (`.github/workflows/tests.yml`) runs the same four steps on Python 3.10 and
3.13 for every pull request, for pushes to `main`, and for pushes to `scratch/`
branches, which run CI on a branch with no pull request; a newer commit on a
pull request cancels the older run. Five more jobs run beside it. `WebKit
integrated book and explorer regressions` installs the `browser` extra and WebKit and
runs the graph, integrated navigation and genuine MathJax browser suites with `AUTOFORM_REQUIRE_BROWSER_TESTS=1`,
so a missing browser fails the job instead of skipping its tests; locally they
skip without the extra, and `make test-browser` installs both and runs them as
CI does. `real Lean
(fixture toolchain)` builds and audits a project from the bundled Mathlib
release and runs
the real-Lean suites other than the skeleton suite, which `real Lean skeleton
suite` runs with one `tests/test_project_inspect.py` test. Another job runs the
inspection, bounded-subprocess, and transactional-output tests on Windows, and
the last builds
`tests/fixtures/repl-smoke` and runs `tests/test_real_repl.py` against the
pinned upstream REPL, also checking the header parser on the oldest Lean release
Autoform supports. Locally, tests that need Lean skip when
`lake` is not on `PATH`. Those in `tests/test_skeleton.py`, `test_impact.py`,
`test_contract.py`, `test_lake_artifact_audit.py`, `test_lsp_protocol.py` and
`test_batch_diagnostics.py` also skip when elan is installed but the
toolchain pinned in `tests/fixtures/skeleton-project/lean-toolchain` is
missing; only the real-Lake test in `tests/test_project_inspect.py` lets elan
download it. The `tests/test_library_generate.py`
ones that run Lean build a fixture that clones Batteries from GitHub, so they
run only when `AUTOFORM_REQUIRE_REAL_LIBRARY_TESTS=1` is set, as their CI job
does; a missing toolchain or a failed download then fails them. Run
`lake build` in `skills/setup/assets/cabannes-thesis-project` when you change
the example's Lean sources or declarations.

The book and explorer share a vanilla-JavaScript client. Its readable sources
are `autoform_cli/assets/blueprint-navigation.js` and `blueprint-dag.js`; their
styles are in `blueprint-dag.css`. When changing either script, install Node
22.23.2 and run `make build-explorer`, then `make check-explorer` and
`make test-browser`. Pinned development-only esbuild and Terser write
`blueprint-explorer.min.js` with an exact source digest. CI checks its bytes and
the 132 KB total client budget, including the pinned Dagre 3.1.1 / Graphlib
4.0.5 layout engine and its MIT notice. This replaces the 72 KB reader/map
ceiling to support dependency-driven placement, obstacle-avoiding routes and
protected count labels; graph-node,
payload, search, request and cache limits are unchanged. Only explicitly `$`-prefixed private properties may
be renamed; renderer-private scene and DOM references use local variables.
Graph/index fields, route attributes, renderer callbacks and history
snapshot fields keep their names. Python publication ships the compiled client;
projects consuming Autoform do not need Node or a separate application build.
The pinned Dagre browser distribution is captured into
`blueprint-dagre.vendor.js` during this development build. Its isolated wrapper
and the readable book/map sources are fingerprinted together; the compiled and
raw-source fallback clients both include the engine. Consumers require no Node,
new script tag or runtime CDN dependency.
Compression never renames public properties, assumes getters are pure, enables
unsafe transforms, or drops console calls.

The verify workflow's kernel-trust probe is Lean, kept in
`autoform_cli/probes/audit_probe.lean`. Projects get one self-contained helper,
`.github/autoform_audit.py`, which carries a verbatim copy of that file, so edit
the `.lean` file and then run `python3 scripts/sync_audit_probe.py` to rewrite
the copy in the template and the bundled example. The probe imports only the
toolchain's Lean, so Lean checks the file without a project:
`elan run "$(cat tests/fixtures/library-project/lean-toolchain)" lean
autoform_cli/probes/audit_probe.lean` prints nothing when it is clean, and the
real-Lean tests run that check on both fixture toolchains.

`tests/test_real_repl.py` skips unless `AUTOFORM_RUN_REAL_REPL_TESTS=1`. To run
it locally, build its fixture and install the oldest supported toolchain first:

```bash
(cd tests/fixtures/repl-smoke && lake build Mathlib Secret @repl/repl)
elan toolchain install "$(cat tests/fixtures/minimum-lean/lean-toolchain)"
AUTOFORM_RUN_REAL_REPL_TESTS=1 uv run pytest -q tests/test_real_repl.py
```

## Pull Requests
We actively welcome your pull requests.

1. Fork the repo and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. If you've changed APIs, update the documentation.
4. Ensure the test suite passes.
5. Make sure your code lints.
6. If you haven't already, complete the Contributor License Agreement ("CLA").

## Contributor License Agreement ("CLA")
In order to accept your pull request, we need you to submit a CLA. You only need
to do this once to work on any of Meta's open source projects.

Complete your CLA here: <https://code.facebook.com/cla>

## Issues
We use GitHub issues to track public bugs. Please ensure your description is
clear and has sufficient instructions to be able to reproduce the issue.

Meta has a [bounty program](https://bugbounty.meta.com/) for the safe
disclosure of security bugs. In those cases, please go through the process
outlined on that page and do not file a public issue.

## Coding Style
* Python uses 4-space indentation and a 120-character line length
  (`[tool.ruff]` in `pyproject.toml`). Ruff's default rules do not check line
  length, and a few existing lines run longer.
* `make lint` runs `ruff check` with ruff's default rules on `autoform_cli`,
  `servers`, and `tests`; CI runs the same check.
* CI runs no formatter, so match the surrounding code instead of reformatting
  unrelated lines.

## License
By contributing to autoform-bot, you agree that your contributions will be licensed
under the LICENSE file in the root directory of this source tree.
