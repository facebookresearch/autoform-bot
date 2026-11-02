"""Reader-facing regressions reported by the Hartshorne wiki consumer."""
from pathlib import Path

from autoform_cli.__main__ import main
from autoform_cli.audit import audit_blueprint
from autoform_cli.markdown import article_prose_issues, site_converter
from autoform_cli.render import render_site


def blueprint(tmp_path: Path, text: str) -> Path:
    root = tmp_path / 'blueprint'
    roadmap = root / 'roadmap'
    roadmap.mkdir(parents=True)
    (roadmap / 'README.md').write_text('# Curves\n\nA mathematical chapter.\n', encoding='utf-8')
    (roadmap / 'valuation.md').write_text(text, encoding='utf-8')
    coverage = root / 'coverage'
    coverage.mkdir()
    (coverage / 'README.md').write_text(
        '# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n'
        '| Curves | OUT | No proof target in this rendering fixture |\n', encoding='utf-8',
    )
    return root


def test_hartshorne_program_syntax_and_issue_heading_are_advisory(tmp_path: Path, capsys) -> None:
    text = (
        '---\ndeclaration: def\n---\n\n# Valuation\n\n'
        'Let `X` be a curve.\n\n'
        '`valuationSubringAt P : ValuationSubring X.FunctionField`.\n\n'
        'Do not rely on open PR\n#43655.\n\n'
        '## Depends on\n\nNo prerequisites.\n'
    )
    root = blueprint(tmp_path, text)
    original = (root / 'roadmap' / 'valuation.md').read_bytes()

    assert main(['check', str(root)]) == 0
    output = capsys.readouterr().out
    assert 'line 7: mathematics-written-as-code' in output
    assert 'line 9: lean-in-mathematical-prose' in output
    assert 'line 12: numeric-prose-heading' in output
    result = audit_blueprint(root)
    assert result.clean
    assert {warning.code for warning in result.warnings} == {
        'mathematics-written-as-code', 'lean-in-mathematical-prose', 'numeric-prose-heading',
    }
    report = render_site(root, tmp_path / 'site-src')
    assert any('line 9: lean-in-mathematical-prose' in warning for warning in report.warnings)
    assert (root / 'roadmap' / 'valuation.md').read_bytes() == original


def test_zero_dimensional_variety_uses_math_and_metadata(tmp_path: Path) -> None:
    bad = '# Point\n\nIf `projDim Y = 0`, then `Y = {P}`.\n'
    assert {code for _, code, _ in article_prose_issues(bad)} == {
        'mathematics-written-as-code', 'lean-in-mathematical-prose',
    }
    repaired = (
        '---\nlean: Hartshorne.IsProjVariety.eq_singleton_of_projDim_eq_zero\n---\n\n'
        '# Point\n\nLet $Y$ be a projective variety. If $\\dim Y=0$, '
        'then $Y=\\{P\\}$ for a projective point $P$.\n'
    )
    assert article_prose_issues(repaired) == ()
    html = site_converter().convert(repaired.split('---', 2)[2])
    assert 'arithmatex' in html and '<code>' not in html


def test_diagnostics_respect_fences_comments_sources_and_unrelated_code() -> None:
    text = (
        '---\nlean: Project.theorem\n---\n\n# Mathematical text\n\n'
        'A precise statement with an unrelated code example `hello`.\n\n'
        '<!-- `Project.hidden`\n#43655.\n-->\n\n'
        '```lean\n#43655.\ndef Project.example : Nat := 0\n```\n\n'
        '## Sources\n\n- [`Project.source`](source.md)\n\n'
        '## Depends on\n\n- [`Project.prerequisite`](other.md)\n'
    )
    assert article_prose_issues(text) == ()


def test_source_number_reference_links_to_statement_without_renumbering(tmp_path: Path) -> None:
    root = blueprint(tmp_path, '---\ndeclaration: theorem\n---\n\n# Local rings distinguish points\n\nA precise theorem.\n\n## Depends on\n\nNo prerequisites.\n')
    chapter = root / 'roadmap' / 'README.md'
    unlinked = '# Curves\n\nLemma 6.4 distinguishes points.\n'
    assert article_prose_issues(unlinked)[0][1] == 'unlinked-source-number'
    chapter.write_text(
        "# Curves\n\nHartshorne's [Lemma I.6.4](valuation.md) distinguishes points.\n", encoding='utf-8',
    )
    assert article_prose_issues(chapter.read_text(encoding='utf-8')) == ()
    report = render_site(root, tmp_path / 'site-src')
    rendered = (report.output_dir / 'roadmap' / 'README.md').read_text(encoding='utf-8')
    assert 'id="valuation"' in rendered
    assert '[Lemma I.6.4](#valuation)' in rendered
    assert not any('unlinked-source-number' in warning for warning in report.warnings)
