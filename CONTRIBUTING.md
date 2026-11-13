# Contributing to autoform-bot

## Our Development Process
Changes reach `main` through pull requests. You need Python 3.11 to 3.14,
[`uv`](https://docs.astral.sh/uv/), Git, and Make. From a clone:

```bash
make setup          # uv sync --extra dev --extra repl
make lint           # uv run ruff check autoform_cli servers tests
make test           # uv run pytest -q
make check-example  # validate, render, and build the bundled example site
```

CI ([`tests.yml`](.github/workflows/tests.yml) lists every job and its tests)
runs these steps on Python 3.11, 3.13 and 3.14 for pull requests and pushes to
`main` and to `scratch/` branches, which run CI without a pull request. CI sets
each `AUTOFORM_REQUIRE_*` variable below, so it fails where a local run skips:

- Browser tests skip without the `browser` extra and WebKit; `make test-browser`
  installs both and runs them as CI does (`AUTOFORM_REQUIRE_BROWSER_TESTS=1`).
- MathJax tests skip unless `AUTOFORM_MATHJAX_DIR` names an unpacked MathJax
  3.2.2 package, and the Node ones unless `node` is on `PATH`; CI's MathJax and
  WebKit jobs unpack it (`AUTOFORM_REQUIRE_MATHJAX_TESTS=1`).
  `npm ci --ignore-scripts` supplies the locked JSDOM bridge, not a math engine.
- Tests that run the site's scripts through `tests/dom_fixture.py` skip without
  `node` or jsdom, and fail on a jsdom other than the locked one; CI's test job
  has both (`AUTOFORM_REQUIRE_NODE_TESTS=1`).
- Lean tests skip without `lake` on `PATH`, and most without the toolchain in
  `tests/fixtures/skeleton-project/lean-toolchain`, which only the real-Lake
  test in `tests/test_project_inspect.py` lets elan download
  (`AUTOFORM_REQUIRE_REAL_LEAN_TESTS=1`).
- The Lean tests in `tests/test_library_generate.py` clone Batteries from GitHub
  and run only with `AUTOFORM_REQUIRE_REAL_LIBRARY_TESTS=1`.
- The audit helper's Python 3.10 test skips without `python3.10` on `PATH`; CI's
  3.11 leg installs one (`AUTOFORM_REQUIRE_PYTHON310_TESTS=1`).
- Run `lake build` in `skills/setup/assets/cabannes-thesis-project` when you
  change the example's Lean sources or declarations.

Display code lives in `autoform_cli/ui/` (JS and CSS in `ui/assets/`), its tests in `tests/ui/`.
With Node 22.23.2, run `make build-explorer`, `make check-explorer`, and browser tests.
See [explorer ownership and failure boundaries](docs/explorer-design.md) before changing the client.

After editing the audit probe, run `python3 scripts/sync_audit_probe.py` and
check it: `elan run "$(cat tests/fixtures/library-project/lean-toolchain)" lean
autoform_cli/probes/audit_probe.lean` prints nothing when it is clean.

`tests/test_real_repl.py` skips unless `AUTOFORM_RUN_REAL_REPL_TESTS=1`. To run
it locally, build its fixture and install the oldest supported toolchain first:

```bash
(cd tests/fixtures/repl-smoke && lake build Mathlib Secret @repl/repl)
elan toolchain install "$(cat tests/fixtures/minimum-lean/lean-toolchain)"
AUTOFORM_RUN_REAL_REPL_TESTS=1 uv run pytest -q tests/test_real_repl.py
```

## Pull Requests
1. Fork the repo and create your branch from `main`.
2. Fill in the [pull request template](.github/pull_request_template.md).
3. If you haven't already, complete the Contributor License Agreement ("CLA").

## Reviews
A review finding blocks merge only with evidence (a reproduction, a failing
test, a CI run, or a differential against `main`) of a regression, data loss, a
leaked secret or private path, misleading docs or skill rules, a test that
cannot fail, or public surface that is undeclared or removed without
deprecation; #441's 14x slower audit blocked. Everything else (wording, naming,
a missing comment) is a note. After fixes, re-check only the blockers.

## Contributor License Agreement ("CLA")
Submit the [CLA](https://code.facebook.com/cla) once for Meta's open source projects.

## Issues
Use GitHub issues for public bugs, with clear reproduction instructions.
Disclose security bugs through Meta's [bounty program](https://bugbounty.meta.com/),
following its process, not a public issue.

## Coding Style
Python uses 4-space indentation and 120-character lines (`[tool.ruff]`), which
lint does not enforce: it runs only ruff's default rules. There is no formatter:
match the surrounding code and leave unrelated lines.

## License
Contributions are licensed under the root LICENSE file.
