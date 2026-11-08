"""What people read: the site render writes, the explorer, the project map, the dashboard, visualize and site check.

No other autoform_cli module imports this package, except
autoform_cli/__main__.py inside the render, site check and dashboard handlers,
and inside the check handler for publication_issues alone
(tests/test_ui_boundary.py); modules here may import any autoform_cli name,
but not each other in a cycle.
check prints render's publication_issues, so a change here to what render
writes or captures, how it rewrites links, which pages it checks, or its
MathJax warnings changes check's documented output: update "Validation" in
autoform_cli/reference/validation-and-audit.md and the published-site page,
autoform_cli/reference/site.md, with it. The tests are in tests/ui/.
"""
