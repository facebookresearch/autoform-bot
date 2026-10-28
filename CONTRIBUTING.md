# Contributing to autoform-bot

## Our Development Process
Changes reach `main` through pull requests. You need Python 3.10 or newer,
[`uv`](https://docs.astral.sh/uv/), Git, and Make. From a clone:

```bash
make setup          # uv sync --extra dev --extra repl
make lint           # uv run ruff check autoform_cli servers tests
make test           # uv run pytest -q
make check-example  # validate, render, and build the bundled example site
```

CI ([`tests.yml`](.github/workflows/tests.yml) lists every job and its tests)
runs these steps on Python 3.10 and 3.13 for pull requests and for pushes to
`main` and to `scratch/` branches, which run CI without a pull request. CI sets
each `AUTOFORM_REQUIRE_*` variable below, so it fails where a local run skips:

- Browser tests skip without the `browser` extra and WebKit; `make test-browser`
  installs both and runs them as CI does (`AUTOFORM_REQUIRE_BROWSER_TESTS=1`).
- MathJax tests skip unless `AUTOFORM_MATHJAX_DIR` names an unpacked MathJax
  3.2.2 package, and the Node ones unless `node` is on `PATH`; CI's MathJax and
  WebKit jobs unpack it (`AUTOFORM_REQUIRE_MATHJAX_TESTS=1`).
  `npm ci --ignore-scripts` supplies the locked JSDOM bridge, not a math engine.
- Lean tests skip without `lake` on `PATH`, and most without the toolchain in
  `tests/fixtures/skeleton-project/lean-toolchain`, which only the real-Lake
  test in `tests/test_project_inspect.py` lets elan download
  (`AUTOFORM_REQUIRE_REAL_LEAN_TESTS=1`).
- The Lean tests in `tests/test_library_generate.py` clone Batteries from GitHub
  and run only with `AUTOFORM_REQUIRE_REAL_LIBRARY_TESTS=1`.
- Run `lake build` in `skills/setup/assets/cabannes-thesis-project` when you
  change the example's Lean sources or declarations.

`autoform_cli/assets/` holds the book/map's shared JS and CSS.
With Node 22.23.2, run `make build-explorer`, `make check-explorer`, and browser tests.
Pinned esbuild/Terser emit one JS response below 144 KB; publication checks the
UTF-8/LF source fingerprint. CRLF checkouts work too. Consumers need no Node.
Compression renames only private properties, never graph/index/route/history
fields, and uses no unsafe transforms or dropped logs.
See [explorer ownership and failure boundaries](docs/explorer-design.md) before changing the client.

The kernel probe `autoform_cli/probes/audit_probe.lean` is copied verbatim to
`.github/autoform_audit.py`; after editing it, run `python3 scripts/sync_audit_probe.py`
to sync the template and example. Check without a project (silent when clean):
`elan run "$(cat tests/fixtures/library-project/lean-toolchain)" lean
autoform_cli/probes/audit_probe.lean`.

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
Submit the [CLA](https://code.facebook.com/cla) once for Meta's open source projects.

## Issues
Use GitHub issues for public bugs, with clear reproduction instructions.
Disclose security bugs through Meta's [bounty program](https://bugbounty.meta.com/),
following its process, not a public issue.

## Coding Style
* Python uses 4-space indentation and a 120-character line length
  (`[tool.ruff]`); `make lint` and CI run only ruff's default rules, which do
  not enforce them.
* There is no formatter: match the surrounding code and leave unrelated lines.

## License
Contributions are licensed under the root LICENSE file.
