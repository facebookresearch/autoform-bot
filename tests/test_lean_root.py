"""An unusable Lean root is refused with a reason that names no host path."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.lean import LeanSourceError, index_project, resolve_lean_root

_MISSING = "Lean root does not exist or is not a directory"
_UNSEARCHABLE = "permission denied while resolving the Lean root"
_UNRESOLVABLE = "Lean root cannot be resolved"


def _blueprint(tmp_path: Path) -> Path:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    coverage = blueprint / "coverage"
    roadmap.mkdir(parents=True)
    coverage.mkdir(parents=True)
    (roadmap / "result.md").write_text(
        "---\ndeclaration: theorem\nlean: Proj.result\n---\n\n"
        "# Result\n\nA precise statement.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    (coverage / "README.md").write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | No formalization target |\n",
        encoding="utf-8",
    )
    return blueprint


@pytest.fixture
def unusable_root(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """Yield a Lean root that cannot be used and the reason it must be refused with."""

    kind = request.param
    if kind == "missing":
        yield tmp_path / "missing", _MISSING
    elif kind == "file":
        root = tmp_path / "file"
        root.write_text("", encoding="utf-8")
        yield root, _MISSING
    elif kind == "unknown-home":
        if os.name == "nt":
            pytest.skip("Windows expands ~name beside the current home directory")
        yield Path("~autoform-no-such-user/proj"), _UNRESOLVABLE
    elif kind == "loop":
        if os.name == "nt":
            pytest.skip("Windows reports a symbolic link loop by its own error code")
        root = tmp_path / "loop"
        try:
            root.symlink_to(root)
        except OSError:
            pytest.skip("symbolic links are unavailable")
        yield root, _UNRESOLVABLE
    else:
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            pytest.skip("search permission is not enforced here")
        locked = tmp_path / "locked"
        (locked / "proj").mkdir(parents=True)
        (locked / "proj" / "Proj.lean").write_text("theorem result : True := trivial\n", encoding="utf-8")
        locked.chmod(0)
        try:
            if os.access(locked, os.X_OK):
                pytest.skip("search permission is not enforced")
            yield locked / "proj", _UNSEARCHABLE
        finally:
            locked.chmod(0o755)


def _without_host_paths(text: str, tmp_path: Path) -> bool:
    return str(tmp_path) not in text and str(tmp_path.resolve()) not in text


@pytest.mark.parametrize(
    "unusable_root", ["missing", "file", "unsearchable", "unknown-home", "loop"], indirect=True
)
def test_resolve_lean_root_names_why_the_root_is_unusable(unusable_root: tuple[Path, str]) -> None:
    root, reason = unusable_root

    with pytest.raises(LeanSourceError) as caught:
        resolve_lean_root(root)

    assert str(caught.value) == reason


@pytest.mark.parametrize("unusable_root", ["missing", "unknown-home"], indirect=True)
def test_index_project_refuses_an_unusable_root(unusable_root: tuple[Path, str]) -> None:
    root, reason = unusable_root

    with pytest.raises(LeanSourceError) as caught:
        index_project(root)

    assert str(caught.value) == reason


# One case per command code path, each with a root that command misreported
# before it called resolve_lean_root. The unknown-home cases pin that check and
# render resolve the root before anything expands it, which would raise a raw
# RuntimeError. render's other messages, skeleton and doctor are pinned in
# tests/test_render.py, tests/test_skeleton.py and tests/test_doctor.py.
@pytest.mark.parametrize(
    ("arguments", "unusable_root", "code", "expected"),
    [
        pytest.param(["check", "{blueprint}"], "missing", 1, "error: Lean sources could not be indexed: {}\n", id="check"),
        pytest.param(
            ["check", "{blueprint}"],
            "unknown-home",
            1,
            "error: Lean sources could not be indexed: {}\n",
            id="check-unknown-home",
        ),
        pytest.param(
            ["render", "{blueprint}", "-o", "{site}"],
            "unknown-home",
            1,
            "error: Lean sources could not be indexed: {}\n",
            id="render-unknown-home",
        ),
        pytest.param(["search", "{blueprint}", "result"], "unsearchable", 2, "error: {}\n", id="search"),
        pytest.param(["work", "list", "{blueprint}"], "unsearchable", 2, "error: {}\n", id="work-list"),
        pytest.param(["work", "impact", "result", "{blueprint}"], "missing", 2, "error: {}\n", id="work-impact"),
    ],
    indirect=["unusable_root"],
)
def test_command_names_the_unusable_lean_root(
    tmp_path: Path,
    unusable_root: tuple[Path, str],
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    code: int,
    expected: str,
) -> None:
    root, reason = unusable_root
    blueprint = _blueprint(tmp_path)
    arguments = [argument.format(blueprint=blueprint, site=tmp_path / "site") for argument in arguments]

    assert main([*arguments, "--lean-root", str(root)]) == code

    captured = capsys.readouterr()
    assert captured.out + captured.err == expected.format(reason)
    assert _without_host_paths(captured.out + captured.err, tmp_path)


@pytest.mark.parametrize("unusable_root", ["unsearchable"], indirect=True)
def test_audit_names_the_unusable_lean_root(
    tmp_path: Path, unusable_root: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, reason = unusable_root
    blueprint = _blueprint(tmp_path)

    assert main(["audit", str(blueprint), "--lean-root", str(root), "--json"]) == 1

    captured = capsys.readouterr()
    findings = json.loads(captured.out)["findings"]
    assert {"article_path": ".", "code": "invalid-lean-root", "reason": reason} in findings
    assert _without_host_paths(captured.out + captured.err, tmp_path)
