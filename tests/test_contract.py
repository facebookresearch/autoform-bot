"""The contract between the wiki and the Lean build, checked on random roadmaps.

PR #115 lets a theorem's statement land with a ``sorry`` proof when
``roadmap/README.md`` sets ``open_statements: allowed``. Three readers must then
agree on what that means: the derived status (``derive``), the work frontier
(``list_ready_work``), and the assumptions contract CI audits the Lean build
against (``assumption_contract``). The property test writes seeded random
roadmaps under both policies and checks each reader against the others and
against the semantics stated here; the real-Lean test checks that the published
site never claims more than the audit of an actual build.
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from autoform_cli.graph import GraphValidationError, load_graph
from autoform_cli.lean import declaration_names
from autoform_cli.runtime import load_runtime_graph
from autoform_cli.status import STATES, derive
from autoform_cli.work import WorkError, assumption_contract, list_ready_work
from tests.test_impact import _lean_toolchain_available
from tests.test_lake_artifact_audit import _TEMPLATE, _load_helper, _run, _run_probe, _write


_SEEDS = range(30)
_POLICIES = ("forbidden", "allowed")


@dataclass(frozen=True)
class _Spec:
    """One generated article: its frontmatter and its typed dependency links."""

    name: str
    declaration: str | None = None
    statement: str | None = None
    proof: bool = False
    mathlib: bool = False
    not_ready: bool = False
    lean: str | None = None
    article_id: str | None = None
    statement_dependencies: tuple[str, ...] = ()
    proof_dependencies: tuple[str, ...] = ()

    @property
    def dependencies(self) -> tuple[str, ...]:
        return self.statement_dependencies + tuple(
            name for name in self.proof_dependencies if name not in self.statement_dependencies
        )


def _generate(rng: random.Random) -> list[_Spec]:
    """Return a random roadmap in dependency order; file names are shuffled."""
    count = rng.randint(2, 8)
    names = [f"a{index}" for index in rng.sample(range(count), count)]
    specs: list[_Spec] = []
    for index, name in enumerate(names):
        declaration = rng.choices(["theorem", "def", None], weights=[6, 3, 1])[0]
        mathlib = rng.random() < 0.15
        lean = None
        if rng.random() < 0.6:
            lean = f"Gen.{name}" if rng.random() < 0.8 else f"Gen.{name}, Gen.{name}_aux"
        statements = [None, "formalized"] + (["retracted"] if lean and not mathlib else [])
        statement = rng.choices(statements, weights=[35, 45, 20][: len(statements)])[0]
        # The loader requires lean: for a formalized statement or proof.
        if statement == "formalized" and lean is None:
            lean = f"Gen.{name}"
        statement_dependencies: list[str] = []
        proof_dependencies: list[str] = []
        for earlier in names[:index]:
            roll = rng.random()
            if roll < 0.2 or 0.35 <= roll < 0.4:
                statement_dependencies.append(earlier)
            if 0.2 <= roll < 0.4:
                proof_dependencies.append(earlier)
        specs.append(
            _Spec(
                name=name,
                declaration=declaration,
                statement=statement,
                proof=statement == "formalized" and rng.random() < 0.35,
                mathlib=mathlib,
                not_ready=rng.random() < 0.1,
                lean=lean,
                article_id=f"af_{index + 1:024x}" if rng.random() < 0.95 else None,
                statement_dependencies=tuple(statement_dependencies),
                proof_dependencies=tuple(proof_dependencies),
            )
        )
    return specs


def _write_roadmap(project: Path, specs: list[_Spec], policy: str) -> None:
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text(f"---\nopen_statements: {policy}\n---\n\n# Roadmap\n", encoding="utf-8")
    for spec in specs:
        metadata = [
            f"{key}: {value}"
            for key, value in (
                ("article_id", spec.article_id),
                ("declaration", spec.declaration),
                ("statement", spec.statement),
                ("proof", "formalized" if spec.proof else None),
                ("mathlib", "true" if spec.mathlib else None),
                ("not_ready", "true" if spec.not_ready else None),
                ("lean", spec.lean),
            )
            if value is not None
        ]
        lines = ["---", *metadata, "---", "", f"# Article {spec.name}", "", "A precise statement."]
        for heading, targets in (
            ("Depends on", spec.statement_dependencies),
            ("Proof depends on", spec.proof_dependencies),
        ):
            if targets:
                lines.extend(["", f"## {heading}", "", *(f"- [{target}]({target}.md)" for target in targets)])
        (roadmap / f"{spec.name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _open_statements(specs: list[_Spec], open_policy: bool) -> dict[str, frozenset[str]]:
    """The open statements each article's proof rests on, from the frontmatter alone.

    An open statement is a theorem that is not proved but is stated or records
    ``statement: retracted``; its own proof is the ``sorry``. Either way it
    names ``lean:``, which the loader requires. A dependency that is open
    contributes itself and what its statement prerequisites reach, a proved
    dependency or a definition naming ``lean:`` passes on everything it
    reaches, and anything else (an unstated theorem, a Mathlib article)
    reaches nothing.
    """
    reaches: dict[str, frozenset[str]] = {}
    assumes: dict[str, frozenset[str]] = {}
    for spec in specs:
        definition = spec.declaration == "def"
        stated = spec.statement == "formalized" or spec.mathlib
        proved = spec.proof or spec.mathlib or (definition and stated)
        reached = frozenset().union(*(reaches.get(name, frozenset()) for name in spec.dependencies))
        assumes[spec.name] = reached if open_policy and not spec.mathlib else frozenset()
        if not open_policy or spec.mathlib:
            continue
        if proved or (definition and spec.lean):
            reaches[spec.name] = reached
        elif not definition and (stated or spec.statement == "retracted"):
            reaches[spec.name] = frozenset({spec.name}).union(
                *(reaches.get(name, frozenset()) for name in spec.statement_dependencies)
            )
    return assumes


_LOAD_FAULTS = {
    "no lean": (
        {"lean": None},
        "{name}: statement: retracted needs the lean: declaration it retracts; without lean:, omit statement",
    ),
    "proof": ({"proof": True}, "{name}: proof: formalized needs statement: formalized, not retracted"),
    "mathlib": ({"mathlib": True}, "{name}: a mathlib: true article cannot record statement: retracted"),
    "formalized without lean": (
        {"statement": "formalized", "lean": None},
        "{name}: statement: formalized needs the lean: declaration that formalizes it",
    ),
    "proof without statement": (
        {"statement": None, "proof": True},
        "{name}: proof: formalized needs statement: formalized",
    ),
}


def _check_roadmap(
    project: Path, specs: list[_Spec], policy: str, audit_helper, seen: set[str]
) -> None:
    open_policy = policy == "allowed"
    by_name = {spec.name: spec for spec in specs}
    graph = load_graph(project / "blueprint")
    assert graph.open_statements is open_policy
    assert audit_helper.blueprint_policy(project / "blueprint") == policy
    statuses = derive(graph)
    assert set(statuses) == {"roadmap", *by_name}
    expected_assumes = _open_statements(specs, open_policy)

    def proved_by_frontmatter(name: str) -> bool:
        spec = by_name[name]
        return spec.proof or spec.mathlib or (spec.declaration == "def" and spec.statement == "formalized")

    def transitive(name: str) -> set[str]:
        found: set[str] = set()
        pending = list(by_name[name].dependencies)
        while pending:
            dependency = pending.pop()
            if dependency not in found:
                found.add(dependency)
                pending.extend(by_name[dependency].dependencies)
        return found

    open_articles: set[str] = set()
    for spec in specs:
        status = statuses[spec.name]
        seen.add(f"{policy}:{status.key}")
        assert status.proved == proved_by_frontmatter(spec.name)
        assert status.fully_proved == (
            status.proved and all(statuses[name].fully_proved for name in spec.dependencies)
        )
        # 1. Fully proved means every prerequisite, however far down, is proved in
        # its own frontmatter, and nothing is assumed.
        if status.fully_proved:
            assert all(proved_by_frontmatter(name) for name in transitive(spec.name)), spec.name
            assert status.assumes == ()
        # 2. and 3. Assuming an open statement rules out fully proved, and a
        # proved article that assumes one is exactly a conditional one.
        if status.assumes:
            assert not status.fully_proved
        assert (status.key == "conditional") == (status.proved and bool(status.assumes)), spec.name
        # 4. The strict policy has no open statements to assume.
        if not open_policy:
            assert status.assumes == ()
        assert set(status.assumes) == expected_assumes[spec.name], spec.name
        assert list(status.assumes) == sorted(status.assumes)
        # 10. A theorem that was never stated is nobody's assumption.
        if spec.statement is None:
            assert all(spec.name not in other.assumes for other in statuses.values())
        # 6. Waiting on nothing means proved, or ready for the next phase.
        ready = status.can_prove if status.stated else status.can_state
        assert (status.waiting_on == ()) == (status.proved or ready), spec.name
        if (
            open_policy
            and spec.lean
            and not status.proved
            and spec.declaration != "def"
            and (status.stated or spec.statement == "retracted")
        ):
            open_articles.add(spec.name)

    runtime = load_runtime_graph(project)
    nodes = {node.id: node for node in runtime.nodes}
    for spec in specs:
        assert nodes[spec.name].dispatchable == (spec.declaration is not None)

    # 5. The work list offers exactly the ready phases derive reports, on
    # formalizable leaves with an article_id that are not marked not_ready; an
    # unfinished leaf without an article_id stops the whole list instead.
    missing = [
        spec.name
        for spec in specs
        if spec.declaration is not None
        and not spec.mathlib
        and not statuses[spec.name].proved
        and spec.article_id is None
    ]
    if missing:
        seen.add("work:missing-article-id")
        with pytest.raises(WorkError, match=re.escape(", ".join(sorted(missing)) + " (plan IDs")):
            list_ready_work(project)
    else:
        frontier = list_ready_work(project)
        assert frontier.open_statements is open_policy
        offered = {item.node_id: item for item in frontier.items}
        expected = {
            spec.name: "proof" if statuses[spec.name].stated else "statement"
            for spec in specs
            if spec.declaration is not None
            and spec.article_id is not None
            and not spec.not_ready
            and not statuses[spec.name].proved
            and (statuses[spec.name].can_prove if statuses[spec.name].stated else statuses[spec.name].can_state)
        }
        assert {name: item.phase for name, item in offered.items()} == expected
        for name, item in offered.items():
            seen.add(f"work:{item.phase}")
            status = statuses[name]
            assert (item.state, item.assumes, item.blockers) == (status.key, status.assumes, ())
            assert item.claim_target == by_name[name].article_id
            assert item.revision == (by_name[name].statement == "retracted")

    # 7. to 9. The contract lists every article naming lean:, opens exactly the
    # open statements, and allows each article only what it rests on.
    contract = assumption_contract(project)
    assert contract.open_statements is open_policy
    articles = {article.id: article for article in contract.articles}
    assert set(articles) == {spec.name for spec in specs if spec.lean}
    assert {name for name, article in articles.items() if article.open} == open_articles
    open_declarations = {name for article in articles.values() if article.open for name in article.declarations}
    for name, article in articles.items():
        spec = by_name[name]
        status = statuses[name]
        assert article.declarations == tuple(declaration_names(spec.lean or ""))
        assert (article.state, article.assumes) == (status.key, status.assumes)
        if len(article.assumes) >= 2:
            seen.add("contract:assumes-several")
        assert set(article.allowed_open_declarations) <= open_declarations, name
        allowed = {
            declaration
            for assumed in status.assumes
            if assumed in articles
            for declaration in articles[assumed].declarations
        }
        if article.open:
            allowed |= set(article.declarations)
        assert article.allowed_open_declarations == tuple(sorted(allowed)), name
        if article.open:
            seen.add("contract:retracted-open" if spec.statement == "retracted" else "contract:open")
            assert set(article.declarations) <= set(article.allowed_open_declarations)
        if spec.mathlib:
            seen.add("contract:mathlib")
            assert (article.open, article.assumes, article.allowed_open_declarations) == (False, (), ())
    if open_policy:
        # The audit's own validator accepts every contract the CLI emits.
        path = project / "contract.json"
        path.write_text(contract.to_json(), encoding="utf-8")
        entries = audit_helper.load_assumption_contract(path)
        assert {entry.name for entry in entries if entry.is_open} == open_declarations


def test_random_roadmaps_keep_the_wiki_and_lean_contract(repo_root: Path, tmp_path: Path) -> None:
    audit_helper = _load_helper(repo_root)
    seen: set[str] = set()
    for seed in _SEEDS:
        rng = random.Random(seed)
        specs = _generate(rng)
        for policy in _POLICIES:
            project = tmp_path / f"{seed}-{policy}"
            _write_roadmap(project, specs, policy)
            try:
                _check_roadmap(project, specs, policy, audit_helper, seen)
            except AssertionError as error:
                raise AssertionError(f"seed {seed}, policy {policy}: {error}") from error

        # 11. Each invalid retraction, and each formalized assertion the Lean
        # cannot back, is refused at load, with its own message.
        fault = rng.choice(sorted(_LOAD_FAULTS))
        changes, message = _LOAD_FAULTS[fault]
        victim = rng.randrange(len(specs))
        fields = {"statement": "retracted", "lean": f"Gen.{specs[victim].name}", "proof": False, "mathlib": False}
        broken = replace(specs[victim], **{**fields, **changes})
        project = tmp_path / f"{seed}-invalid"
        _write_roadmap(project, [*specs[:victim], broken, *specs[victim + 1 :]], rng.choice(_POLICIES))
        with pytest.raises(GraphValidationError) as raised:
            load_graph(project / "blueprint")
        assert raised.value.issues == (message.format(name=broken.name),), f"seed {seed}"
        seen.add(f"invalid:{fault}")

    # The seeds are fixed, so this only guards against a generator change that
    # stops exercising a state or a branch.
    expected = {f"forbidden:{state.key}" for state in STATES if state.key != "conditional"}
    expected |= {f"allowed:{state.key}" for state in STATES}
    expected |= {"work:statement", "work:proof", "work:missing-article-id"}
    expected |= {"contract:open", "contract:retracted-open", "contract:mathlib", "contract:assumes-several"}
    expected |= {f"invalid:{fault}" for fault in _LOAD_FAULTS}
    assert expected <= seen, sorted(expected - seen)


# --------------------------------------------------------------------------- #
# Agreement with the audit of a real Lean build
# --------------------------------------------------------------------------- #


_CONTRACT_LEAN = """namespace Fixture

def base : Nat := 2

theorem base_eq : base = 2 := rfl

theorem open_stmt : 1 + 1 = 2 := sorry

theorem uses_open : 1 + 1 = 2 ∧ True := ⟨open_stmt, trivial⟩

theorem retracted_stmt : 3 + 3 = 6 := sorry

theorem uses_retracted : 3 + 3 = 6 := retracted_stmt

theorem clean : 2 + 2 = 4 := rfl

end Fixture
"""

_CONTRACT_ROADMAP = (
    ("base", ["declaration: def", "statement: formalized", "lean: Fixture.base"], (), ()),
    (
        "base-eq",
        ["declaration: theorem", "statement: formalized", "proof: formalized", "lean: Fixture.base_eq"],
        ("base",),
        (),
    ),
    ("open", ["declaration: theorem", "statement: formalized", "lean: Fixture.open_stmt"], (), ()),
    (
        "uses-open",
        ["declaration: theorem", "statement: formalized", "proof: formalized", "lean: Fixture.uses_open"],
        (),
        ("open",),
    ),
    ("retracted", ["declaration: theorem", "statement: retracted", "lean: Fixture.retracted_stmt"], (), ()),
    (
        "uses-retracted",
        ["declaration: theorem", "statement: formalized", "proof: formalized", "lean: Fixture.uses_retracted"],
        (),
        ("retracted",),
    ),
    (
        "clean",
        ["declaration: theorem", "statement: formalized", "proof: formalized", "lean: Fixture.clean"],
        (),
        (),
    ),
)

_AUDIT_LINE = re.compile(r"(sorry-free|conditional|open statement \([^)]*\)): (\S+) \[([^\]]+)\]")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_the_site_never_claims_more_than_the_audit_of_a_real_build(repo_root: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(repo_root / "tests/fixtures/skeleton-project/lean-toolchain", project / "lean-toolchain")
    _write(project / "lakefile.toml", 'name = "Fixture"\ndefaultTargets = ["Fixture"]\n\n[[lean_lib]]\nname = "Fixture"\n')
    _write(project / "Fixture.lean", _CONTRACT_LEAN)
    _write(project / "blueprint/roadmap/README.md", "---\nopen_statements: allowed\n---\n\n# Roadmap\n")
    for index, (name, metadata, depends, proof_depends) in enumerate(_CONTRACT_ROADMAP, start=1):
        lines = ["---", f"article_id: af_{index:024x}", *metadata, "---", "", f"# {name}", ""]
        for heading, targets in (("Depends on", depends), ("Proof depends on", proof_depends)):
            if targets:
                lines.extend([f"## {heading}", "", *(f"- [{target}]({target}.md)" for target in targets), ""])
        _write(project / f"blueprint/roadmap/{name}.md", "\n".join(lines))

    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    # The verify workflow's steps: read the policy, write the contract, prepare
    # the open-statement probe, and run it as the audit step does.
    helper = [sys.executable, str(repo_root / _TEMPLATE)]
    policy = subprocess.run([*helper, "--policy", "blueprint"], cwd=project, capture_output=True, text=True)
    assert (policy.returncode, policy.stdout) == (0, "allowed\n"), policy.stderr
    contract = project / "contract.json"
    contract.write_text(assumption_contract(project).to_json(), encoding="utf-8")
    probe = project / "probe.lean"
    prepared = subprocess.run(
        [*helper, "--open-statements", str(contract), "Fixture", str(archive), str(probe)],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    audited = _run_probe(project, probe)
    output = audited.stdout + audited.stderr
    assert audited.returncode == 0, output
    assert "kernel trust clean except declared open statements (" in output

    reported: dict[str, set[str]] = {}
    for verdict, declaration, article in _AUDIT_LINE.findall(output):
        reported.setdefault(article, set()).add(verdict.split(" (")[0])
        assert declaration.startswith("Fixture.")

    runtime = {node.id: node for node in load_runtime_graph(project).nodes}
    opened = {article.id for article in assumption_contract(project).articles if article.open}
    assert {name: node.status.state for name, node in runtime.items() if name != "roadmap"} == {
        "base": "fully_proved",
        "base-eq": "fully_proved",
        "open": "can_prove",
        "uses-open": "conditional",
        "retracted": "can_state",
        "uses-retracted": "conditional",
        "clean": "fully_proved",
    }
    assert opened == {"open", "retracted"}
    # Every article the site shows fully proved, the audit found sorry-free.
    for name, node in runtime.items():
        if node.status.fully_proved:
            assert reported.get(name) == {"sorry-free"}, (name, output)
    # Every article the audit found resting on an open statement, the site
    # shows conditional or open, never proved outright.
    resting = {name for name, verdicts in reported.items() if verdicts & {"conditional", "open statement"}}
    assert resting == {"open", "uses-open", "retracted", "uses-retracted"}, output
    for name in resting:
        assert runtime[name].status.state == "conditional" or name in opened, name
        assert not runtime[name].status.fully_proved
