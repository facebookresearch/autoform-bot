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
3.13 for every push and pull request. A separate job runs
`tests/test_skeleton.py` and one `tests/test_project_inspect.py` test against a
real Lean toolchain; another runs the inspection, bounded-subprocess, and
transactional-output tests on Windows. Locally, tests that need Lean skip when
`lake` is not on `PATH`. The `tests/test_skeleton.py` ones also skip when the
toolchain pinned in `tests/fixtures/skeleton-project/lean-toolchain` is
missing; the others let elan download it. Run `lake build` in
`skills/setup/assets/cabannes-thesis-project` when you change the example's
Lean sources or declarations.

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
