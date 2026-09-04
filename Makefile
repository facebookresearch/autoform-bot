.PHONY: setup test test-deterministic test-daemon test-wheel test-real-lean lint check-example

THESIS_EXAMPLE := skills/setup/assets/cabannes-thesis-project
PYTEST := uv run pytest -q

setup:
	uv sync --extra dev --extra repl

test:
	$(PYTEST)

test-deterministic:
	$(PYTEST) -m "not daemon and not installed_wheel and not real_lean"

test-daemon:
	$(PYTEST) -m daemon

test-wheel:
	$(PYTEST) -m installed_wheel

test-real-lean:
	$(PYTEST) -m real_lean

lint:
	uv run ruff check autoform_cli servers tests

check-example:
	uv run autoform check $(THESIS_EXAMPLE)/blueprint --lean-root $(THESIS_EXAMPLE)
	uv run autoform-visualize $(THESIS_EXAMPLE)/blueprint
	uv run autoform render $(THESIS_EXAMPLE)/blueprint \
		--output $(THESIS_EXAMPLE)/site-src \
		--lean-root $(THESIS_EXAMPLE) \
		--require-declarations
	uv run --extra dev mkdocs build --strict --config-file $(THESIS_EXAMPLE)/mkdocs.yml
