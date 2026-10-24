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
pull request cancels the older run. Six more jobs run beside it. `WebKit
integrated book and explorer regressions` installs the `browser` extra and WebKit and
runs the graph, integrated navigation and genuine MathJax browser suites with `AUTOFORM_REQUIRE_BROWSER_TESTS=1`,
so a missing browser fails the job instead of skipping its tests; locally they
skip without the extra, and `make test-browser` installs both and runs them as
CI does. The `MathJax read-back cards (Node)` tests skip locally unless `node`
is on `PATH` and `AUTOFORM_MATHJAX_DIR` names an unpacked MathJax 3.2.2 package;
`AUTOFORM_REQUIRE_MATHJAX_TESTS=1`, which CI sets, makes that a failure. Run
`npm ci --ignore-scripts` for their pinned development-only JSDOM document
bridge; it verifies real TeX inputs, not browser layout. Consumers need neither
Node nor JSDOM. The
other jobs, which build and audit a project from the bundled Mathlib release,
run the real-Lean suites, run the inspection, subprocess, and publication tests
on Windows, and run `tests/test_real_repl.py` against the pinned upstream REPL,
are listed with the tests each runs in
[`tests.yml`](.github/workflows/tests.yml). Locally, tests that need Lean skip
when `lake` is not on `PATH`, and most also skip when elan is installed but the
toolchain pinned in `tests/fixtures/skeleton-project/lean-toolchain` is missing;
only the real-Lake test in `tests/test_project_inspect.py` lets elan download
it. `AUTOFORM_REQUIRE_REAL_LEAN_TESTS=1`, which CI sets, makes a missing
toolchain a failure instead. The `tests/test_library_generate.py` tests that run
Lean clone Batteries from GitHub, so they run only with
`AUTOFORM_REQUIRE_REAL_LIBRARY_TESTS=1`, and then a missing toolchain or a
failed download fails them. Run
`lake build` in `skills/setup/assets/cabannes-thesis-project` when you change
the example's Lean sources or declarations.

The book and explorer share the readable `autoform_cli/assets/blueprint-navigation.js`
and `blueprint-dag.js`, with `blueprint-dag.css`. After script edits, use Node
22.23.2 and run `make build-explorer`, `make check-explorer`, and `make test-browser`.
Pinned development-only esbuild/Terser produce `blueprint-explorer.min.js`;
publication checks its exact source digest and fails if the asset is stale or missing.
The combined JS response stays below 132 KB (the former graph-only budget was
66,000 bytes); CSS is a separate response and consumer projects need no Node build.
Compression may rename only explicitly private properties, never graph/index,
route or history fields; it enables no unsafe transforms, getter assumptions,
or dropped console calls. The bounded scene has no vendor layout dependency.

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
