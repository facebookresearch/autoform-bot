"""Scratch check: typeset ATLAS articles' formulas as the site does, before and after a fix.

    python atlas_typeset.py MATHJAX_PACKAGE BEFORE AFTER

BEFORE and AFTER are vault directories: the articles, and tex-macros.json when
there is one. Each article is converted with the site's Markdown converter into
a page of a small site, beside the javascripts/mathjax.js render writes from
that tex-macros.json, and site check typesets every formula with MathJax.
Exits 0 when BEFORE has formula warnings and AFTER has none.
"""

import sys
import tempfile
from pathlib import Path

from autoform_cli.markdown import site_converter
from autoform_cli.tex_macros import TEX_MACROS
from autoform_cli.ui.mathjax import MATHJAX_SCRIPT, mathjax_script
from autoform_cli.ui.site_check import check_site

FORMULA_ISSUES = ("MathJax cannot typeset", "is undefined, so the site shows it in red")


def formula_issues(vault: Path, mathjax: Path, show: str | None = None) -> list[str]:
    macros = vault / TEX_MACROS
    script, warnings = mathjax_script(None, macros.read_bytes() if macros.is_file() else None)
    for warning in warnings:
        print(f"  mathjax.js: {warning}")
    with tempfile.TemporaryDirectory() as scratch:
        site = Path(scratch, "site")
        (site / MATHJAX_SCRIPT).parent.mkdir(parents=True)
        (site / MATHJAX_SCRIPT).write_text(script, encoding="utf-8")
        articles = sorted(vault.rglob("*.md"))
        for article in articles:
            html = site_converter().convert(article.read_text(encoding="utf-8"))
            if article.name == show:
                print(html)
            page = site / article.relative_to(vault).with_suffix("") / "index.html"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(f"<!doctype html>\n<html><body><article>\n{html}\n</article></body></html>\n", "utf-8")
        report = check_site(site, None, mathjax)
    print(f"  {len(articles)} articles, {report.pages} pages")
    found = [issue for issue in report.issues if any(mark in issue for mark in FORMULA_ISSUES)]
    for issue in report.issues:
        print(f"  {'FORMULA' if issue in found else 'other'}: {issue}")
    return found


def main() -> int:
    mathjax, before, after = map(Path, sys.argv[1:4])
    print("== before")
    old = formula_issues(before, mathjax)
    print("== after")
    new = formula_issues(after, mathjax, show="normal-tree-at-a-cardinal.md")
    print(f"== formula warnings: before {len(old)}, after {len(new)}")
    return 0 if old and not new else 1


if __name__ == "__main__":
    sys.exit(main())
