from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest


_SHA256 = "9d38fe39237afdf673073fd6ebeb15f01514f033689edd56ba3b3251d611d7d3"
_CANONICAL_REPOSITORY = "facebookresearch/autoform-bot"
_DISPOSITIONS = {
    "COMPOSE",
    "CORE-ADAPT",
    "CORE-MERGE",
    "CORPUS-ADAPT",
    "EXCLUDE-INTERNAL",
    "EXCLUDE-PROJECT",
    "FORMALIZE-ADAPT",
    "FORMALIZE-MERGE",
    "EXTRACT-CONCEPT",
}
_REUSE_POLICIES = {"none", "concepts-only", "independent-reimplementation"}
_PLAN_ROW = re.compile(
    r"^\|\s*(?P<number>\d+)\s*\|\s*`(?P<name>[^`]+)`\s*\|"
    r"\s*(?P<disposition>[^|]+?)\s*\|"
)
_DELIVERY_ROW = re.compile(r"^[A-Z]\d{2}$")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _manifest(repo_root: Path) -> dict:
    return json.loads(
        (repo_root / "skills/archive-transport-manifest.json").read_text(
            encoding="utf-8"
        ),
        object_pairs_hook=_strict_object,
    )


def _plan_rows(repo_root: Path) -> list[tuple[int, str, str]]:
    rows = []
    plan = (repo_root / "ARCHIVE_SKILL_TRANSPORT_PLAN.md").read_text(
        encoding="utf-8"
    )
    for line in plan.splitlines():
        match = _PLAN_ROW.match(line)
        if match:
            rows.append(
                (
                    int(match.group("number")),
                    match.group("name"),
                    match.group("disposition").strip(),
                )
            )
    return rows


def _unquote(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value


def _cell_list(value: str) -> list[str]:
    value = value.strip()
    if value == "—":
        return []
    return [_unquote(item) for item in value.split(",")]


def _delivery_rows(repo_root: Path) -> list[dict[str, object]]:
    plan = (repo_root / "ARCHIVE_SKILL_TRANSPORT_PLAN.md").read_text(
        encoding="utf-8"
    )
    rows: list[dict[str, object]] = []
    for line in plan.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 9 or not _DELIVERY_ROW.fullmatch(cells[0]):
            continue
        rows.append(
            {
                "id": cells[0],
                "owner": _unquote(cells[1]),
                "target_repository": None if cells[2] == "—" else _unquote(cells[2]),
                "target_branch": _unquote(cells[3]),
                "stack_parent": None if cells[4] == "—" else _unquote(cells[4]),
                "depends_on": _cell_list(cells[6]),
                "approval_gates": _cell_list(cells[7]),
            }
        )
    return rows


def _assert_repository_skill_inventory(
    repo_root: Path, declared: list[dict[str, object]]
) -> None:
    declared_names = [skill["name"] for skill in declared]
    actual_names = sorted(
        path.parent.name for path in (repo_root / "skills").glob("*/SKILL.md")
    )
    assert declared_names == sorted(set(declared_names))
    assert actual_names == declared_names


def test_transport_manifest_has_one_unique_declared_policy_per_attested_skill(
    repo_root: Path,
) -> None:
    manifest = _manifest(repo_root)
    skills = manifest["skills"]
    names = [skill["name"] for skill in skills]

    assert set(manifest) == {
        "schema",
        "baseline",
        "archive",
        "authorization",
        "expected_disposition_counts",
        "owners",
        "repository_skills",
        "delivery_units",
        "skills",
    }
    assert manifest["schema"] == "autoform-skill-transport/v2"
    assert manifest["archive"] == {
        "filename": "math_lean_skills_agent_config_2026-08-31.zip",
        "sha256": _SHA256,
        "evidence_mode": "external_attestation",
        "attested_entry_count": 335,
        "attested_skill_count": 51,
        "license_filename_candidates_found": [],
        "attestation": {
            "date": "2026-08-31",
            "method": (
                "authenticated download, SHA-256 comparison, and ZIP "
                "central-directory review"
            ),
            "repository_regeneration_available": False,
            "limitation": (
                "the archive object locator and generated member inventory "
                "are not available in this repository"
            ),
        },
    }
    assert len(skills) == manifest["archive"]["attested_skill_count"] == 51
    assert len(names) == len(set(names))
    assert names == sorted(names)

    for skill in skills:
        assert set(skill) == {
            "name",
            "disposition",
            "target_layer",
            "owner",
            "reuse",
        }
        assert skill["disposition"] in _DISPOSITIONS
        assert skill["reuse"] in _REUSE_POLICIES
        assert skill["target_layer"]
        assert skill["owner"]


def test_transport_plan_and_manifest_are_the_same_policy(repo_root: Path) -> None:
    manifest = _manifest(repo_root)
    rows = _plan_rows(repo_root)
    manifest_policy = {
        skill["name"]: skill["disposition"] for skill in manifest["skills"]
    }

    assert [number for number, _, _ in rows] == list(range(1, 52))
    assert len({name for _, name, _ in rows}) == 51
    assert {name: disposition for _, name, disposition in rows} == manifest_policy

    counts = Counter(manifest_policy.values())
    assert dict(sorted(counts.items())) == manifest["expected_disposition_counts"]


def test_transport_manifest_fails_closed_on_reuse(repo_root: Path) -> None:
    manifest = _manifest(repo_root)
    authorization = manifest["authorization"]

    assert authorization == {
        "archive_access": "user_requested",
        "verbatim_reuse": "not_established",
        "default_implementation_policy": "independent_reimplementation",
        "archive_distribution": "prohibited",
    }

    for skill in manifest["skills"]:
        if skill["disposition"].startswith("EXCLUDE") or skill["disposition"] == "COMPOSE":
            assert skill["reuse"] == "none"
        assert skill["reuse"] != "verbatim"


def test_transport_manifest_rejects_duplicate_authority_keys(tmp_path: Path) -> None:
    manifest = tmp_path / "skills" / "archive-transport-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        '{"schema":"autoform-skill-transport/v2",'
        '"authorization":{"verbatim_reuse":"established",'
        '"verbatim_reuse":"not_established"}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key 'verbatim_reuse'"):
        _manifest(tmp_path)


def test_adaptations_have_one_valid_destination(repo_root: Path) -> None:
    manifest = _manifest(repo_root)
    expected_layers = {
        "CORE-ADAPT": "main",
        "CORE-MERGE": "main",
        "CORPUS-ADAPT": "autoform-corpus",
        "FORMALIZE-ADAPT": "formalize",
        "FORMALIZE-MERGE": "formalize",
    }

    for skill in manifest["skills"]:
        expected = expected_layers.get(skill["disposition"])
        if expected is None:
            continue
        assert skill["target_layer"] == expected
        assert skill["reuse"] in {
            "concepts-only",
            "independent-reimplementation",
        }
        assert skill["owner"] not in {"", "unassigned"}


def test_every_decision_and_delivery_unit_has_a_registered_owner(
    repo_root: Path,
) -> None:
    manifest = _manifest(repo_root)
    owners = manifest["owners"]

    assert owners
    for owner_id, layer in owners.items():
        assert owner_id
        assert layer in {
            "main",
            "formalize",
            "autoform-corpus",
            "external",
            "provider-interface",
            "internal",
            "project",
        }

    for skill in manifest["skills"]:
        assert skill["owner"] in owners
        assert owners[skill["owner"]] == skill["target_layer"]

    for unit in manifest["delivery_units"]:
        assert unit["owner"] in owners


def test_delivery_graph_is_closed_acyclic_and_topologically_ordered(
    repo_root: Path,
) -> None:
    manifest = _manifest(repo_root)
    units = manifest["delivery_units"]
    ids = [unit["id"] for unit in units]
    positions = {unit_id: position for position, unit_id in enumerate(ids)}

    assert len(ids) == len(set(ids))
    assert ids[0] == "P00"
    for unit in units:
        assert set(unit) == {
            "id",
            "owner",
            "target_repository",
            "target_branch",
            "stack_parent",
            "depends_on",
            "approval_gates",
            "source_skills",
            "state",
        }
        assert len(unit["depends_on"]) == len(set(unit["depends_on"]))
        assert len(unit["approval_gates"]) == len(set(unit["approval_gates"]))
        assert len(unit["source_skills"]) == len(set(unit["source_skills"]))
        assert set(unit["approval_gates"]) <= {
            "specialist-scope-approved",
            "companion-repository-approved",
        }
        assert unit["state"] in {
            "this-policy",
            "planned",
            "satisfied-by-main-formalize",
        }
        for dependency in unit["depends_on"]:
            assert dependency in positions
            assert positions[dependency] < positions[unit["id"]]
        assert unit["target_branch"] == "main"
        if unit["id"].startswith("C"):
            assert unit["target_repository"] is None
            assert "companion-repository-approved" in unit["approval_gates"]
        else:
            assert unit["target_repository"] == _CANONICAL_REPOSITORY
        if unit["stack_parent"] is not None:
            assert unit["stack_parent"] in unit["depends_on"]
            parent = units[positions[unit["stack_parent"]]]
            assert unit["target_repository"] == parent["target_repository"]
            assert unit["target_branch"] == parent["target_branch"]

    by_id = {unit["id"]: unit for unit in units}
    assert by_id["P00"]["state"] == "this-policy"
    assert by_id["E01"]["state"] == "satisfied-by-main-formalize"
    assert by_id["E01"]["depends_on"] == []
    assert by_id["C00"]["depends_on"] == ["P03", "P05", "P06"]
    assert by_id["D02"]["depends_on"] == ["C00"]
    for unit_id in ("E02", "E03", "E04", "E05", "E06"):
        assert by_id[unit_id]["approval_gates"] == ["specialist-scope-approved"]
    for unit_id in ("C00", "C01", "C02", "C03", "C04", "C05", "D02"):
        assert by_id[unit_id]["approval_gates"] == [
            "companion-repository-approved"
        ]
    assert all(
        unit["state"] == "planned"
        for unit in units
        if unit["id"] not in {"P00", "E01"}
    )


def test_every_adapted_source_skill_has_exactly_one_delivery_unit(
    repo_root: Path,
) -> None:
    manifest = _manifest(repo_root)
    adapted_dispositions = {
        "CORE-ADAPT",
        "CORE-MERGE",
        "CORPUS-ADAPT",
        "FORMALIZE-ADAPT",
        "FORMALIZE-MERGE",
    }
    expected = {
        skill["name"]
        for skill in manifest["skills"]
        if skill["disposition"] in adapted_dispositions
    }
    assigned = [
        name
        for unit in manifest["delivery_units"]
        for name in unit["source_skills"]
    ]

    assert set(assigned) == expected
    assert all(count == 1 for count in Counter(assigned).values())

    skills_by_name = {skill["name"]: skill for skill in manifest["skills"]}
    for unit in manifest["delivery_units"]:
        for name in unit["source_skills"]:
            assert skills_by_name[name]["owner"] == unit["owner"]


def test_repository_skill_inventory_is_complete_and_classified(
    repo_root: Path,
) -> None:
    manifest = _manifest(repo_root)
    declared = manifest["repository_skills"]
    _assert_repository_skill_inventory(repo_root, declared)


def test_delivery_plan_and_manifest_have_identical_machine_fields(
    repo_root: Path,
) -> None:
    manifest = _manifest(repo_root)
    manifest_rows = [
        {
            "id": unit["id"],
            "owner": unit["owner"],
            "target_repository": unit["target_repository"],
            "target_branch": unit["target_branch"],
            "stack_parent": unit["stack_parent"],
            "depends_on": unit["depends_on"],
            "approval_gates": unit["approval_gates"],
        }
        for unit in manifest["delivery_units"]
    ]
    assert _delivery_rows(repo_root) == manifest_rows


def test_manifest_records_the_reviewed_repository_baseline(repo_root: Path) -> None:
    manifest = _manifest(repo_root)
    baseline = manifest["baseline"]

    assert set(baseline) == {
        "canonical_repository",
        "main_commit",
        "landed_capabilities",
    }
    assert baseline["canonical_repository"] == _CANONICAL_REPOSITORY
    assert re.fullmatch(r"[0-9a-f]{40}", baseline["main_commit"])
    assert baseline["main_commit"] == "7fa6d1d6dcda161d5575a588bb723271ac58467c"
    assert baseline["landed_capabilities"] == {
        "skeleton": "#12",
        "readback_faithfulness": "#53",
        "markdown_formalize": "#92",
        "revision_impact": "#136",
        "open_statements": "#138",
        "project_creation": "#96",
        "formalized_work_load_invariants": "#158",
    }

    source_names = {skill["name"] for skill in manifest["skills"]}
    for skill in manifest["repository_skills"]:
        assert set(skill) == {"name", "origin", "archive_sources"}
        assert skill["origin"] in {"native", "hybrid", "transported"}
        assert set(skill["archive_sources"]) <= source_names
        if skill["origin"] == "native":
            assert skill["archive_sources"] == []
        else:
            assert skill["archive_sources"]


def test_quality_goal_documents_evidence_selection_and_default_deny(
    repo_root: Path,
) -> None:
    quality = (repo_root / "FORMALIZATION_QUALITY_GOAL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(quality.split())

    for completion_field in (
        "`statement: formalized`",
        "`proof: formalized`",
        "`mathlib: true`",
        "`lean:`",
        "`mathlib_declaration:`",
        "`mathlib_file:`",
    ):
        assert completion_field in quality
    assert "independently of `declaration`" in normalized
    assert "`declaration` alone as planning" in normalized
    assert "container carries any formalization evidence" in normalized

    applicability = quality.split("Applicability is default-deny:", 1)[1].split(
        "### 3.", 1
    )[0]
    matrix: dict[str, str] = {}
    for line in applicability.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] in {
            "source-fidelity",
            "clause-coverage",
            "definition-fidelity",
            "boundary-probes",
            "lean-validity",
            "proof-integrity",
            "provenance",
        }:
            matrix[cells[0]] = cells[1]
    for gate in (
        "source-fidelity",
        "clause-coverage",
        "definition-fidelity",
        "boundary-probes",
        "lean-validity",
        "proof-integrity",
        "provenance",
    ):
        assert gate in matrix
    assert "`origin: background` or `origin: bridged`" in matrix["source-fidelity"]
    assert "`origin: cited` requires `passed`" in matrix["source-fidelity"]
    assert "omitted origin is invalid" in matrix["source-fidelity"]
    for gate in (
        "clause-coverage",
        "definition-fidelity",
        "boundary-probes",
        "lean-validity",
        "provenance",
    ):
        assert matrix[gate].startswith("Never.")
    assert matrix["proof-integrity"].startswith(
        "Only when no completed proof is claimed."
    )
    assert "proof-bearing `mathlib: true`" in matrix["proof-integrity"]
    assert "Missing `origin` never grants an exemption" in applicability
    assert "Any case not explicitly allowed" in applicability
    assert "Every quality subject must declare `origin` explicitly" in normalized
    assert "`missing-quality-origin`" in normalized
    assert "`DeclarationSkeleton.kind` values" in normalized
    assert "never the authored `declaration` label" in normalized
    assert "any theorem or axiom makes the subject proof-bearing" in normalized
    assert "only an all-definition-like set" in normalized
    assert "A mixed set is proof-bearing" in normalized
    assert "quality-target-kind-mismatch" in normalized


def test_quality_goal_documents_current_compiled_and_review_evidence(
    repo_root: Path,
) -> None:
    quality = (repo_root / "FORMALIZATION_QUALITY_GOAL.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "autoform-readback-faithfulness-verdict/v1",
        "article review hash",
        "`agrees`",
        "skeleton freshness and environment probe",
        "a lexical source hit never",
        "unverified-lean-validity",
        "unresolved-quality-declaration",
        "missing-quality-origin",
        "quality-target-kind-mismatch",
        "missing-quality-passage",
        "definitely_not_a_tactic",
        "stale `.olean`",
        "raw read-back Markdown file",
        "`auditor=<human|model>:<local-id>`",
        "`judge=<human|model>:<local-id>`",
        "pairwise distinct",
    ):
        assert required in quality
    p03_contract = quality.split("- P03 orders generated verification as", 1)[1].split(
        "\n\n", 1
    )[0]
    assert p03_contract.index("`lake build`") < p03_contract.index(
        "`autoform quality`"
    ) < p03_contract.index("integrity audit")


def test_quality_goal_documents_all_hosts_and_merged_owners(
    repo_root: Path,
) -> None:
    quality = (repo_root / "FORMALIZATION_QUALITY_GOAL.md").read_text(
        encoding="utf-8"
    )

    assert "Codex, Claude Code, and Muse" in quality
    assert "`.muse-plugin/plugin.json`" in quality
    for owner_boundary in (
        "P01 adds the gate and handoff contract",
        "P02 owns visible quality-table parsing",
        "P03 orders generated verification",
        "readback-faithfulness.md",
        "Pages must not publish it",
        "authorized frozen source passages",
    ):
        assert owner_boundary in quality

    for policy_case in (
        "Omitted origin, source fidelity passed with otherwise current evidence",
        "Mathlib theorem mislabeled as a definition",
        "Mathlib definition mislabeled as a theorem",
        "Mixed Mathlib definition and theorem roots, proof integrity N/A",
        "All-definition Mathlib roots, proof integrity N/A",
        "Unresolved or unsupported compiled declaration kind",
    ):
        assert policy_case in quality


def test_release_gates_use_only_resolved_commands(repo_root: Path) -> None:
    plan = (repo_root / "ARCHIVE_SKILL_TRANSPORT_PLAN.md").read_text(
        encoding="utf-8"
    )

    assert "PLUGIN_CREATOR_ROOT" not in plan
    assert not re.search(r"<[A-Z][A-Z0-9_]+>", plan)
    assert (
        "uv run pytest -q tests/test_plugin_runtime.py "
        "tests/test_skill_examples.py"
    ) in plan
    assert "claude plugin validate . --strict" in plan
    assert "muse plugins validate . --json" in plan


def test_goal_prompt_uses_the_planned_pr_boundaries(repo_root: Path) -> None:
    goal = (repo_root / "ARCHIVE_SKILL_TRANSPORT_GOAL.md").read_text(
        encoding="utf-8"
    )
    plan = (repo_root / "ARCHIVE_SKILL_TRANSPORT_PLAN.md").read_text(
        encoding="utf-8"
    )

    assert "The first eligible unit is P00" in goal
    assert "Do not\ncombine the program into one implementation branch" in goal
    assert "The present planning task\ndoes not itself create branches" in plan
    for pr_id in (unit["id"] for unit in _manifest(repo_root)["delivery_units"]):
        assert f"| {pr_id} |" in plan
