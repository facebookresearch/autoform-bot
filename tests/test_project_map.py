"""The whole-project map's per-level data, built from search-index records and typed relations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import project_map
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode


def _scope(node_id: str, parent: str | None, title: str, **extra: object) -> dict[str, object]:
    return {"id": node_id, "kind": "scope", "title": title, "parent": parent, "result": False, **extra}


def _result(node_id: str, parent: str, title: str, status: str = "fully_proved", **extra: object) -> dict[str, object]:
    return {"id": node_id, "kind": "node", "title": title, "parent": parent, "result": True, "status": status, **extra}


def _library() -> tuple[list[dict[str, object]], list[tuple[str, str, int, int]]]:
    records = [
        _scope("roadmap", None, "Library"),
        _scope("Algebra", "roadmap", "Algebra", area="Algebra & Number Theory"),
        _scope("Algebra/MathlibExt", "Algebra", "MathlibExt", navigation="collection"),
        _scope("Algebra/MathlibExt/part-01", "Algebra/MathlibExt", "Groups to Rings", navigation="transparent"),
        _scope("Algebra/MathlibExt/part-01/groups", "Algebra/MathlibExt/part-01", "Groups", navigation="module"),
        _result("Algebra/MathlibExt/part-01/groups/lagrange", "Algebra/MathlibExt/part-01/groups", "Lagrange", lean="Subgroup.card_dvd"),
        _result("Algebra/MathlibExt/part-01/groups/cauchy", "Algebra/MathlibExt/part-01/groups", "Cauchy", status="can_prove"),
        _scope("NumberTheory", "roadmap", "Number Theory", area="Algebra & Number Theory"),
        _result("NumberTheory/euclid", "NumberTheory", "Euclid", status="fully_proved"),
        _scope("Analysis", "roadmap", "Analysis", area="Analysis & Probability"),
        _scope("Analysis/WantedExt", "Analysis", "WantedExt", navigation="collection"),
        _scope("Analysis/WantedExt/series", "Analysis/WantedExt", "Series", navigation="module"),
        _result("Analysis/WantedExt/series/abel", "Analysis/WantedExt/series", "Abel summation", status="planned"),
    ]
    # (user, used, statement links, proof links): Abel summation uses Lagrange; Cauchy uses Lagrange in its proof.
    relations = [
        ("Analysis/WantedExt/series/abel", "Algebra/MathlibExt/part-01/groups/lagrange", 1, 0),
        ("Algebra/MathlibExt/part-01/groups/cauchy", "Algebra/MathlibExt/part-01/groups/lagrange", 0, 1),
    ]
    return records, relations


def _part(files: dict[str, object], part_id: str) -> dict[str, object]:
    return files[f"parts/{project_map.part_file(part_id)}"]["parts"][part_id]  # type: ignore[index,return-value]


def test_collections_and_pagination_are_flattened_and_collections_become_facets() -> None:
    files = project_map.build_project_map(*_library())
    groups = _part(files, "Algebra/MathlibExt/part-01/groups")
    assert groups["path"] == [["region:Algebra & Number Theory", "Algebra & Number Theory"], ["Algebra", "Algebra"],
                              ["Algebra/MathlibExt/part-01/groups", "Groups"]]
    assert [row["facet"] for row in groups["results"]] == ["MathlibExt", "MathlibExt"]
    # Results keep reading order, not id order.
    assert [row["title"] for row in groups["results"]] == ["Lagrange", "Cauchy"]
    algebra = _part(files, "Algebra")
    assert [child["id"] for child in algebra["children"]] == ["Algebra/MathlibExt/part-01/groups"]
    assert all("Algebra/MathlibExt" not in shard["parts"] for key, shard in files.items() if key.startswith("parts/"))
    assert files["index.json"]["facets"] == ["MathlibExt", "WantedExt"]


def test_authored_areas_become_regions_and_projects_without_them_use_chapters() -> None:
    records, relations = _library()
    regions = project_map.build_project_map(records, relations)["index.json"]["regions"]
    # Analysis is alone in its area, so the chapter stands for the area instead of adding a level.
    assert [region["id"] for region in regions] == ["region:Algebra & Number Theory", "Analysis"]
    assert [region["results"] for region in regions] == [3, 1]
    plain = [{key: value for key, value in record.items() if key != "area"} for record in records]
    # Without areas the chapters are the regions, in reading order.
    assert [region["id"] for region in project_map.build_project_map(plain, relations)["index.json"]["regions"]] == [
        "Algebra", "NumberTheory", "Analysis"]


def test_a_part_that_names_another_area_joins_that_area() -> None:
    records = [
        _scope("roadmap", None, "Library"),
        _scope("Geometry", "roadmap", "Geometry", area="Geometry"),
        _result("Geometry/euclid", "Geometry", "Euclid"),
        _scope("Geometry/measure", "Geometry", "Measure", area="Analysis", navigation="module"),
        _result("Geometry/measure/lebesgue", "Geometry/measure", "Lebesgue"),
        _scope("Geometry/measure/outer", "Geometry/measure", "Outer measure", navigation="module"),
        _result("Geometry/measure/outer/caratheodory", "Geometry/measure/outer", "Caratheodory"),
        _scope("Calculus", "roadmap", "Calculus", area="Analysis"),
        _result("Calculus/ftc", "Calculus", "Fundamental theorem"),
    ]
    files = project_map.build_project_map(records, [])
    regions = {region["id"]: (region["title"], region["results"]) for region in files["index.json"]["regions"]}
    # Measure sits in the Geometry chapter but is authored as Analysis: it is counted there, with what it holds.
    assert regions == {"Geometry": ("Geometry", 1), "region:Analysis": ("Analysis", 3)}
    assert [step for step, _title in _part(files, "Geometry/measure/outer")["path"]] == [
        "region:Analysis", "Geometry/measure", "Geometry/measure/outer"]
    # An area no chapter names is a finer one inside the chapter's, so the part stays where it was written.
    finer = [record for record in records if not str(record["id"]).startswith("Calculus")]
    files = project_map.build_project_map([*finer, _scope("Algebra", "roadmap", "Algebra", area="Algebra"),
                                           _result("Algebra/x", "Algebra", "X")], [])
    assert {region["id"]: region["results"] for region in files["index.json"]["regions"]} == {"Geometry": 3, "Algebra": 1}
    assert [step for step, _title in _part(files, "Geometry/measure")["path"]] == ["Geometry", "Geometry/measure"]


def test_public_api_counts_outside_users_and_imports_count_outside_dependencies() -> None:
    files = project_map.build_project_map(*_library())
    regions = {region["id"]: region for region in files["index.json"]["regions"]}
    algebra, analysis = regions["region:Algebra & Number Theory"], regions["Analysis"]
    # Abel summation (Analysis) uses Lagrange (Algebra): Lagrange is the region's public API, Analysis imports it.
    assert algebra["api"] == [["Algebra/MathlibExt/part-01/groups/lagrange", "Lagrange", 1]]
    assert (algebra["api_total"], algebra["imports_total"]) == (1, 0)
    assert (analysis["api_total"], analysis["imports_total"]) == (0, 1)
    # Cauchy's use of Lagrange stays inside the part, so it is internal, not API.
    groups = _part(files, "Algebra/MathlibExt/part-01/groups")
    assert groups["result_edges"] == [["Algebra/MathlibExt/part-01/groups/cauchy", "Algebra/MathlibExt/part-01/groups/lagrange", 0, 1]]
    assert groups["boundary_edges"] == [["Analysis/WantedExt/series/abel", "Algebra/MathlibExt/part-01/groups/lagrange", 1, 0]]
    assert groups["outside"] == [["Analysis/WantedExt/series/abel", "Abel summation", ["Analysis", "Analysis/WantedExt/series"]]]


def test_links_are_summed_between_the_siblings_below_their_common_ancestor() -> None:
    records, relations = _library()
    relations.append(("Algebra/MathlibExt/part-01/groups/lagrange", "Analysis/WantedExt/series/abel", 0, 2))
    relations.append(("NumberTheory/euclid", "Algebra/MathlibExt/part-01/groups/lagrange", 1, 0))
    files = project_map.build_project_map(records, relations)
    assert files["index.json"]["region_edges"] == [
        ["Analysis", "region:Algebra & Number Theory", 1, 0],
        ["region:Algebra & Number Theory", "Analysis", 0, 2],
    ]
    # Inside the region, Number Theory uses Algebra.
    assert _part(files, "region:Algebra & Number Theory")["edges"] == [["NumberTheory", "Algebra", 1, 0]]


def test_wide_parts_are_grouped_into_linked_communities_and_one_standalone_group() -> None:
    records = [_scope("roadmap", None, "Library"), _scope("Big", "roadmap", "Big subject")]
    relations = []
    for index in range(40):
        records.append(_scope(f"Big/t{index:02d}", "Big", f"Topic {index:02d}", navigation="module"))
        records.append(_result(f"Big/t{index:02d}/r", f"Big/t{index:02d}", f"Result {index:02d}"))
    # Topics 0-2 link to each other; topics 3-4 link; the other 35 link to nothing.
    relations += [("Big/t01/r", "Big/t00/r", 1, 0), ("Big/t02/r", "Big/t01/r", 1, 0), ("Big/t04/r", "Big/t03/r", 0, 1)]
    files = project_map.build_project_map(records, relations)
    big = _part(files, "Big")
    titles = {child["id"]: child["title"] for child in big["children"]}
    assert len(big["children"]) == 3
    assert titles["Big/~1"] == "Topic 00 and 2 linked topics"
    assert titles["Big/~2"] == "Topic 03 and Topic 04"
    assert titles["Big/~3"] == "Standalone topics"
    assert all(child["grouped"] for child in big["children"])
    standalone = _part(files, "Big/~3")
    assert [child["title"] for child in standalone["children"]][:3] == ["Topic 05", "Topic 06", "Topic 07"]
    # The groups' links survive one level up, summed per pair of groups.
    assert _part(files, "Big/~1")["edges"] == [["Big/t01", "Big/t00", 1, 0], ["Big/t02", "Big/t01", 1, 0]]
    rebuilt = project_map.build_project_map(records, relations)
    assert rebuilt == files, "the same data must give the same groups, ids and files"


def test_search_table_rebuilds_every_path_and_stores_short_ids() -> None:
    files = project_map.build_project_map(*_library())
    search = files["search.json"]
    parts = search["parts"]
    position = {row[0]: index for index, row in enumerate(parts)}

    def path(index: int) -> list[str]:
        out = []
        while index >= 0:
            out.insert(0, parts[index][0])
            index = parts[index][3]
        return out

    row = next(row for row in search["rows"] if row[0] == "Lagrange")
    assert row[1] == "Subgroup.card_dvd" and row[3] == "lagrange"
    assert path(row[2]) == ["region:Algebra & Number Theory", "Algebra", "Algebra/MathlibExt/part-01/groups"]
    assert parts[position["Algebra"]][2] == project_map.part_file("Algebra")


def test_records_from_view_turn_dependency_edges_into_user_used_relations() -> None:
    nodes = (
        ViewNode("roadmap", "Library", "scope", (), (("planned", 1),)),
        ViewNode("a", "A", "node", (), (("fully_proved", 1),), status_key="fully_proved", lean="A.a"),
        ViewNode("b", "B", "node", (), (("planned", 1),), status_key="planned"),
    )
    view = GraphView("full", "Library", nodes, (ViewEdge("a", "b", statement_count=1),))  # b depends on a
    records, relations = project_map.records_from_view(view, {"a": {"parent": "roadmap", "result": True}, "b": {"parent": "roadmap", "result": True}})
    assert relations == [("b", "a", 1, 0)]
    assert records[1] == {"id": "a", "kind": "node", "title": "A", "status": "fully_proved", "lean": "A.a",
                          "parent": "roadmap", "result": True}


def test_page_escapes_its_links_and_title(tmp_path: Path) -> None:
    page = project_map.render_page(title='A "map" <b>', map_base="data/", routes="../dependencies/index.json",
                                   home="../", script="../javascripts/project-map.js", style="../stylesheets/project-map.css",
                                   mathjax="../javascripts/mathjax.js")
    assert "<title>A &quot;map&quot; &lt;b&gt; · map</title>" in page
    assert 'data-map="data/"' in page
    written = project_map.write_project_map(tmp_path, project_map.build_project_map(*_library()))
    assert tmp_path / "index.json" in written
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["schema"] == project_map.SCHEMA


def test_results_in_the_root_get_a_part_of_their_own() -> None:
    flat = [_scope("roadmap", None, "Library"), _result("roadmap/a", "roadmap", "A"), _result("roadmap/b", "roadmap", "B")]
    files = project_map.build_project_map(flat, [("roadmap/b", "roadmap/a", 1, 0)])
    # A flat roadmap is one region, named after the project, holding every result.
    regions = files["index.json"]["regions"]
    assert [(region["id"], region["title"], region["results"]) for region in regions] == [("roadmap/~results", "Library", 2)]
    assert [row["id"] for row in _part(files, "roadmap/~results")["results"]] == ["roadmap/a", "roadmap/b"]
    assert [row[0] for row in files["search.json"]["rows"]] == ["A", "B"]
    # Their ids don't extend the generated part's, so search keeps them whole.
    assert [row[3] for row in files["search.json"]["rows"]] == [["roadmap/a"], ["roadmap/b"]]
    # Beside chapters, the loose results are named for what they are.
    records, relations = _library()
    records.append(_result("roadmap/main", "roadmap", "Main theorem"))
    relations.append(("roadmap/main", "Analysis/WantedExt/series/abel", 1, 0))
    regions = {region["id"]: region for region in project_map.build_project_map(records, relations)["index.json"]["regions"]}
    assert regions["roadmap/~results"]["title"] == "Outside the chapters"
    assert regions["Analysis"]["api_total"] == 1


def test_generated_parts_never_take_an_authored_id() -> None:
    # Authored ids that look generated: a group under a wide part, and the root's own results.
    records = [_scope("roadmap", None, "Library"), _scope("Big", "roadmap", "Big subject"),
               _scope("Big/~1", "Big", "Authored topic", navigation="module"), _result("Big/~1/r", "Big/~1", "Authored result"),
               _scope("roadmap/~results", "roadmap", "Authored chapter"), _result("roadmap/~results/r", "roadmap/~results", "In it"),
               _result("roadmap/loose", "roadmap", "Loose result")]
    for index in range(40):
        records.append(_scope(f"Big/t{index:02d}", "Big", f"Topic {index:02d}", navigation="module"))
        records.append(_result(f"Big/t{index:02d}/r", f"Big/t{index:02d}", f"Result {index:02d}"))
    files = project_map.build_project_map(records, [("Big/t01/r", "Big/t00/r", 1, 0)])
    # The linked pair is the first group and the rest stand alone, reading first; the authored part sits in a group,
    # not in itself.
    assert [(child["id"], child["title"]) for child in _part(files, "Big")["children"]] == [
        ("Big/~2", "Standalone topics"), ("Big/~1~", "Topic 00 and Topic 01")]
    assert [step for step, _title in _part(files, "Big/~1")["path"]] == ["Big", "Big/~2", "Big/~1"]
    regions = {region["id"]: region["title"] for region in files["index.json"]["regions"]}
    assert regions == {"Big": "Big subject", "roadmap/~results": "Authored chapter", "roadmap/~results~": "Outside the chapters"}
    rows = {row[0]: row[3] for row in files["search.json"]["rows"]}
    assert rows["Loose result"] == ["roadmap/loose"] and rows["In it"] == "r"


def test_the_map_writes_only_files_render_can_guard() -> None:
    files = project_map.build_project_map(*_library())
    assert {f"{project_map.MAP_DIRECTORY}/{name}" for name in files} <= set(project_map.map_paths())
    assert len(project_map.map_paths()) == 2 + project_map.SHARDS


def test_a_view_without_one_root_gets_one() -> None:
    nodes = (ViewNode("a", "A", "node", (), (("fully_proved", 1),), status_key="fully_proved"),
             ViewNode("b", "B", "node", (), (("planned", 1),), status_key="planned"))
    view = GraphView("full", "Library", nodes, (ViewEdge("a", "b", statement_count=1),))
    records, relations = project_map.records_from_view(view, {})
    assert records[0] == {"id": "~roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False}
    regions = project_map.build_project_map(records, relations)["index.json"]["regions"]
    assert [(region["title"], region["results"]) for region in regions] == [("Library", 2)]
    # Nor does the made-up root take an authored id.
    clash = GraphView("full", "Library", (*nodes, ViewNode("~roadmap", "Odd name", "node", (), (("planned", 1),))), view.edges)
    records, _relations = project_map.records_from_view(clash, {})
    assert records[0]["id"] == "~roadmap~" and {record["parent"] for record in records[1:]} == {"~roadmap~"}


def test_a_map_needs_exactly_one_root() -> None:
    with pytest.raises(ValueError, match="exactly one root"):
        project_map.build_project_map([_scope("a", None, "A"), _scope("b", None, "B")], [])
