from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autoform_cli.coverage import COVERAGE_V2_SCHEMA, load_coverage


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_FIRST_TWO_HASH = _digest(b"First line.\nSecond line.\n")
_SECOND_HASH = _digest(b"Second line.\n")
_SECOND_AND_APPENDIX_HASH = _digest(b"Second line.\nAppendix.\n")
_APPENDIX_HASH = _digest(b"Appendix.\n")


def _article(
    blueprint: Path,
    relative: str,
    *,
    declaration: str | None = None,
    source_units: tuple[str, ...] = (),
) -> Path:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = []
    if declaration is not None:
        metadata.append(f"declaration: {declaration}")
    if source_units:
        metadata.append(f"source_units: [{', '.join(source_units)}]")
    title = path.parent.name.title() if path.name == "README.md" else path.stem.title()
    path.write_text(
        "---\n" + "\n".join(metadata) + "\n---\n\n" + f"# {title}\n\nA statement.\n",
        encoding="utf-8",
    )
    return path


def _project(tmp_path: Path) -> tuple[Path, bytes]:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "README.md")
    _article(blueprint, "chapter/README.md")
    _article(
        blueprint,
        "chapter/result.md",
        declaration="theorem",
        source_units=("opening",),
    )
    artifact = b"First line.\nSecond line.\nAppendix.\n"
    source = blueprint / "sources" / "nested" / "book.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(artifact)
    return blueprint, artifact


def _contract(
    blueprint: Path,
    artifact: bytes,
    *,
    rows: str | None = None,
    artifact_path: str = "sources/nested/book.txt",
    artifact_hash: str | None = None,
    schema: str = COVERAGE_V2_SCHEMA,
) -> Path:
    if rows is None:
        rows = (
            f"| opening | Opening | 1-2 | §1 | {_FIRST_TWO_HASH} | "
            "DECOMPOSED | [Result](../roadmap/chapter/result.md) |\n"
            f"| appendix | Appendix | 3-3 | back matter | {_APPENDIX_HASH} | "
            "OUT | Bibliography and index are outside the formal scope |\n"
        )
    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"schema: {schema}\n"
        f"artifact: {artifact_path}\n"
        f"artifact_sha256: {artifact_hash or _digest(artifact)}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"{rows}",
        encoding="utf-8",
    )
    return path


def test_loads_exhaustive_v2_contract_and_reciprocal_bindings(tmp_path: Path) -> None:
    blueprint, artifact = _project(tmp_path)
    contract = _contract(blueprint, artifact)

    first, issues = load_coverage(blueprint)
    second, repeated_issues = load_coverage(blueprint)

    assert issues == repeated_issues == ()
    assert first == second
    assert first is not None
    assert first.schema == COVERAGE_V2_SCHEMA
    assert first.complete
    assert first.artifact_path == "sources/nested/book.txt"
    assert first.artifact_sha256 == _digest(artifact)
    assert first.source_sha256 == _digest(contract.read_bytes())
    assert [(unit.unit, unit.start_line, unit.end_line) for unit in first.units] == [
        ("opening", 1, 2),
        ("appendix", 3, 3),
    ]
    assert first.units[0].roadmap_nodes == ("chapter/result",)
    assert [(binding.unit, binding.node_id) for binding in first.node_bindings] == [
        ("opening", "chapter/result")
    ]
    assert first.to_json() == second.to_json()


@pytest.mark.parametrize(
    ("artifact", "code"),
    [
        (b"", "coverage-artifact-empty"),
        (b"\xef\xbb\xbftext\n", "coverage-artifact-bom"),
        (b"text\x00\n", "coverage-artifact-nul"),
        (b"text\r\n", "coverage-artifact-cr"),
        (b"text", "coverage-artifact-final-lf"),
        (b"\xff\n", "coverage-artifact-utf8"),
    ],
)
def test_rejects_noncanonical_source_artifacts(
    tmp_path: Path, artifact: bytes, code: str
) -> None:
    blueprint, _ = _project(tmp_path)
    (blueprint / "sources/nested/book.txt").write_bytes(artifact)
    _contract(blueprint, artifact)

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.code for issue in issues] == [code]


def test_rejects_stale_hashes_gaps_overlap_and_bounds(tmp_path: Path) -> None:
    blueprint, artifact = _project(tmp_path)
    rows = (
        f"| opening | Opening | 2-2 | §1 | {_SECOND_HASH} | OUT | Excluded by scope |\n"
        f"| appendix | Appendix | 2-4 | appendix | {_SECOND_AND_APPENDIX_HASH} | OUT | Excluded by scope |\n"
    )
    _contract(blueprint, artifact, rows=rows)

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert {issue.code for issue in issues} == {
        "coverage-unit-gap",
        "coverage-unit-overlap",
        "coverage-unit-bounds",
    }


def test_rejects_stale_artifact_and_unit_hashes_separately(tmp_path: Path) -> None:
    blueprint, artifact = _project(tmp_path)
    _contract(blueprint, artifact, artifact_hash="0" * 64)
    assert [issue.code for issue in load_coverage(blueprint)[1]] == [
        "coverage-artifact-hash-stale"
    ]

    rows = (
        f"| opening | Opening | 1-2 | §1 | {'0' * 64} | OUT | Excluded by scope |\n"
        f"| appendix | Appendix | 3-3 | appendix | {_APPENDIX_HASH} | OUT | Excluded by scope |\n"
    )
    _contract(blueprint, artifact, rows=rows)
    assert [issue.code for issue in load_coverage(blueprint)[1]] == [
        "coverage-unit-hash-stale"
    ]


def test_unknown_and_duplicate_schemas_never_fall_back_to_v1(tmp_path: Path) -> None:
    blueprint, artifact = _project(tmp_path)
    contract = _contract(blueprint, artifact, schema="autoform-coverage/v9")
    assert [issue.code for issue in load_coverage(blueprint)[1]] == [
        "coverage-schema-unknown"
    ]

    text = contract.read_text(encoding="utf-8")
    contract.write_text(
        text.replace(
            "schema: autoform-coverage/v9\n",
            "schema: autoform-coverage/v2\nschema: autoform-coverage/v1\n",
        ),
        encoding="utf-8",
    )
    assert {issue.code for issue in load_coverage(blueprint)[1]} == {"coverage-schema-mixed"}


@pytest.mark.parametrize(
    ("frontmatter", "expected_code"),
    [
        (
            "---\nschema: autoform-coverage/v2\nartifact: sources/nested/book.txt\n",
            "coverage-frontmatter-invalid",
        ),
        ("---\nschema autoform-coverage/v2\n---\n", "coverage-frontmatter-invalid"),
        ("----\nscema: autoform-coverage/v2\n---\n", "coverage-schema-ambiguous"),
        (
            "----\n# intended frontmatter\nschema: autoform-coverage/v2\n---\n",
            "coverage-schema-ambiguous",
        ),
        ("----\nschema: autoform-coverage/v2\n---\n", "coverage-schema-ambiguous"),
        ("\ufeff---\nschema: autoform-coverage/v2\n---\n", "coverage-schema-ambiguous"),
    ],
)
def test_malformed_v2_frontmatter_cannot_downgrade_to_v1(
    tmp_path: Path, frontmatter: str, expected_code: str
) -> None:
    blueprint, _ = _project(tmp_path)
    contract = blueprint / "coverage/README.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        frontmatter
        + "\n# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Whole source | OUT | Explicitly outside scope |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.code for issue in issues] == [expected_code]


@pytest.mark.parametrize(
    ("selector", "expected_code"),
    [
        ('"schema": autoform-coverage/v2', "coverage-schema-ambiguous"),
        ("'schema': 'autoform-coverage/v2'", "coverage-schema-ambiguous"),
        ('"schema": "autoform-coverage\\u002fv2"', "coverage-schema-ambiguous"),
        ('"\\u0073chema": "autoform-coverage\\u002fv2"', "coverage-schema-ambiguous"),
        (
            '"\\U00000073chema": "autoform-coverage\\U0000002Fv2"',
            "coverage-schema-ambiguous",
        ),
        ('"\\x73chema": "autoform-coverage\\x2Fv2"', "coverage-schema-ambiguous"),
        ("schema = autoform-coverage/v2", "coverage-frontmatter-invalid"),
        ("schema=\"autoform-coverage/v2\"", "coverage-frontmatter-invalid"),
        ("scema autoform-coverage/v2", "coverage-frontmatter-invalid"),
        ("scema: autoform-coverage/v2", "coverage-schema-ambiguous"),
        ("Schema: AUTOFORM-COVERAGE/v2", "coverage-schema-ambiguous"),
        ('"schema" = "autoform-coverage\\/v2"', "coverage-frontmatter-invalid"),
        ('"scehma": autoform_coverage/v02', "coverage-schema-ambiguous"),
    ],
)
def test_valid_frontmatter_with_malformed_v2_intent_cannot_downgrade(
    tmp_path: Path, selector: str, expected_code: str
) -> None:
    blueprint, _ = _project(tmp_path)
    contract = blueprint / "coverage/README.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        f"---\ntitle: Coverage\n{selector}\n---\n\n"
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Whole source | OUT | Explicitly outside scope |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.code for issue in issues] == [expected_code]


def test_v2_schema_token_in_frontmatter_comment_or_body_prose_is_not_intent(
    tmp_path: Path,
) -> None:
    blueprint, _ = _project(tmp_path)
    contract = blueprint / "coverage/README.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        "---\n"
        "title: Coverage\n"
        "# schema: autoform-coverage/v2\n"
        "---\n\n"
        "# Coverage\n\n"
        'Migration prose may quote `"schema": autoform-coverage/v2`.\n\n'
        "Compatibility: autoform-coverage/v2\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Whole source | OUT | Explicitly outside scope |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.schema == "autoform-coverage/v1"


@pytest.mark.parametrize(
    "opening",
    [
        "--",
        "-- yaml",
        "---yaml",
        "--- yaml",
        "---decorated",
        "--- # coverage metadata",
    ],
)
def test_malformed_opening_fence_with_v2_intent_cannot_downgrade(
    tmp_path: Path, opening: str
) -> None:
    blueprint, _ = _project(tmp_path)
    contract = blueprint / "coverage/README.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        f"{opening}\n"
        '"schema": "autoform-coverage\\u002fv2"\n'
        "---\n\n"
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Whole source | OUT | Explicitly outside scope |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.code for issue in issues] == ["coverage-schema-ambiguous"]


def test_two_hyphen_v1_prose_and_v2_example_do_not_select_schema(tmp_path: Path) -> None:
    blueprint, _ = _project(tmp_path)
    contract = blueprint / "coverage/README.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        "-- This is prose, not a frontmatter fence.\n\n"
        "# Coverage\n\n"
        "```yaml\n"
        '"schema": "autoform-coverage\\u002fv2"\n'
        "```\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Whole source | OUT | Explicitly outside scope |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.schema == "autoform-coverage/v1"


def test_v2_schema_example_inside_code_fence_does_not_select_v2(tmp_path: Path) -> None:
    blueprint, _ = _project(tmp_path)
    contract = blueprint / "coverage/README.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        "# Coverage\n\n"
        "```yaml\n"
        "schema: autoform-coverage/v2\n"
        "```\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Whole source | OUT | Explicitly outside scope |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.schema == "autoform-coverage/v1"


def test_v2_table_without_schema_and_mixed_rendered_tables_fail_closed(tmp_path: Path) -> None:
    blueprint, artifact = _project(tmp_path)
    contract = _contract(blueprint, artifact)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "---\nschema: autoform-coverage/v2\n"
            "artifact: sources/nested/book.txt\n"
            f"artifact_sha256: {_digest(artifact)}\n---\n\n",
            "",
        ),
        encoding="utf-8",
    )
    assert [issue.code for issue in load_coverage(blueprint)[1]] == [
        "coverage-v2-schema-required"
    ]

    _contract(blueprint, artifact)
    contract.write_text(
        contract.read_text(encoding="utf-8")
        + "\n| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Legacy | OUT | Legacy scope |\n",
        encoding="utf-8",
    )
    assert [issue.code for issue in load_coverage(blueprint)[1]] == [
        "coverage-schema-mixed"
    ]


def test_rejects_one_way_unknown_and_nonleaf_bindings(tmp_path: Path) -> None:
    blueprint, artifact = _project(tmp_path)
    result = blueprint / "roadmap/chapter/result.md"
    result.write_text(result.read_text(encoding="utf-8").replace("[opening]", "[other]"), encoding="utf-8")
    _contract(blueprint, artifact)

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert {issue.code for issue in issues} == {
        "coverage-node-binding-unknown-unit",
        "coverage-node-binding-missing-reciprocal",
    }

    result.write_text(result.read_text(encoding="utf-8").replace("[other]", "[opening]"), encoding="utf-8")
    rows = (
        f"| opening | Opening | 1-2 | §1 | {_FIRST_TWO_HASH} | "
        "DECOMPOSED | [Chapter](../roadmap/chapter/README.md) |\n"
        f"| appendix | Appendix | 3-3 | appendix | {_APPENDIX_HASH} | OUT | Excluded by scope |\n"
    )
    _contract(blueprint, artifact, rows=rows)
    assert "coverage-decomposed-target-not-leaf" in {
        issue.code for issue in load_coverage(blueprint)[1]
    }


def test_rejects_source_artifact_symlinks_and_escaping_paths(tmp_path: Path) -> None:
    blueprint, artifact = _project(tmp_path)
    external = tmp_path / "external.txt"
    external.write_bytes(artifact)
    source = blueprint / "sources/nested/book.txt"
    source.unlink()
    source.symlink_to(external)
    _contract(blueprint, artifact)
    assert [issue.code for issue in load_coverage(blueprint)[1]] == [
        "coverage-artifact-symlink"
    ]

    _contract(blueprint, artifact, artifact_path="sources/../external.txt")
    assert [issue.code for issue in load_coverage(blueprint)[1]] == [
        "coverage-artifact-path-invalid"
    ]
