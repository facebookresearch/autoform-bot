from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


_SHA256 = "9d38fe39237afdf673073fd6ebeb15f01514f033689edd56ba3b3251d611d7d3"
_DISPOSITIONS = {
    "COMPOSE",
    "CORE-ADAPT",
    "CORE-MERGE",
    "CORPUS-ADAPT",
    "EXCLUDE-INTERNAL",
    "EXCLUDE-PROJECT",
    "EXEC-ADAPT",
    "EXEC-MERGE",
    "EXTRACT-CONCEPT",
}
_REUSE_POLICIES = {"none", "concepts-only", "independent-reimplementation"}
_PLAN_ROW = re.compile(
    r"^\|\s*(?P<number>\d+)\s*\|\s*`(?P<name>[^`]+)`\s*\|"
    r"\s*(?P<disposition>[^|]+?)\s*\|"
)


def _manifest(repo_root: Path) -> dict:
    return json.loads(
        (repo_root / "skills/archive-transport-manifest.json").read_text(
            encoding="utf-8"
        )
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


def test_transport_manifest_has_complete_unique_inventory(repo_root: Path) -> None:
    manifest = _manifest(repo_root)
    skills = manifest["skills"]
    names = [skill["name"] for skill in skills]

    assert manifest["schema"] == "autoform-skill-transport/v1"
    assert manifest["archive"] == {
        "filename": "math_lean_skills_agent_config_2026-08-31.zip",
        "sha256": _SHA256,
        "entry_count": 335,
        "skill_count": 51,
        "license_files_found": [],
    }
    assert len(skills) == manifest["archive"]["skill_count"] == 51
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


def test_adaptations_have_one_valid_destination(repo_root: Path) -> None:
    manifest = _manifest(repo_root)
    expected_layers = {
        "CORE-ADAPT": "main",
        "CORE-MERGE": "main",
        "CORPUS-ADAPT": "autoform-corpus",
        "EXEC-ADAPT": "execution",
        "EXEC-MERGE": "execution",
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
    for pr_id in (
        "P00",
        "P01",
        "P02",
        "P03",
        "P04",
        "P05",
        "P06",
        "E01",
        "E02",
        "E03",
        "E04",
        "E05",
        "E06",
        "C00",
        "C01",
        "C02",
        "C03",
        "C04",
        "C05",
        "D01",
    ):
        assert f"| {pr_id} |" in plan
