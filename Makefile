.PHONY: setup test test-browser lint check-example

THESIS_EXAMPLE := skills/setup/assets/cabannes-thesis-project

setup:
	uv sync --extra dev --extra repl

test:
	uv run pytest -q

test-browser:
	uv sync --extra dev --extra browser
	uv run --extra dev --extra browser playwright install webkit
	AUTOFORM_REQUIRE_BROWSER_TESTS=1 uv run --extra dev --extra browser pytest -q tests/test_dag_viewer_browser.py tests/test_continuous_explorer_browser.py

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
