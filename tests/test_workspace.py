from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from autoform_cli import graph as graph_module
from autoform_cli.__main__ import main
from autoform_cli.doctor import diagnose_project
from autoform_cli.runtime import RuntimeProjectionError, resolve_runtime_paths
from autoform_cli.visualize import main as visualize_main
from autoform_cli import runtime as runtime_module
from autoform_cli import scaffold as scaffold_module
from autoform_cli import workspace as workspace_reader_module
from autoform_cli import workspace_cli as workspace_cli_module
from autoform_cli import workspace_mutation as workspace_module
from autoform_cli.workspace import (
    discover_workspace,
    inspect_workspace,
    load_workspace,
)
from autoform_cli.workspace_manifest import (
    BLUEPRINT_CHANGE_SCHEMA,
    BLUEPRINT_LIST_SCHEMA,
    MAX_MANIFEST_BYTES,
    WORKSPACE_ERROR_SCHEMA,
    WORKSPACE_INIT_SCHEMA,
    WORKSPACE_SCHEMA,
    WorkspaceError,
    parse_workspace,
)
from autoform_cli.workspace_mutation import (
    create_blueprint_project,
    initialize_workspace,
    register_blueprint_project,
)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    initialize_workspace(root, blueprint_root="Plans", location_id="roadmaps")
    return root


def test_manifest_uses_repository_neutral_named_locations() -> None:
    manifest = parse_workspace(
        'schema = "autoform-workspace/v1"\n\n'
        "[locations.plans]\n"
        'path = "docs/formalization"\n'
        'provides = ["blueprints"]\n\n'
        "[locations.library]\n"
        'path = "LeanProject"\n'
        'provides = ["lean-source"]\n\n'
        '[projects."problem-a"]\n'
        'title = "Problem A"\n'
        'blueprint = { location = "plans", path = "ProblemA" }\n'
    )

    assert manifest.schema == WORKSPACE_SCHEMA
    assert [location.id for location in manifest.locations] == ["library", "plans"]
    assert manifest.project("problem-a").blueprint_location == "plans"
    assert manifest.project("problem-a").blueprint_path == "ProblemA"


@pytest.mark.parametrize(
    ("text", "unknown"),
    [
        (
            'schema = "autoform-workspace/v1"\nproject = {}\n'
            '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n',
            "project",
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = "Plans"\nprovide = ["blueprints"]\n',
            "provide",
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n'
            '[projects.one]\ntitel = "One"\n'
            'blueprint = { location = "plans", path = "One" }\n',
            "titel",
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n'
            '[projects.one]\nblueprint = { location = "plans", paths = "One" }\n',
            "paths",
        ),
    ],
)
def test_manifest_rejects_unknown_keys_at_every_schema_level(text: str, unknown: str) -> None:
    with pytest.raises(WorkspaceError, match=unknown):
        parse_workspace(text)


@pytest.mark.parametrize(
    "text",
    [
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = ".git"\nprovides = ["blueprints"]\n'
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.root]\npath = "."\nprovides = ["blueprints"]\n'
            '[projects.control]\n'
            'blueprint = { location = "root", path = ".autoform.toml" }\n'
        ),
    ],
)
def test_manifest_rejects_reserved_repository_paths(text: str) -> None:
    with pytest.raises(WorkspaceError, match="reserved repository path"):
        parse_workspace(text)


def test_workspace_init_creates_only_root_manifest_and_collection(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    result = initialize_workspace(root, blueprint_root="Blueprint", location_id="plans")

    assert result.manifest_path == ".autoform.toml"
    assert (root / ".autoform.toml").is_file()
    assert (root / "Blueprint").is_dir()
    assert list((root / "Blueprint").iterdir()) == []
    assert {path.name for path in root.iterdir()} == {".autoform.toml", "Blueprint"}


def test_workspace_init_supports_a_220_byte_nested_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    component = "N" * 220
    staged_names: list[str] = []

    def record_stage(
        event: str,
        _parent_descriptor: int,
        staging_name: str,
        target_name: str,
    ) -> None:
        if event == "identity-captured-before-bind" and target_name == component:
            staged_names.append(staging_name)

    monkeypatch.setattr(workspace_module, "_workspace_directory_checkpoint", record_stage)

    initialize_workspace(root, blueprint_root=f"Plans/{component}")

    assert len(component.encode()) == 220
    assert (root / "Plans" / component).is_dir()
    assert load_workspace(root).manifest.locations[0].path == f"Plans/{component}"
    assert len(staged_names) == 1
    assert component not in staged_names[0]
    assert len(os.fsencode(staged_names[0])) < 64


def test_workspace_init_fsyncs_each_created_directory_parent_before_manifest_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    events: list[tuple[str, tuple[int, int] | None]] = []
    original_sync = workspace_module.os.fsync
    original_publish = workspace_module._rename_noreplace

    def record_sync(descriptor: int) -> None:
        metadata = workspace_module.os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            events.append(("directory-fsync", (metadata.st_dev, metadata.st_ino)))
        original_sync(descriptor)

    def record_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        if target == ".autoform.toml":
            events.append(("manifest-publish", None))
        original_publish(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module.os, "fsync", record_sync)
    monkeypatch.setattr(workspace_module, "_rename_noreplace", record_publish)

    initialize_workspace(root, blueprint_root="Plans/Nested")

    publish_index = events.index(("manifest-publish", None))
    before_publish = events[:publish_index]
    root_metadata = root.stat()
    plans_metadata = (root / "Plans").stat()
    assert before_publish == [
        ("directory-fsync", (root_metadata.st_dev, root_metadata.st_ino)),
        ("directory-fsync", (plans_metadata.st_dev, plans_metadata.st_ino)),
    ]


def test_workspace_init_rejects_root_replacement_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    held = tmp_path / "held-repository"
    replaced = False

    def replace_root(event: str) -> None:
        nonlocal replaced
        if event != "workspace-init-root-bound" or replaced:
            return
        root.rename(held)
        root.mkdir()
        (root / "foreign").write_text("untouched\n", encoding="utf-8")
        replaced = True

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", replace_root)

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        initialize_workspace(root, blueprint_root="Plans")

    assert replaced
    assert {path.name for path in root.iterdir()} == {"foreign"}
    assert list(held.iterdir()) == []


@pytest.mark.parametrize("operation", ["initialize", "load"])
def test_workspace_root_is_opened_one_component_at_a_time_without_following_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "nested" / "repository"
    root.mkdir(parents=True)
    if operation == "load":
        initialize_workspace(root, blueprint_root="Plans")
    original_open = workspace_reader_module.os.open
    calls: list[tuple[str, int, int | None]] = []
    recording = True

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        if recording and flags & os.O_DIRECTORY:
            calls.append((os.fspath(path), flags, dir_fd))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def stop_mutation_recording(event: str) -> None:
        nonlocal recording
        if event == "workspace-init-root-bound":
            recording = False
            workspace_reader_module.os.open = original_open

    def stop_read_recording(
        event: str,
        _binding: workspace_reader_module._WorkspaceRootBinding,
    ) -> None:
        nonlocal recording
        if event == "before-manifest-open":
            recording = False
            workspace_reader_module.os.open = original_open

    monkeypatch.setattr(workspace_reader_module.os, "open", record_open)
    if operation == "initialize":
        monkeypatch.setattr(workspace_module, "_require_workspace_mutation_support", lambda: None)
        monkeypatch.setattr(
            workspace_module,
            "_workspace_mutation_checkpoint",
            stop_mutation_recording,
        )
        initialize_workspace(root, blueprint_root="Plans")
    else:
        monkeypatch.setattr(
            workspace_reader_module,
            "_workspace_read_checkpoint",
            stop_read_recording,
        )
        load_workspace(root)

    expected_names = [root.anchor, *root.absolute().parts[1:]]
    assert [name for name, _, _ in calls] == expected_names
    assert calls[0][2] is None
    assert all(dir_fd is not None for _, _, dir_fd in calls[1:])
    assert all(flags & os.O_NOFOLLOW for _, flags, _ in calls)


def test_workspace_manifest_name_must_have_canonical_case(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    alias = root / ".AUTOFORM.TOML"
    alias.write_text(
        'schema = "autoform-workspace/v1"\n'
        '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n'
        '[projects]\n',
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="not portable"):
        initialize_workspace(root, blueprint_root="Plans")
    with pytest.raises(WorkspaceError, match="not portable"):
        load_workspace(root)
    with pytest.raises(WorkspaceError, match="not portable"):
        discover_workspace(root)

    assert {path.name for path in root.iterdir()} == {alias.name}


def test_workspace_init_rejects_case_colliding_collection(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "blueprint").mkdir()

    with pytest.raises(WorkspaceError, match="not portable"):
        initialize_workspace(root, blueprint_root="Blueprint")

    assert not (root / ".autoform.toml").exists()


def test_workspace_init_preserves_existing_unmanaged_collection_contents(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    existing = root / "Blueprint/Hartshorne"
    existing.mkdir(parents=True)
    readme = existing / "README.md"
    readme.write_text("# Existing blueprint\n", encoding="utf-8")

    initialize_workspace(root, blueprint_root="Blueprint")

    assert readme.read_text(encoding="utf-8") == "# Existing blueprint\n"
    assert discover_workspace(root).manifest.projects == ()


def test_multiple_blueprints_are_registered_without_nested_markers(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    unregistered = root / "Plans" / "ExistingWork"
    unregistered.mkdir()
    (unregistered / "README.md").write_text("# Existing\n", encoding="utf-8")

    first = create_blueprint_project(
        root,
        project_id="synthetic-homotopy",
        title="Synthetic Homotopy",
        path="SyntheticHomotopy",
    )
    second = create_blueprint_project(
        root,
        project_id="open-problem",
        title="Open Problem",
        path="OpenProblem",
    )

    assert first.blueprint_path == "Plans/SyntheticHomotopy"
    assert second.blueprint_path == "Plans/OpenProblem"
    assert first.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert second.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert (root / first.manifest_backup_path).is_file()
    assert (root / second.manifest_backup_path).is_file()
    workspace = discover_workspace(root / "Plans" / "SyntheticHomotopy" / "roadmap")
    assert [project.id for project in workspace.manifest.projects] == [
        "open-problem",
        "synthetic-homotopy",
    ]
    assert not (root / "Plans/SyntheticHomotopy/autoform.toml").exists()
    assert not (root / "Plans/OpenProblem/autoform.toml").exists()
    assert (unregistered / "README.md").read_text(encoding="utf-8") == "# Existing\n"


def test_creation_supports_a_220_byte_project_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    member = "P" * 220
    staged_names: list[str] = []

    def record_stage(
        event: str,
        _parent_descriptor: int,
        staging_name: str,
        target_name: str,
    ) -> None:
        if event == "identity-captured-before-bind" and target_name == member:
            staged_names.append(staging_name)

    monkeypatch.setattr(workspace_module, "_workspace_directory_checkpoint", record_stage)

    result = create_blueprint_project(
        root,
        project_id="long-project",
        title="Long Project",
        path=member,
    )

    assert len(member.encode()) == 220
    assert result.blueprint_path == f"Plans/{member}"
    assert (root / "Plans" / member / "roadmap/README.md").is_file()
    assert load_workspace(root).manifest.project("long-project").blueprint_path == member
    assert len(staged_names) == 1
    assert member not in staged_names[0]
    assert len(os.fsencode(staged_names[0])) < 64


def test_concurrent_blueprint_registrations_preserve_both_projects(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "autoform_cli",
                "blueprint",
                "new",
                project_id,
                "--workspace",
                str(root),
                "--path",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for project_id, path in (("one", "One"), ("two", "Two"))
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]

    assert [result[2] for result in results] == [0, 0], results
    assert {project.id for project in load_workspace(root).manifest.projects} == {"one", "two"}


def test_concurrent_case_colliding_blueprint_creation_preserves_loadable_winner(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "autoform_cli",
                "blueprint",
                "new",
                project_id,
                "--workspace",
                str(root),
                "--path",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for project_id, path in (("upper", "Example"), ("lower", "example"))
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]

    assert sorted(result[2] for result in results) == [0, 1], results
    workspace = load_workspace(root)
    assert len(workspace.manifest.projects) == 1
    assert len([path for path in (root / "Plans").iterdir() if path.name.casefold() == "example"]) == 1


def test_failed_blueprint_binding_preparation_closes_discovered_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    captured = []
    original_discover = workspace_module.discover_workspace

    def capture_workspace(start: str | Path = "."):
        workspace = original_discover(start)
        captured.append(workspace)
        return workspace

    monkeypatch.setattr(workspace_module, "discover_workspace", capture_workspace)

    with pytest.raises(WorkspaceError, match="already registered"):
        create_blueprint_project(root, project_id="example", title="Duplicate", path="Other")

    assert len(captured) == 2
    for workspace in captured:
        with pytest.raises(WorkspaceError, match="workspace root changed"):
            workspace.verify_root_binding()


def test_blueprint_creation_rejects_location_replacement_after_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    collection = root / "Plans"
    held = tmp_path / "held-plans"
    original_discover = workspace_module.discover_workspace
    discoveries = 0

    def discover_then_replace(start: str | Path = "."):
        nonlocal discoveries
        workspace = original_discover(start)
        discoveries += 1
        if discoveries == 2:
            collection.rename(held)
            collection.mkdir()
        return workspace

    monkeypatch.setattr(workspace_module, "discover_workspace", discover_then_replace)

    with pytest.raises(WorkspaceError, match="managed directory changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert not (collection / "Example").exists()
    assert not load_workspace(root).manifest.projects


def test_blueprint_registration_rejects_location_replacement_after_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    collection = root / "Plans"
    (collection / "Example/roadmap").mkdir(parents=True)
    held = tmp_path / "held-plans"
    original_discover = workspace_module.discover_workspace
    discoveries = 0

    def discover_then_replace(start: str | Path = "."):
        nonlocal discoveries
        workspace = original_discover(start)
        discoveries += 1
        if discoveries == 2:
            collection.rename(held)
            (collection / "Example/roadmap").mkdir(parents=True)
        return workspace

    monkeypatch.setattr(workspace_module, "discover_workspace", discover_then_replace)

    with pytest.raises(WorkspaceError, match="managed directory changed"):
        register_blueprint_project(
            root,
            project_id="example",
            title="Example",
            path="Example",
        )

    assert not load_workspace(root).manifest.projects


def test_registering_project_preserves_manifest_comments(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    manifest = root / ".autoform.toml"
    original = manifest.read_text(encoding="utf-8")
    manifest.write_text(original + "\n# Maintained by this repository.\n", encoding="utf-8")

    create_blueprint_project(root, project_id="example", title="Example", path="Example")

    updated = manifest.read_text(encoding="utf-8")
    assert "# Maintained by this repository." in updated
    assert '[projects."example"]' in updated
    assert 'blueprint = {location = "roadmaps", path = "Example"}' in updated


def test_registering_project_updates_an_inline_projects_table(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    (root / "Plans").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    manifest.write_text(
        '# Repository-owned comment.\n'
        'schema = "autoform-workspace/v1"\n'
        'projects = {} # Keep this representation.\n\n'
        '[locations.roadmaps]\n'
        'path = "Plans"\n'
        'provides = ["blueprints"]\n',
        encoding="utf-8",
    )

    result = create_blueprint_project(
        root,
        project_id="example",
        title="Example",
        path="Example",
    )

    assert result.blueprint_path == "Plans/Example"
    assert load_workspace(root).manifest.project("example").title == "Example"
    updated = manifest.read_text(encoding="utf-8")
    assert "# Repository-owned comment." in updated
    assert "# Keep this representation." in updated
    assert "projects = {" in updated


def test_existing_blueprint_can_be_registered_without_modification(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    (existing / "roadmap/README.md").write_text("# Existing roadmap\n", encoding="utf-8")
    before = (existing / "roadmap/README.md").read_bytes()

    result = register_blueprint_project(
        root,
        project_id="existing",
        title="Existing",
        path="Existing",
    )

    assert result.written == ()
    assert result.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert (existing / "roadmap/README.md").read_bytes() == before
    assert discover_workspace(root).manifest.project("existing").blueprint_path == "Existing"


def test_failed_creation_leaves_recovery_directory_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)

    def interrupted(target: Path, *, title: str):
        (target / "authored.md").write_text(title, encoding="utf-8")
        raise workspace_module.ScaffoldError(["injected failure"])

    monkeypatch.setattr(workspace_module, "scaffold_blueprint", interrupted)

    with pytest.raises(WorkspaceError, match="inspect the unregistered directory"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert (root / "Plans/Example/authored.md").read_text(encoding="utf-8") == "Example"
    assert discover_workspace(root).manifest.projects == ()


def test_creation_keeps_its_destination_binding_while_scaffolding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_write = scaffold_module._exclusive_write_at
    replaced = False

    def replace_destination_before_write(*args, **kwargs) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            (root / "Plans/Example").rename(root / "held-example")
            (root / "Plans/Example").mkdir()
        original_write(*args, **kwargs)

    monkeypatch.setattr(scaffold_module, "_exclusive_write_at", replace_destination_before_write)

    with pytest.raises(WorkspaceError, match="destination changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert replaced
    assert list((root / "Plans/Example").iterdir()) == []
    assert (root / "held-example/README.md").is_file()
    assert load_workspace(root).manifest.projects == ()


@pytest.mark.parametrize(
    "event",
    ["identity-captured-before-bind", "bound-before-publication"],
)
def test_creation_rejects_staged_destination_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    root = _workspace(tmp_path)
    location = root / "Plans"
    held = location / "held-example-stage"
    staged_name = ""

    def replace_stage(
        current_event: str,
        _parent_descriptor: int,
        current_staging_name: str,
        target_name: str,
    ) -> None:
        nonlocal staged_name
        if current_event != event or target_name != "Example" or staged_name:
            return
        staged_name = current_staging_name
        staged = location / staged_name
        staged.rename(held)
        (held / "owned").write_text("original\n", encoding="utf-8")
        staged.mkdir()
        (staged / "foreign").write_text("replacement\n", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_workspace_directory_checkpoint", replace_stage)

    with pytest.raises(WorkspaceError, match="blueprint destination changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert (held / "owned").read_text(encoding="utf-8") == "original\n"
    if event == "identity-captured-before-bind":
        assert not (location / "Example").exists()
        foreign = location / staged_name
    else:
        foreign = location / "Example"
    assert (foreign / "foreign").read_text(encoding="utf-8") == "replacement\n"
    assert load_workspace(root).manifest.projects == ()


def test_creation_rejects_destination_replaced_after_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_sync = workspace_module._fsync_directory_descriptor
    location = root / "Plans"
    replacement = location / "Example"
    displaced = location / "held-example"
    raced = False

    def replace_after_parent_fsync(descriptor: int) -> None:
        nonlocal raced
        original_sync(descriptor)
        opened = workspace_module.os.fstat(descriptor)
        current_location = location.stat()
        if (
            not raced
            and (opened.st_dev, opened.st_ino)
            == (current_location.st_dev, current_location.st_ino)
            and replacement.is_dir()
        ):
            replacement.rename(displaced)
            replacement.mkdir()
            raced = True

    monkeypatch.setattr(
        workspace_module,
        "_fsync_directory_descriptor",
        replace_after_parent_fsync,
    )

    with pytest.raises(WorkspaceError, match="blueprint destination changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert raced
    assert list(replacement.iterdir()) == []
    assert list(displaced.iterdir()) == []
    assert load_workspace(root).manifest.projects == ()


def test_creation_refuses_to_register_if_the_location_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original = workspace_module.scaffold_blueprint

    def move_location(target: Path, *, title: str):
        written = original(target, title=title)
        manifest = root / ".autoform.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('path = "Plans"', 'path = "Moved"'),
            encoding="utf-8",
        )
        return written

    monkeypatch.setattr(workspace_module, "scaffold_blueprint", move_location)

    with pytest.raises(WorkspaceError, match="location changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert (root / "Plans/Example").is_dir()
    assert load_workspace(root).manifest.projects == ()


def test_creation_fsyncs_destination_and_scaffold_topology_before_registry_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    events: list[tuple[str, tuple[int, int] | None]] = []
    original_sync = workspace_module.os.fsync
    original_exchange = workspace_module._rename_exchange

    def record_sync(descriptor: int) -> None:
        metadata = workspace_module.os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            events.append(("directory-fsync", (metadata.st_dev, metadata.st_ino)))
        original_sync(descriptor)

    def record_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        events.append(("registry-exchange", None))
        original_exchange(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module.os, "fsync", record_sync)
    monkeypatch.setattr(workspace_module, "_rename_exchange", record_exchange)

    create_blueprint_project(root, project_id="example", title="Example", path="Example")

    exchange_index = events.index(("registry-exchange", None))
    before_exchange = events[:exchange_index]
    location_metadata = (root / "Plans").stat()
    destination_metadata = (root / "Plans/Example").stat()
    location_identity = (location_metadata.st_dev, location_metadata.st_ino)
    destination_identity = (destination_metadata.st_dev, destination_metadata.st_ino)
    assert before_exchange.count(("directory-fsync", location_identity)) == 1
    assert before_exchange.count(("directory-fsync", destination_identity)) == 5


def test_creation_rejects_generated_file_replacement_before_registry_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_stage = workspace_module._stage_new_file
    replaced = False

    def replace_after_stage(*args, **kwargs):
        nonlocal replaced
        staged = original_stage(*args, **kwargs)
        if not replaced:
            readme = root / "Plans/Example/coverage/README.md"
            readme.rename(root / "Plans/Example/coverage/original-readme")
            readme.write_text("foreign replacement\n", encoding="utf-8")
            replaced = True
        return staged

    monkeypatch.setattr(workspace_module, "_stage_new_file", replace_after_stage)

    with pytest.raises(WorkspaceError, match="blueprint file changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert replaced
    assert (root / "Plans/Example/coverage/README.md").read_text(encoding="utf-8") == (
        "foreign replacement\n"
    )
    assert load_workspace(root).manifest.projects == ()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("content", "blueprint file changed"),
        ("file-mode", "blueprint file changed"),
        ("directory-mode", "blueprint directory changed"),
    ],
)
def test_creation_rejects_same_inode_generated_artifact_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    root = _workspace(tmp_path)
    original_stage = workspace_module._stage_new_file

    def mutate_after_stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        readme = root / "Plans/Example/coverage/README.md"
        if mutation == "content":
            descriptor = os.open(readme, os.O_RDWR | os.O_NOFOLLOW)
            try:
                os.pwrite(descriptor, b"X", 0)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif mutation == "file-mode":
            readme.chmod(0o600)
        else:
            readme.parent.chmod(0o700)
        return staged

    monkeypatch.setattr(workspace_module, "_stage_new_file", mutate_after_stage)

    with pytest.raises(WorkspaceError, match=message):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert load_workspace(root).manifest.projects == ()


def test_creation_rejects_extra_generated_entry_before_registry_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_stage = workspace_module._stage_new_file

    def add_after_stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        (root / "Plans/Example/foreign").write_text("preserve me\n", encoding="utf-8")
        return staged

    monkeypatch.setattr(workspace_module, "_stage_new_file", add_after_stage)

    with pytest.raises(WorkspaceError, match="generated blueprint entries changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert (root / "Plans/Example/foreign").read_text(encoding="utf-8") == "preserve me\n"
    assert load_workspace(root).manifest.projects == ()


def test_creation_retains_every_generated_file_descriptor_through_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_verify = workspace_module._verify_blueprint_scaffold_binding
    observed: list[frozenset[str]] = []

    def verify(
        binding: scaffold_module._BlueprintScaffoldBinding,
        *,
        exact: bool,
    ) -> None:
        for file_binding in binding.files.values():
            metadata = os.fstat(file_binding.descriptor)
            assert stat.S_ISREG(metadata.st_mode)
        observed.append(frozenset("/".join(parts) for parts in binding.files))
        original_verify(binding, exact=exact)

    monkeypatch.setattr(workspace_module, "_verify_blueprint_scaffold_binding", verify)

    create_blueprint_project(root, project_id="example", title="Example", path="Example")

    expected = {
        ".gitignore",
        "README.md",
        "coverage/README.md",
        "javascripts/mathjax.js",
        "roadmap/README.md",
        "sources/README.md",
    }
    assert observed
    assert all(paths == expected for paths in observed)


def test_creation_leaves_complete_unregistered_blueprint_on_parent_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)

    def fail(
        event: str,
        relative: str,
        _binding: scaffold_module._BlueprintScaffoldBinding,
    ) -> None:
        if event == "after-parent-fsync" and relative == "roadmap":
            raise OSError("ambiguous parent fsync result")

    monkeypatch.setattr(scaffold_module, "_scaffold_binding_checkpoint", fail)

    with pytest.raises(WorkspaceError, match="inspect the unregistered directory"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert (root / "Plans/Example/roadmap/README.md").is_file()
    assert load_workspace(root).manifest.projects == ()


@pytest.mark.parametrize("operation", ["create", "register"])
def test_project_mutation_rejects_workspace_root_replacement_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = _workspace(tmp_path)
    if operation == "register":
        (root / "Plans/Example/roadmap").mkdir(parents=True)
    held = tmp_path / "held-repository"
    replaced = False

    def replace_root(event: str) -> None:
        nonlocal replaced
        if event != "registry-before-read" or replaced:
            return
        root.rename(held)
        root.mkdir()
        (root / "foreign").write_text("untouched\n", encoding="utf-8")
        replaced = True

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", replace_root)

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        if operation == "create":
            create_blueprint_project(
                root,
                project_id="example",
                title="Example",
                path="Example",
            )
        else:
            register_blueprint_project(
                root,
                project_id="example",
                title="Example",
                path="Example",
            )

    assert replaced
    assert {item.name for item in root.iterdir()} == {"foreign"}
    assert load_workspace(held).manifest.projects == ()


@pytest.mark.parametrize("operation", ["create", "register"])
def test_project_mutation_reloads_manifest_after_acquiring_root_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = _workspace(tmp_path)
    (root / "Plans/Concurrent/roadmap").mkdir(parents=True)
    if operation == "register":
        (root / "Plans/Primary/roadmap").mkdir(parents=True)
    concurrent_registered = False

    def register_concurrent_project(event: str) -> None:
        nonlocal concurrent_registered
        if event != "project-before-root-lock" or concurrent_registered:
            return
        concurrent_registered = True
        register_blueprint_project(
            root,
            project_id="concurrent",
            title="Concurrent",
            path="Concurrent",
        )

    monkeypatch.setattr(
        workspace_module,
        "_workspace_mutation_checkpoint",
        register_concurrent_project,
    )

    if operation == "create":
        create_blueprint_project(
            root,
            project_id="primary",
            title="Primary",
            path="Primary",
        )
    else:
        register_blueprint_project(
            root,
            project_id="primary",
            title="Primary",
            path="Primary",
        )

    workspace = load_workspace(root)
    try:
        assert [project.id for project in workspace.manifest.projects] == [
            "concurrent",
            "primary",
        ]
    finally:
        workspace.close()


def test_workspace_mutation_fails_before_writing_on_an_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setattr(workspace_module, "fcntl", None)

    with pytest.raises(WorkspaceError, match="platform"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert not (root / "Plans/Example").exists()
    assert load_workspace(root).manifest.projects == ()


def test_workspace_init_fails_before_writing_on_an_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(workspace_module, "fcntl", None)

    with pytest.raises(WorkspaceError, match="platform"):
        initialize_workspace(root, blueprint_root="Plans")

    assert list(root.iterdir()) == []


def test_workspace_module_imports_without_directory_open_flags() -> None:
    script = (
        "import os\n"
        "for name in ('O_DIRECTORY', 'O_NOFOLLOW'):\n"
        "    if hasattr(os, name):\n"
        "        delattr(os, name)\n"
        "from pathlib import Path\n"
        "from autoform_cli.workspace import WorkspaceError, _open_workspace_root\n"
        "try:\n"
        "    _open_workspace_root(Path.cwd())\n"
        "except WorkspaceError as error:\n"
        "    assert 'required path safety' in str(error)\n"
        "else:\n"
        "    raise AssertionError('unsupported directory binding was accepted')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_read_only_cli_supports_portable_legacy_and_managed_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "blueprint/roadmap").mkdir(parents=True)
    (legacy / "blueprint/roadmap/README.md").write_text(
        "# Legacy roadmap\n",
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()
    workspace = _workspace(managed)
    create_blueprint_project(
        workspace,
        project_id="example",
        title="Example",
        path="Example",
    )
    monkeypatch.setattr(workspace_reader_module, "_DIRECTORY_BINDING_SUPPORTED", False)
    monkeypatch.setattr(graph_module, "_DIRECTORY_BINDING_SUPPORTED", False)

    assert main(["check", str(legacy)]) == 0
    assert main(["check", str(workspace), "--project", "example"]) == 0
    assert capsys.readouterr().out.count("OK: 1 articles") == 2

    paths = resolve_runtime_paths(workspace, project_id="example", _retain_workspace=True)
    try:
        assert not paths.strongly_bound
        with pytest.raises(RuntimeProjectionError, match="read-only inspection only"):
            paths.require_strong_binding(operation="test mutation")
    finally:
        paths.close()


def test_portable_workspace_load_rejects_a_changed_semantic_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    manifest = root / ".autoform.toml"
    changed = False

    def change_manifest(event: str, _workspace_value) -> None:
        nonlocal changed
        if event == "before-final-snapshot" and not changed:
            manifest.write_text(
                manifest.read_text(encoding="utf-8") + "\n# concurrent edit\n",
                encoding="utf-8",
            )
            changed = True

    monkeypatch.setattr(workspace_reader_module, "_DIRECTORY_BINDING_SUPPORTED", False)
    monkeypatch.setattr(
        workspace_reader_module,
        "_portable_workspace_snapshot_checkpoint",
        change_manifest,
    )

    with pytest.raises(WorkspaceError, match="changed during portable inspection"):
        load_workspace(root)

    assert changed


@pytest.mark.parametrize("blueprint_root", [".autoform.toml/vaults", ".git/autoform", ".HG/plans"])
def test_workspace_init_rejects_reserved_roots_without_writing(
    tmp_path: Path, blueprint_root: str
) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    with pytest.raises(WorkspaceError, match="reserved"):
        initialize_workspace(root, blueprint_root=blueprint_root)

    assert list(root.iterdir()) == []


def test_workspace_init_path_collision_restores_exact_tree(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    collision = root / "Control"
    collision.write_text("repository-owned\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="blueprint root"):
        initialize_workspace(root, blueprint_root="Control/Plans")

    assert {path.name for path in root.iterdir()} == {"Control"}
    assert collision.read_text(encoding="utf-8") == "repository-owned\n"


def test_workspace_init_treats_complete_manifest_publication_as_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    manifest = root / ".autoform.toml"
    raced = False
    original_unlink = Path.unlink

    def replace_before_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal raced
        if path == manifest:
            raced = True
            path.rename(root / ".autoform-owned")
            path.write_text("concurrent replacement\n", encoding="utf-8")
        original_unlink(path, *args, **kwargs)

    def fail_fsync(_path: Path) -> None:
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(Path, "unlink", replace_before_unlink)
    monkeypatch.setattr(workspace_module, "_fsync_directory", fail_fsync)
    initialize_workspace(root, blueprint_root="Plans")

    assert not raced
    assert load_workspace(root).manifest.locations[0].path == "Plans"
    assert (root / "Plans").is_dir()


def test_workspace_init_reports_directory_sync_failure_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    calls = 0

    def fail_fsync(_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory sync failure")

    monkeypatch.setattr(workspace_module, "_fsync_directory_descriptor", fail_fsync)

    with pytest.raises(WorkspaceError, match="published .autoform.toml"):
        initialize_workspace(root, blueprint_root="Plans")

    assert load_workspace(root).manifest.locations[0].path == "Plans"
    assert (root / "Plans").is_dir()


def test_workspace_init_retains_paths_instead_of_racing_directory_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    opened_components: list[int] = []
    staged_names: list[str] = []

    def fail_before_binding_nested(
        event: str,
        parent_descriptor: int,
        staging_name: str,
        target_name: str,
    ) -> None:
        if event == "identity-captured-before-bind" and target_name == "Nested":
            opened_components.append(parent_descriptor)
            staged_names.append(staging_name)
            raise workspace_module._DirectoryPublicationError("changed", staging_name)

    monkeypatch.setattr(
        workspace_module,
        "_workspace_directory_checkpoint",
        fail_before_binding_nested,
    )

    with pytest.raises(WorkspaceError, match="retained complete staged manifest"):
        initialize_workspace(root, blueprint_root="Plans/Nested")

    assert len(opened_components) == 1
    with pytest.raises(OSError):
        workspace_module.os.fstat(opened_components[0])
    assert not (root / ".autoform.toml").exists()
    assert not (root / "Plans/Nested").exists()
    assert (root / "Plans" / staged_names[0]).is_dir()
    staged, = root.glob("..autoform.toml.*.tmp")
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert parse_workspace(staged.read_text(encoding="utf-8")).locations[0].path == "Plans/Nested"


def test_workspace_init_checks_atomic_publication_support_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(workspace_module, "_atomic_noreplace_available", lambda: False)

    with pytest.raises(WorkspaceError, match="platform"):
        initialize_workspace(root, blueprint_root="Plans")

    assert list(root.iterdir()) == []


def test_workspace_init_does_not_claim_incomplete_stage_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    def fail_stage_fsync(_descriptor: int) -> None:
        raise OSError("injected stage sync failure")

    monkeypatch.setattr(workspace_module.os, "fsync", fail_stage_fsync)

    with pytest.raises(WorkspaceError) as raised:
        initialize_workspace(root, blueprint_root="Plans")

    assert "complete" not in str(raised.value)
    staged, = root.glob("..autoform.toml.*.tmp")
    assert staged.name in str(raised.value)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert not (root / ".autoform.toml").exists()
    assert not (root / "Plans").exists()


@pytest.mark.parametrize("replacement", [False, True], ids=["absent", "replaced"])
def test_workspace_init_rejects_changed_final_manifest_after_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bool,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace
    displaced = root / "published-manifest"

    def change_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        manifest = root / target
        manifest.rename(displaced)
        if replacement:
            manifest.write_text("concurrent replacement\n", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_rename_noreplace", change_after_publish)

    with pytest.raises(WorkspaceError, match="changed before initialization could continue"):
        initialize_workspace(root, blueprint_root="Plans")

    assert parse_workspace(displaced.read_text(encoding="utf-8")).locations[0].path == "Plans"
    assert (root / "Plans").is_dir()
    manifest = root / ".autoform.toml"
    if replacement:
        assert manifest.read_text(encoding="utf-8") == "concurrent replacement\n"
    else:
        assert not manifest.exists()


def test_workspace_init_rejects_same_inode_stage_mutation_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace

    def mutate_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        descriptor = workspace_module.os.open(target, workspace_module.os.O_RDWR, dir_fd=target_parent)
        try:
            workspace_module.os.pwrite(descriptor, b"X", 0)
            workspace_module.os.fsync(descriptor)
        finally:
            workspace_module.os.close(descriptor)

    monkeypatch.setattr(workspace_module, "_rename_noreplace", mutate_after_publish)

    with pytest.raises(WorkspaceError, match="published .autoform.toml changed"):
        initialize_workspace(root, blueprint_root="Plans")

    assert (root / ".autoform.toml").read_bytes().startswith(b"X")


def test_workspace_init_rejects_same_inode_mode_mutation_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace

    def chmod_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        workspace_module.os.chmod(target, 0o777, dir_fd=target_parent)

    monkeypatch.setattr(workspace_module, "_rename_noreplace", chmod_after_publish)

    with pytest.raises(WorkspaceError, match="published .autoform.toml changed"):
        initialize_workspace(root, blueprint_root="Plans")

    assert stat.S_IMODE((root / ".autoform.toml").stat().st_mode) == 0o777


def test_workspace_init_rejects_bound_directory_replaced_by_file_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace

    def replace_directory_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        (root / "Plans").rename(root / "held-plans")
        (root / "Plans").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_rename_noreplace", replace_directory_after_publish)

    with pytest.raises(WorkspaceError, match="blueprint root changed"):
        initialize_workspace(root, blueprint_root="Plans")

    assert (root / "Plans").read_text(encoding="utf-8") == "foreign\n"
    assert (root / "held-plans").is_dir()


@pytest.mark.parametrize(
    "event",
    ["identity-captured-before-bind", "bound-before-publication"],
)
def test_workspace_init_rejects_replaced_staged_nested_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    held = root / "Plans/held-nested-stage"
    staged_name = ""

    def replace_stage(
        current_event: str,
        _parent_descriptor: int,
        current_staging_name: str,
        target_name: str,
    ) -> None:
        nonlocal staged_name
        if current_event != event or target_name != "Nested" or staged_name:
            return
        staged_name = current_staging_name
        staged = root / "Plans" / staged_name
        staged.rename(held)
        (held / "owned").write_text("original\n", encoding="utf-8")
        staged.mkdir()
        (staged / "foreign").write_text("replacement\n", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_workspace_directory_checkpoint", replace_stage)

    with pytest.raises(WorkspaceError, match="blueprint root changed"):
        initialize_workspace(root, blueprint_root="Plans/Nested")

    assert not (root / ".autoform.toml").exists()
    assert (held / "owned").read_text(encoding="utf-8") == "original\n"
    if event == "identity-captured-before-bind":
        assert not (root / "Plans/Nested").exists()
        foreign = root / "Plans" / staged_name
    else:
        foreign = root / "Plans/Nested"
    assert (foreign / "foreign").read_text(encoding="utf-8") == "replacement\n"
    staged, = root.glob("..autoform.toml.*.tmp")
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert parse_workspace(staged.read_text(encoding="utf-8")).locations[0].path == "Plans/Nested"


def test_workspace_init_rejects_created_directory_replaced_after_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original_sync = workspace_module._fsync_directory_descriptor
    replacement = root / "Plans"
    displaced = root / "held-plans"
    raced = False

    def replace_after_parent_fsync(descriptor: int) -> None:
        nonlocal raced
        original_sync(descriptor)
        opened = workspace_module.os.fstat(descriptor)
        current_root = root.stat()
        if (
            not raced
            and (opened.st_dev, opened.st_ino) == (current_root.st_dev, current_root.st_ino)
            and replacement.is_dir()
        ):
            replacement.rename(displaced)
            replacement.mkdir()
            raced = True

    monkeypatch.setattr(
        workspace_module,
        "_fsync_directory_descriptor",
        replace_after_parent_fsync,
    )

    with pytest.raises(WorkspaceError, match="blueprint root changed"):
        initialize_workspace(root, blueprint_root="Plans/Nested")

    assert raced
    assert not (root / ".autoform.toml").exists()
    assert list(replacement.iterdir()) == []
    assert list(displaced.iterdir()) == []
    staged, = root.glob("..autoform.toml.*.tmp")
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600


def test_workspace_init_rejects_nested_child_replaced_after_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    plans = root / "Plans"
    nested = plans / "Nested"
    displaced = plans / "held-nested"
    original_sync = workspace_module._fsync_directory_descriptor
    raced = False

    def replace_nested_after_parent_fsync(descriptor: int) -> None:
        nonlocal raced
        original_sync(descriptor)
        if raced or not plans.is_dir() or not nested.is_dir():
            return
        opened = workspace_module.os.fstat(descriptor)
        current_plans = plans.stat()
        if (opened.st_dev, opened.st_ino) == (current_plans.st_dev, current_plans.st_ino):
            nested.rename(displaced)
            nested.mkdir()
            raced = True

    monkeypatch.setattr(
        workspace_module,
        "_fsync_directory_descriptor",
        replace_nested_after_parent_fsync,
    )

    with pytest.raises(WorkspaceError, match="blueprint root changed"):
        initialize_workspace(root, blueprint_root="Plans/Nested")

    assert raced
    assert not (root / ".autoform.toml").exists()
    assert list(nested.iterdir()) == []
    assert list(displaced.iterdir()) == []
    staged, = root.glob("..autoform.toml.*.tmp")
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600


def test_registry_exchange_rejects_an_unbound_concurrent_editor_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    original_manifest = manifest.read_bytes()
    displaced = root / "manifest-before-editor"
    foreign = b"concurrent editor replacement\n"
    original_exchange = workspace_module._rename_exchange
    raced = False

    def replace_before_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            manifest.rename(displaced)
            manifest.write_bytes(foreign)
        original_exchange(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_before_exchange)

    with pytest.raises(WorkspaceError, match="rolled back safely"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert raced
    assert manifest.read_bytes() != foreign
    assert displaced.read_bytes() == original_manifest
    retained, = root.glob("..autoform.toml.*.tmp")
    assert retained.read_bytes() == foreign


@pytest.mark.parametrize("mutation", ["bytes", "mode"])
def test_registry_exchange_rejects_same_inode_backup_mutation_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    editor_bytes = b"editor wrote through its old open descriptor\n"

    def mutate_displaced(name: str) -> None:
        if name != "registry-backup-published":
            return
        recovery, = root.glob(".autoform.toml.backup-*")
        if mutation == "bytes":
            with recovery.open("r+b", buffering=0) as stream:
                stream.seek(0)
                stream.write(editor_bytes)
                stream.truncate()
                workspace_module.os.fsync(stream.fileno())
        else:
            recovery.chmod(0o600)

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", mutate_displaced)

    with pytest.raises(WorkspaceError, match="recovery path could not be confirmed"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert load_workspace(root).manifest.project("existing").blueprint_path == "Existing"
    recovery, = root.glob(".autoform.toml.backup-*")
    if mutation == "bytes":
        assert recovery.read_bytes() == editor_bytes
    else:
        assert stat.S_IMODE(recovery.stat().st_mode) == 0o600


def test_registry_exchange_rejects_replaced_backup_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    original_manifest = (root / ".autoform.toml").read_bytes()
    held_backup = root / "held-owned-backup"
    replacement: list[Path] = []

    def replace_backup(name: str) -> None:
        if name != "registry-backup-published":
            return
        recovery, = root.glob(".autoform.toml.backup-*")
        recovery.rename(held_backup)
        recovery.write_bytes(original_manifest)
        recovery.chmod(stat.S_IMODE(held_backup.stat().st_mode))
        replacement.append(recovery)

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", replace_backup)

    with pytest.raises(WorkspaceError, match="recovery path could not be confirmed"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert load_workspace(root).manifest.project("existing").blueprint_path == "Existing"
    assert held_backup.read_bytes() == original_manifest
    assert replacement and replacement[0].read_bytes() == original_manifest
    assert replacement[0].stat().st_ino != held_backup.stat().st_ino


def test_registry_recovers_success_after_backup_publication_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    original_manifest = (root / ".autoform.toml").read_bytes()

    def fail_after_backup(name: str) -> None:
        if name == "registry-backup-published":
            raise WorkspaceError(["injected after backup publication"])

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", fail_after_backup)

    result = register_blueprint_project(
        root,
        project_id="existing",
        title="Existing",
        path="Existing",
    )

    assert load_workspace(root).manifest.project("existing").blueprint_path == "Existing"
    assert result.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert (root / result.manifest_backup_path).read_bytes() == original_manifest


def test_registry_recovery_does_not_report_a_foreign_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    original_manifest = manifest.read_bytes()
    foreign = root / ".autoform.toml.backup-0000"
    foreign.hardlink_to(manifest)

    def fail_after_backup(name: str) -> None:
        if name == "registry-backup-published":
            raise WorkspaceError(["injected after backup publication"])

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", fail_after_backup)

    result = register_blueprint_project(
        root,
        project_id="existing",
        title="Existing",
        path="Existing",
    )

    assert result.manifest_backup_path != foreign.name
    assert (root / result.manifest_backup_path).read_bytes() == original_manifest
    assert foreign.read_bytes() == original_manifest


def test_registry_rollback_rejects_a_replaced_displaced_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    original_manifest = manifest.read_bytes()
    held_prior = root / "held-prior-manifest"
    foreign = b"foreign replacement\n"
    original_exchange = workspace_module._rename_exchange
    exchanged = False

    def replace_displaced_after_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal exchanged
        original_exchange(source_parent, source, target_parent, target)
        if not exchanged:
            exchanged = True
            displaced = root / source
            displaced.rename(held_prior)
            displaced.write_bytes(foreign)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_displaced_after_exchange)

    with pytest.raises(WorkspaceError, match="rolled back safely"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert manifest.read_bytes() != foreign
    assert held_prior.read_bytes() == original_manifest


def test_registry_rollback_fsyncs_parent_after_restoring_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    original_manifest = (root / ".autoform.toml").read_bytes()
    original_exchange = workspace_module._rename_exchange
    original_sync = workspace_module._fsync_directory_descriptor
    events: list[str] = []
    exchanged = False

    def replace_roadmap_after_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal exchanged
        original_exchange(source_parent, source, target_parent, target)
        events.append("exchange")
        if not exchanged:
            exchanged = True
            roadmap = existing / "roadmap"
            roadmap.rename(root / "held-roadmap")
            roadmap.write_text("foreign\n", encoding="utf-8")

    def record_sync(descriptor: int) -> None:
        events.append("fsync")
        original_sync(descriptor)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_roadmap_after_exchange)
    monkeypatch.setattr(workspace_module, "_fsync_directory_descriptor", record_sync)

    with pytest.raises(WorkspaceError, match="roadmap changed"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert events == ["exchange", "exchange", "fsync"]
    assert (root / ".autoform.toml").read_bytes() == original_manifest


def test_registry_rollback_reports_parent_fsync_failure_after_restoring_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    original_manifest = (root / ".autoform.toml").read_bytes()
    original_exchange = workspace_module._rename_exchange
    exchanged = False

    def replace_roadmap_after_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal exchanged
        original_exchange(source_parent, source, target_parent, target)
        if not exchanged:
            exchanged = True
            roadmap = existing / "roadmap"
            roadmap.rename(root / "held-roadmap")
            roadmap.write_text("foreign\n", encoding="utf-8")

    def fail_sync(_descriptor: int) -> None:
        raise OSError("injected rollback directory sync failure")

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_roadmap_after_exchange)
    monkeypatch.setattr(workspace_module, "_fsync_directory_descriptor", fail_sync)

    with pytest.raises(WorkspaceError, match="rollback could not be committed durably"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert (root / ".autoform.toml").read_bytes() == original_manifest


def test_project_registration_rejects_location_replaced_at_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_exchange = workspace_module._rename_exchange
    replaced = False

    def replace_location_before_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            (root / "Plans").rename(root / "held-plans")
            (root / "Plans").write_text("foreign\n", encoding="utf-8")
        original_exchange(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_location_before_exchange)

    with pytest.raises(WorkspaceError, match="blueprint location changed"):
        create_blueprint_project(root, project_id="one", title="One", path="One")

    assert replaced
    assert (root / "Plans").read_text(encoding="utf-8") == "foreign\n"
    assert (root / "held-plans/One/roadmap").is_dir()
    assert load_workspace(root).manifest.projects == ()


def test_project_registration_rejects_roadmap_replaced_at_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    (root / "Plans/Existing/roadmap").mkdir(parents=True)
    original_exchange = workspace_module._rename_exchange
    replaced = False

    def replace_roadmap_before_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            roadmap = root / "Plans/Existing/roadmap"
            roadmap.rename(root / "held-roadmap")
            roadmap.write_text("foreign\n", encoding="utf-8")
        original_exchange(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_roadmap_before_exchange)

    with pytest.raises(WorkspaceError, match="roadmap changed"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert replaced
    assert (root / "Plans/Existing/roadmap").read_text(encoding="utf-8") == "foreign\n"
    assert (root / "held-roadmap").is_dir()
    assert load_workspace(root).manifest.projects == ()


def test_concurrent_workspace_init_never_exposes_partial_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace
    original_fchmod = workspace_module.os.fchmod
    ready = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0
    first_stage: list[str] = []
    restrictive_modes: list[int] = []
    errors: list[WorkspaceError] = []

    def observe_final_mode(descriptor: int, mode: int) -> None:
        if mode == 0o644:
            restrictive_modes.append(stat.S_IMODE(workspace_module.os.fstat(descriptor).st_mode))
        original_fchmod(descriptor, mode)

    def pause_first_publish(source_parent: int, source: str, target_parent: int, target: str):
        nonlocal calls
        with lock:
            calls += 1
            first = calls == 1
        if first:
            stage = root / source
            first_stage.append(source)
            assert stat.S_IMODE(stage.stat().st_mode) == 0o644
            parse_workspace(stage.read_text(encoding="utf-8"))
            ready.set()
            assert release.wait(timeout=30)
        return original(source_parent, source, target_parent, target)

    def initialize_first() -> None:
        try:
            initialize_workspace(root, blueprint_root="PlansA")
        except WorkspaceError as error:
            errors.append(error)

    monkeypatch.setattr(workspace_module.os, "fchmod", observe_final_mode)
    monkeypatch.setattr(workspace_module, "_rename_noreplace", pause_first_publish)
    thread = threading.Thread(target=initialize_first)
    thread.start()
    assert ready.wait(timeout=30)
    assert not (root / ".autoform.toml").exists()
    second_done = threading.Event()

    def initialize_second() -> None:
        try:
            initialize_workspace(root, blueprint_root="PlansB")
        except WorkspaceError as error:
            errors.append(error)
        finally:
            second_done.set()

    second = threading.Thread(target=initialize_second)
    second.start()
    assert not second_done.wait(timeout=0.1)
    release.set()
    thread.join(timeout=30)
    second.join(timeout=30)

    assert not second.is_alive()
    assert not thread.is_alive()
    assert len(errors) == 1
    assert restrictive_modes and set(restrictive_modes) == {0o600}
    assert ".autoform.toml already exists" in str(errors[0])
    assert not (root / first_stage[0]).exists()
    assert load_workspace(root).manifest.locations[0].path == "PlansA"
    assert (root / "PlansA").is_dir()
    assert not (root / "PlansB").exists()


def test_concurrent_workspace_init_preserves_winner_and_safe_loser_residue(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "autoform_cli",
                "workspace",
                "init",
                str(root),
                "--blueprint-root",
                collection,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for collection in ("PlansA", "PlansB")
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]

    assert sorted(result[2] for result in results) == [0, 1], results
    manifest = load_workspace(root).manifest
    winner = manifest.locations[0].path
    loser = "PlansB" if winner == "PlansA" else "PlansA"
    assert (root / winner).is_dir()
    if (root / loser).exists():
        assert list((root / loser).iterdir()) == []
    for staged in root.glob("..autoform.toml.*.tmp"):
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        parse_workspace(staged.read_text(encoding="utf-8"))


def test_concurrent_case_colliding_workspace_init_preserves_loadable_winner(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "autoform_cli",
                "workspace",
                "init",
                str(root),
                "--blueprint-root",
                collection,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for collection in ("Plans", "plans")
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]

    assert sorted(result[2] for result in results) == [0, 1], results
    workspace = load_workspace(root)
    winner = workspace.manifest.locations[0].path
    assert winner.casefold() == "plans"
    assert len([path for path in root.iterdir() if path.name.casefold() == "plans"]) == 1


def test_workspace_resolution_requires_a_project_at_multi_project_root(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    create_blueprint_project(root, project_id="two", title="Two", path="Two")

    with pytest.raises(RuntimeProjectionError, match="pass --project"):
        resolve_runtime_paths(root)

    selected = resolve_runtime_paths(root, project_id="two")
    assert selected.project_root == root.resolve()
    assert selected.blueprint_dir == (root / "Plans/Two").resolve()
    assert selected.workspace_project_id == "two"
    assert selected.workspace_project_binding_sha256 is not None

    inferred = resolve_runtime_paths(root / "Plans/One/roadmap")
    assert inferred.project_root == root.resolve()
    assert inferred.blueprint_dir == (root / "Plans/One").resolve()
    assert inferred.workspace_project_id == "one"
    assert (
        inferred.workspace_project_binding_sha256
        != selected.workspace_project_binding_sha256
    )


def test_runtime_resolution_never_mixes_two_workspace_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    initialize_workspace(root, blueprint_root="OldPlans", location_id="plans")
    create_blueprint_project(root, project_id="example", title="Old", path="Old")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    initialize_workspace(replacement, blueprint_root="NewPlans", location_id="plans")
    create_blueprint_project(replacement, project_id="example", title="New", path="New")
    held = tmp_path / "held-repository"
    original_resolve = runtime_module.resolve_blueprint
    replaced = False

    def replace_then_resolve(start: str | Path, *, project_id: str | None = None):
        nonlocal replaced
        if not replaced:
            root.rename(held)
            replacement.rename(root)
            replaced = True
        return original_resolve(start, project_id=project_id)

    monkeypatch.setattr(runtime_module, "resolve_blueprint", replace_then_resolve)

    resolved = resolve_runtime_paths(root, project_id="example")
    current = discover_workspace(root)
    project = current.manifest.project("example")

    assert replaced
    assert resolved.blueprint_dir == root / "NewPlans/New"
    assert resolved.workspace_project_binding_sha256 == current.project_binding_sha256(project)


def test_runtime_load_translates_workspace_replacement_to_projection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    blueprint = root / "Plans/Example"
    displaced = tmp_path / "displaced-blueprint"
    original_load = runtime_module.load_graph

    def load_then_replace(*args, **kwargs):
        graph = original_load(*args, **kwargs)
        blueprint.rename(displaced)
        blueprint.mkdir()
        (blueprint / "roadmap").mkdir()
        return graph

    monkeypatch.setattr(runtime_module, "load_graph", load_then_replace)

    with pytest.raises(RuntimeProjectionError, match="workspace root changed during use"):
        runtime_module.load_runtime_graph(root, project_id="example")


def test_project_selector_requires_a_workspace_manifest(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    (root / "blueprint/roadmap").mkdir(parents=True)

    with pytest.raises(RuntimeProjectionError, match="requires an enclosing"):
        resolve_runtime_paths(root, project_id="example")


def test_single_project_is_not_inferred_from_an_unrelated_directory(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    unrelated = root / "Plans/ExistingWork"
    unrelated.mkdir()

    selected = resolve_runtime_paths(root)
    assert selected.blueprint_dir == (root / "Plans/One").resolve()
    with pytest.raises(RuntimeProjectionError, match="pass --project"):
        resolve_runtime_paths(unrelated)


def test_doctor_resolves_a_named_workspace_project_from_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import doctor as doctor_module

    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    coverage = root / "Plans/One/coverage/README.md"
    coverage.write_text(
        "# Coverage contract\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Empty scaffold | `OUT` | No formalization targets have been selected |\n",
        encoding="utf-8",
    )
    project_roots = []
    original_build = doctor_module.build_runtime_graph

    def capture_project_root(*args, **kwargs):
        project_roots.append(kwargs["project_root"])
        return original_build(*args, **kwargs)

    monkeypatch.setattr(doctor_module, "build_runtime_graph", capture_project_root)

    result = diagnose_project(root, project_id="one")

    assert result.clean
    assert result.checks[0].detail == "resolved Plans/One"
    assert project_roots == [root]


def test_workspace_check_visits_only_registered_blueprints(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    create_blueprint_project(root, project_id="two", title="Two", path="Two")
    unregistered = root / "Plans/Unregistered"
    unregistered.mkdir()
    (unregistered / "roadmap").mkdir()
    (unregistered / "roadmap/broken.md").write_text("not a valid article", encoding="utf-8")

    assert main(["workspace", "check", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert [project["project"] for project in payload["projects"]] == ["one", "two"]


def test_workspace_check_rejects_cross_generation_project_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected_parent = tmp_path / "selected"
    selected_parent.mkdir()
    root = _workspace(selected_parent)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    replacement = _workspace(replacement_parent)
    create_blueprint_project(replacement, project_id="one", title="One", path="One")
    (replacement / "Plans/One/roadmap/extra.md").write_text(
        "# Extra\n",
        encoding="utf-8",
    )
    held = tmp_path / "held-repository"
    original_bind = workspace_cli_module.bind_runtime_paths

    @contextmanager
    def bind_replacement(*args, **kwargs):
        root.rename(held)
        replacement.rename(root)
        try:
            with original_bind(root, *args[1:], **kwargs) as paths:
                yield paths
        finally:
            root.rename(replacement)
            held.rename(root)

    monkeypatch.setattr(workspace_cli_module, "bind_runtime_paths", bind_replacement)

    assert main(["workspace", "check", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["projects"][0]["articles"] == 0
    assert payload["projects"][0]["issues"] == [
        "workspace changed while registered projects were checked"
    ]


def test_workspace_check_rejects_a_restored_project_path_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    selected = root / "Plans/One"
    substitute = tmp_path / "substitute"
    shutil.copytree(selected, substitute)
    (substitute / "roadmap/extra.md").write_text("# Extra\n", encoding="utf-8")
    held = tmp_path / "held-project"
    original_bind = workspace_cli_module.bind_runtime_paths

    @contextmanager
    def bind_substitute(*args, **kwargs):
        selected.rename(held)
        substitute.rename(selected)
        try:
            with original_bind(*args, **kwargs) as paths:
                yield paths
        finally:
            selected.rename(substitute)
            held.rename(selected)

    monkeypatch.setattr(workspace_cli_module, "bind_runtime_paths", bind_substitute)

    assert main(["workspace", "check", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["projects"][0]["articles"] == 0
    assert payload["projects"][0]["issues"] == [
        "managed directory changed during use: Plans/One"
    ]


def test_workspace_check_refuses_to_succeed_without_registered_projects(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)

    assert main(["workspace", "check", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["projects"] == []
    assert [item["code"] for item in payload["diagnostics"]] == ["projects-empty"]


def test_workspace_check_with_lean_root_rejects_missing_declarations(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    (root / "Plans/One/roadmap/missing.md").write_text(
        "---\ndeclaration: theorem\nlean: Definitely.Missing\n---\n\n# Missing\n",
        encoding="utf-8",
    )

    assert main(["workspace", "check", str(root), "--lean-root", str(root)]) == 1
    assert "declaration not found: Definitely.Missing" in capsys.readouterr().out


def test_workspace_check_reports_unsafe_lean_tree_without_traceback(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    outside = tmp_path / "Outside.lean"
    outside.write_text("def outside : Nat := 0\n", encoding="utf-8")
    try:
        (root / "Linked.lean").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert main(["workspace", "check", str(root), "--lean-root", str(root)]) == 1
    assert "invalid-lean-root" in capsys.readouterr().err


def test_workspace_check_rejects_roadmap_symlinks_like_single_project_check(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "Plans/One/roadmap/external").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert main(["workspace", "check", str(root)]) == 1
    assert "external: roadmap paths must not be symbolic links" in capsys.readouterr().out


def test_workspace_check_labels_nonfatal_diagnostics_as_warnings(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    manifest = root / ".autoform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[locations.optional]\npath = "Missing"\nprovides = ["lean-source"]\n',
        encoding="utf-8",
    )

    assert main(["workspace", "check", str(root)]) == 0
    captured = capsys.readouterr()
    assert "warning: location-missing Missing" in captured.out
    assert "error: location-missing" not in captured.err


def test_workspace_inspection_reports_a_missing_registered_blueprint(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    for child in sorted((root / "Plans/Example").rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
        else:
            child.rmdir()
    (root / "Plans/Example").rmdir()

    result = inspect_workspace(root)

    assert not result.ok
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["blueprint-missing"]


@pytest.mark.parametrize(
    "path",
    [
        "../Escape",
        "Nested/Blueprint",
        "/absolute",
        "C:/absolute",
        "back\\slash",
        "bad*name",
        "CON",
        "trailing.",
        "trailing ",
        ".",
    ],
)
def test_blueprint_member_must_be_one_portable_child(tmp_path: Path, path: str) -> None:
    root = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="immediate child"):
        create_blueprint_project(root, project_id="example", title="Example", path=path)

    assert parse_workspace((root / ".autoform.toml").read_text(encoding="utf-8")).projects == ()


def test_workspace_cli_creates_lists_and_checks_projects(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    assert main(["workspace", "init", str(root), "--blueprint-root", "Roadmaps"]) == 0
    capsys.readouterr()
    assert main(
        [
            "blueprint",
            "new",
            "finite-flat",
            "--workspace",
            str(root),
            "--path",
            "FiniteFlat",
            "--title",
            "Finite Flat",
        ]
    ) == 0
    capsys.readouterr()
    assert main(["blueprint", "list", str(root), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == {
        "ok": True,
        "projects": [
            {"id": "finite-flat", "path": "Roadmaps/FiniteFlat", "title": "Finite Flat"}
        ],
        "schema": BLUEPRINT_LIST_SCHEMA,
    }
    assert main(["check", str(root), "--project", "finite-flat"]) == 0
    assert "OK: 1 articles, 0 dependencies" in capsys.readouterr().out
    assert visualize_main([str(root), "--project", "finite-flat", "--structure"]) == 0
    capsys.readouterr()
    assert (root / "Roadmaps/FiniteFlat/dependencies.md").is_file()
    assert (root / "Roadmaps/FiniteFlat/structure.md").is_file()


def test_workspace_render_defaults_to_project_scoped_output(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    create_blueprint_project(root, project_id="two", title="Two", path="Two")
    monkeypatch.chdir(root)

    assert main(["render", ".", "--project", "one"]) == 0
    capsys.readouterr()
    assert main(["render", ".", "--project", "two"]) == 0
    capsys.readouterr()

    assert (root / "site-src/one/README.md").is_file()
    assert (root / "site-src/two/README.md").is_file()


def test_manifest_rejects_case_colliding_blueprint_paths() -> None:
    with pytest.raises(WorkspaceError, match="same path"):
        parse_workspace(
            'schema = "autoform-workspace/v1"\n'
            "[locations.plans]\n"
            'path = "Blueprint"\n'
            'provides = ["blueprints"]\n'
            "[projects.one]\n"
            'blueprint = { location = "plans", path = "Example" }\n'
            "[projects.two]\n"
            'blueprint = { location = "plans", path = "example" }\n'
        )


def test_manifest_rejects_nested_managed_blueprint_paths() -> None:
    with pytest.raises(WorkspaceError, match="overlaps"):
        parse_workspace(
            'schema = "autoform-workspace/v1"\n'
            "[locations.outer]\n"
            'path = "Blueprint"\n'
            'provides = ["blueprints"]\n'
            "[locations.inner]\n"
            'path = "Blueprint/Outer"\n'
            'provides = ["blueprints"]\n'
            "[projects.outer]\n"
            'blueprint = { location = "outer", path = "Outer" }\n'
            "[projects.inner]\n"
            'blueprint = { location = "inner", path = "Inner" }\n'
        )


def test_manifest_rejects_portably_duplicate_location_paths() -> None:
    with pytest.raises(WorkspaceError, match="same portable path"):
        parse_workspace(
            'schema = "autoform-workspace/v1"\n'
            "[locations.first]\n"
            'path = "Blueprint"\n'
            'provides = ["blueprints"]\n'
            "[locations.second]\n"
            'path = "blueprint"\n'
            'provides = ["blueprints"]\n'
            "[projects]\n"
        )


@pytest.mark.parametrize(
    "path",
    ["C:/Blueprint", "bad*name", "CON", "AUX.txt", "trailing.", " leading", "trailing ", "bad\x7fname"],
)
def test_workspace_paths_are_portable_to_windows(tmp_path: Path, path: str) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    with pytest.raises(WorkspaceError, match="portable"):
        initialize_workspace(root, blueprint_root=path)

    assert not (root / ".autoform.toml").exists()


def test_workspace_project_id_is_safe_as_a_publication_directory(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="project id is not portable"):
        create_blueprint_project(root, project_id="CON", title="Reserved")

    assert list((root / "Plans").iterdir()) == []
    assert load_workspace(root).manifest.projects == ()


def test_workspace_json_results_have_operation_specific_schemas(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    assert main(["workspace", "init", str(root), "--blueprint-root", "Plans", "--json"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["ok"] is True
    assert initialized["schema"] == WORKSPACE_INIT_SCHEMA

    assert main(
        [
            "blueprint",
            "new",
            "example",
            "--workspace",
            str(root),
            "--path",
            "Example",
            "--json",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["ok"] is True
    assert created["schema"] == BLUEPRINT_CHANGE_SCHEMA
    assert created["manifest_backup_path"].startswith(".autoform.toml.backup-")

    assert main(["blueprint", "new", "example", "--workspace", str(root), "--json"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["ok"] is False
    assert failed["schema"] == WORKSPACE_ERROR_SCHEMA


def test_creation_rejects_case_collision_with_unregistered_sibling(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "Plans/ExistingWork").mkdir()

    with pytest.raises(WorkspaceError, match="not portable"):
        create_blueprint_project(
            root,
            project_id="example",
            title="Example",
            path="existingwork",
        )

    assert {path.name for path in (root / "Plans").iterdir()} == {"ExistingWork"}
    assert discover_workspace(root).manifest.projects == ()


def test_loading_rejects_case_colliding_registered_and_unregistered_paths(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    if (root / "Plans/example").exists():
        pytest.skip("case-only sibling names cannot coexist on this filesystem")
    (root / "Plans/example").mkdir()

    with pytest.raises(WorkspaceError, match="not portable"):
        discover_workspace(root)


def test_manifest_rejects_a_symlinked_managed_path(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "Plans").symlink_to(outside, target_is_directory=True)
    (root / ".autoform.toml").write_text(
        'schema = "autoform-workspace/v1"\n'
        "[locations.plans]\n"
        'path = "Plans"\n'
        'provides = ["blueprints"]\n'
        "[projects]\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="symbolic link"):
        discover_workspace(root)


def test_nonregular_manifest_blocks_outer_workspace_discovery(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    initialize_workspace(outer, blueprint_root="Blueprint")
    nested = outer / "nested"
    nested.mkdir()
    (nested / ".autoform.toml").mkdir()

    with pytest.raises(WorkspaceError, match="regular file"):
        discover_workspace(nested)


def test_discovery_rejects_workspace_root_replacement_after_manifest_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    held_root = tmp_path / "held-repository"
    replaced = False

    def replace_root(
        event: str,
        _binding: workspace_reader_module._WorkspaceRootBinding,
    ) -> None:
        nonlocal replaced
        if event != "manifest-found" or replaced:
            return
        root.rename(held_root)
        root.mkdir()
        (root / ".autoform.toml").write_text(
            'schema = "autoform-workspace/v1"\n'
            '[locations.replacement]\npath = "Replacement"\nprovides = ["blueprints"]\n'
            '[projects]\n',
            encoding="utf-8",
        )
        (root / "Replacement").mkdir()
        replaced = True

    monkeypatch.setattr(
        workspace_reader_module,
        "_workspace_discovery_checkpoint",
        replace_root,
    )

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        discover_workspace(root)

    assert replaced
    assert load_workspace(held_root).manifest.locations[0].path == "Plans"
    assert load_workspace(root).manifest.locations[0].path == "Replacement"


def test_discovery_retains_start_generation_across_ancestor_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = False
    original_close = workspace_reader_module._WorkspaceRootBinding.close

    def move_start_then_close(binding: workspace_reader_module._WorkspaceRootBinding) -> None:
        nonlocal moved
        if binding.path == nested and not moved:
            nested.rename(outside / nested.name)
            moved = True
        original_close(binding)

    monkeypatch.setattr(
        workspace_reader_module._WorkspaceRootBinding,
        "close",
        move_start_then_close,
    )

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        discover_workspace(nested)

    assert moved
    assert not nested.exists()
    assert (outside / nested.name).is_dir()


def test_discovery_retains_start_generation_while_ancestor_manifest_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = False

    def move_start(
        event: str,
        _binding: workspace_reader_module._WorkspaceRootBinding,
    ) -> None:
        nonlocal moved
        if event != "before-manifest-open" or moved:
            return
        nested.rename(outside / nested.name)
        moved = True

    monkeypatch.setattr(workspace_reader_module, "_workspace_read_checkpoint", move_start)

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        discover_workspace(nested)

    assert moved
    assert not nested.exists()
    assert (outside / nested.name).is_dir()


def test_discovery_does_not_bind_unregistered_start_after_success(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    nested = root / "scratch"
    nested.mkdir()
    workspace = discover_workspace(nested)
    moved = tmp_path / "moved-scratch"

    nested.rename(moved)

    workspace.verify_root_binding()
    assert workspace.root == root


@pytest.mark.parametrize("named", [False, True])
def test_blueprint_resolution_rejects_root_replacement_after_path_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    named: bool,
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    held_root = tmp_path / "held-repository"
    original_require = workspace_reader_module._require_blueprint
    replaced = False

    def replace_root_after_validation(path: Path) -> None:
        nonlocal replaced
        original_require(path)
        if replaced:
            return
        root.rename(held_root)
        (root / "Plans/Example/roadmap").mkdir(parents=True)
        (root / ".autoform.toml").write_text(
            'schema = "autoform-workspace/v1"\n'
            '[locations.roadmaps]\npath = "Plans"\nprovides = ["blueprints"]\n'
            '[projects.example]\ntitle = "Replacement"\n'
            'blueprint = {location = "roadmaps", path = "Example"}\n',
            encoding="utf-8",
        )
        replaced = True

    monkeypatch.setattr(workspace_reader_module, "_require_blueprint", replace_root_after_validation)
    start = root if named else root / "Plans/Example/roadmap"

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        workspace_reader_module.resolve_blueprint(
            start,
            project_id="example" if named else None,
        )

    assert replaced
    assert (held_root / "Plans/Example/roadmap").is_dir()


@pytest.mark.parametrize("named", [False, True])
def test_blueprint_resolution_rejects_registered_directory_replacement_after_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    named: bool,
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    blueprint = root / "Plans/Example"
    held = root / "Plans/held-example"
    original_discover = workspace_reader_module.discover_workspace
    replaced = False

    def discover_then_replace(start: str | Path = "."):
        nonlocal replaced
        workspace = original_discover(start)
        if not replaced:
            blueprint.rename(held)
            (blueprint / "roadmap").mkdir(parents=True)
            replaced = True
        return workspace

    monkeypatch.setattr(
        workspace_reader_module,
        "discover_workspace",
        discover_then_replace,
    )
    start = root if named else root / "Plans/Example/roadmap"

    with pytest.raises(WorkspaceError, match="managed directory changed"):
        workspace_reader_module.resolve_blueprint(
            start,
            project_id="example" if named else None,
        )

    assert replaced
    assert (held / "roadmap").is_dir()


def test_workspace_inspection_rejects_root_replacement_before_path_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    held_root = tmp_path / "held-repository"
    replaced = False

    def replace_root(event: str, _workspace_value: object) -> None:
        nonlocal replaced
        if event != "before-path-inspection" or replaced:
            return
        root.rename(held_root)
        root.mkdir()
        (root / ".autoform.toml").write_text(
            'schema = "autoform-workspace/v1"\n'
            '[locations.replacement]\npath = "Replacement"\nprovides = ["blueprints"]\n'
            '[projects]\n',
            encoding="utf-8",
        )
        (root / "Replacement").mkdir()
        replaced = True

    monkeypatch.setattr(
        workspace_reader_module,
        "_workspace_inspection_checkpoint",
        replace_root,
    )

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        inspect_workspace(root)

    assert replaced
    assert load_workspace(held_root).manifest.locations[0].path == "Plans"


def test_workspace_inspection_rejects_registered_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    blueprint = root / "Plans/Example"
    held = root / "Plans/held-example"
    replaced = False

    def replace_blueprint(event: str, _workspace_value: object) -> None:
        nonlocal replaced
        if event != "before-path-inspection" or replaced:
            return
        blueprint.rename(held)
        (blueprint / "roadmap").mkdir(parents=True)
        replaced = True

    monkeypatch.setattr(
        workspace_reader_module,
        "_workspace_inspection_checkpoint",
        replace_blueprint,
    )

    with pytest.raises(WorkspaceError, match="managed directory changed"):
        inspect_workspace(root)

    assert replaced
    assert (held / "roadmap").is_dir()


def test_workspace_inspection_distinguishes_missing_directory_from_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    blueprint = root / "Plans/Example"
    held = tmp_path / "held-example"
    blueprint.rename(held)
    outside = tmp_path / "outside"
    (outside / "roadmap").mkdir(parents=True)
    linked = False

    def install_symlink(event: str, _workspace_value: object) -> None:
        nonlocal linked
        if event != "before-path-inspection" or linked:
            return
        try:
            blueprint.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable")
        linked = True

    monkeypatch.setattr(
        workspace_reader_module,
        "_workspace_inspection_checkpoint",
        install_symlink,
    )

    with pytest.raises(WorkspaceError, match="managed directory changed"):
        inspect_workspace(root)

    assert linked
    assert (held / "roadmap").is_dir()


def test_workspace_manifest_read_is_size_bounded(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    manifest = root / ".autoform.toml"
    with manifest.open("wb") as stream:
        stream.truncate(MAX_MANIFEST_BYTES + 1)

    with pytest.raises(WorkspaceError, match="byte limit"):
        load_workspace(root)


def test_workspace_manifest_read_rejects_same_name_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    held_root = tmp_path / "held-repository"
    replacement_manifest = b'repository = "replacement"\n'
    original_read = workspace_reader_module.os.read
    replaced = False

    def replace_root_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        content = original_read(descriptor, size)
        if content and not replaced:
            replaced = True
            root.rename(held_root)
            root.mkdir()
            (root / ".autoform.toml").write_bytes(replacement_manifest)
        return content

    monkeypatch.setattr(workspace_reader_module.os, "read", replace_root_after_read)

    with pytest.raises(WorkspaceError, match="cannot read .autoform.toml safely"):
        load_workspace(root)

    assert replaced
    assert (root / ".autoform.toml").read_bytes() == replacement_manifest
    assert load_workspace(held_root).manifest.locations[0].path == "Plans"


def test_workspace_manifest_read_rejects_root_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    held_root = tmp_path / "held-repository"
    replacement_manifest = b'repository = "replacement"\n'
    replaced = False

    def replace_root(
        event: str,
        _binding: workspace_reader_module._WorkspaceRootBinding,
    ) -> None:
        nonlocal replaced
        if event != "before-manifest-open" or replaced:
            return
        root.rename(held_root)
        root.mkdir()
        (root / ".autoform.toml").write_bytes(replacement_manifest)
        replaced = True

    monkeypatch.setattr(workspace_reader_module, "_workspace_read_checkpoint", replace_root)

    with pytest.raises(WorkspaceError, match="cannot read .autoform.toml safely"):
        load_workspace(root)

    assert replaced
    assert (root / ".autoform.toml").read_bytes() == replacement_manifest
    assert load_workspace(held_root).manifest.locations[0].path == "Plans"


def test_loaded_workspace_retains_and_revalidates_its_root_descriptor(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    workspace = load_workspace(root)
    expected = root.stat()
    held_root = tmp_path / "held-repository"

    root.rename(held_root)
    root.mkdir()
    (root / ".autoform.toml").write_text('repository = "replacement"\n', encoding="utf-8")

    opened = os.fstat(workspace.root_descriptor)
    assert (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
    with pytest.raises(WorkspaceError, match="workspace root changed"):
        workspace.verify_root_binding()
    assert (root / ".autoform.toml").read_text(encoding="utf-8") == (
        'repository = "replacement"\n'
    )


def test_loaded_workspace_rejects_in_place_manifest_change(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    workspace = load_workspace(root)
    manifest = root / ".autoform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n# changed after selection\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        workspace.verify_root_binding()

    workspace.close()


def test_portable_workspace_rejects_manifest_change_after_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setattr(workspace_reader_module, "_DIRECTORY_BINDING_SUPPORTED", False)
    workspace = load_workspace(root)
    manifest = root / ".autoform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n# changed after selection\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="workspace root changed"):
        workspace.verify_root_binding()

    workspace.close()
