"""What the work loop knows about one project: its profile.

A profile names the task source, the checks the harness runs before it pushes,
the pull-request conventions and the review bot to follow. It is read, in
order, from ``--profile FILE``, from ``.autoform/work.toml`` in the checkout,
from the profile Autoform ships for the repository, and otherwise the generic
defaults apply. ``reference/work-loop.md`` documents every key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomli

PROFILE_SCHEMA = "autoform-work-profile/v1"
PROJECT_PROFILE = Path(".autoform") / "work.toml"
_BUILT_IN = Path(__file__).resolve().parent / "profiles"
_PHASES = ("statement", "proof")
_SECTIONS: dict[str, frozenset[str]] = {
    "tasks": frozenset({"command", "tasks_option", "taken_module", "phases", "claims_repo"}),
    "verify": frozenset({"commands", "publish_check", "publish_check_public", "statement_fidelity", "forbidden"}),
    "pull_request": frozenset({"branch_prefix", "body_lines", "blueprint"}),
    "review": frozenset({"bot", "markers", "checks"}),
    "agent": frozenset({"instructions"}),
}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Profile:
    """One project's conventions. ``source`` says where the profile came from."""

    source: str = "generic defaults"
    task_command: tuple[str, ...] | None = None
    tasks_option: str | None = None
    taken_module: str | None = None
    phases: tuple[str, ...] = _PHASES
    claims_repo: str | None = None
    verify_commands: tuple[tuple[str, ...], ...] = ()
    publish_check: tuple[str, ...] | None = None
    publish_check_public: str | None = None
    statement_fidelity: bool = True
    forbidden: tuple[str, ...] = ()
    branch_prefix: str = "autoform-work/"
    body_lines: tuple[str, ...] = ()
    blueprint: str = "blueprint"
    review_bot: str | None = None
    review_markers: tuple[str, ...] = ()
    review_checks: tuple[str, ...] = ()
    instructions: str = ""


def _strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{where} must be a list of strings")
    return tuple(value)


def _command(value: object, where: str) -> tuple[str, ...]:
    command = _strings(value, where)
    if not command or not command[0].strip():
        raise ProfileError(f"{where} must name a program")
    return command


def parse_profile(text: str, *, source: str, base_dir: Path | None = None) -> Profile:
    """Parse a profile's TOML; ``base_dir`` resolves ``[agent] instructions``, a file path relative to it."""

    try:
        data = tomli.loads(text)
    except tomli.TOMLDecodeError as error:
        raise ProfileError(f"{source}: not valid TOML: {error}") from None
    if data.get("schema") != PROFILE_SCHEMA:
        raise ProfileError(f"{source}: set schema = {PROFILE_SCHEMA!r}")
    unknown = sorted(set(data) - set(_SECTIONS) - {"schema"})
    if unknown:
        raise ProfileError(f"{source}: unknown table or key {', '.join(unknown)}")
    for section, keys in _SECTIONS.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise ProfileError(f"{source}: [{section}] must be a table")
        extra = sorted(set(table) - keys)
        if extra:
            raise ProfileError(f"{source}: unknown key in [{section}]: {', '.join(extra)}")
    tasks, verify = data.get("tasks", {}), data.get("verify", {})
    pull_request, review, agent = data.get("pull_request", {}), data.get("review", {}), data.get("agent", {})

    def optional_string(table: dict, key: str, where: str) -> str | None:
        value = table.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(f"{source}: {where} must be a non-empty string")
        return value

    phases = _strings(tasks.get("phases", list(_PHASES)), f"{source}: [tasks] phases")
    if not phases or any(phase not in _PHASES for phase in phases):
        raise ProfileError(f"{source}: [tasks] phases must list 'statement' and/or 'proof'")
    fidelity = verify.get("statement_fidelity", True)
    if not isinstance(fidelity, bool):
        raise ProfileError(f"{source}: [verify] statement_fidelity must be true or false")
    commands = verify.get("commands", [])
    if not isinstance(commands, list):
        raise ProfileError(f"{source}: [verify] commands must be a list of commands")
    prefix = pull_request.get("branch_prefix", "autoform-work/")
    if not isinstance(prefix, str) or not prefix.endswith("/") or prefix.startswith("/") or ".." in prefix:
        raise ProfileError(f"{source}: [pull_request] branch_prefix must be a relative ref prefix ending in '/'")
    instructions = ""
    if (name := optional_string(agent, "instructions", "[agent] instructions")) is not None:
        if base_dir is None:
            raise ProfileError(f"{source}: [agent] instructions needs the profile's directory")
        path = (base_dir / name).resolve()
        if not path.is_relative_to(base_dir.resolve()):
            raise ProfileError(f"{source}: [agent] instructions must stay in the profile's directory")
        try:
            instructions = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProfileError(f"{source}: cannot read [agent] instructions {name}: {error}") from None
    task_command = tasks.get("command")
    publish_check = verify.get("publish_check")
    return Profile(
        source=source,
        task_command=None if task_command is None else _command(task_command, f"{source}: [tasks] command"),
        tasks_option=optional_string(tasks, "tasks_option", "[tasks] tasks_option"),
        taken_module=optional_string(tasks, "taken_module", "[tasks] taken_module"),
        phases=phases,
        claims_repo=optional_string(tasks, "claims_repo", "[tasks] claims_repo"),
        verify_commands=tuple(
            _command(command, f"{source}: [verify] commands[{index}]") for index, command in enumerate(commands)
        ),
        publish_check=None if publish_check is None else _command(publish_check, f"{source}: [verify] publish_check"),
        publish_check_public=optional_string(verify, "publish_check_public", "[verify] publish_check_public"),
        statement_fidelity=fidelity,
        forbidden=_strings(verify.get("forbidden", []), f"{source}: [verify] forbidden"),
        branch_prefix=prefix,
        body_lines=_strings(pull_request.get("body_lines", []), f"{source}: [pull_request] body_lines"),
        blueprint=optional_string(pull_request, "blueprint", "[pull_request] blueprint") or "blueprint",
        review_bot=optional_string(review, "bot", "[review] bot"),
        review_markers=_strings(review.get("markers", []), f"{source}: [review] markers"),
        review_checks=_strings(review.get("checks", []), f"{source}: [review] checks"),
        instructions=instructions,
    )


def built_in_profile_path(repository: str) -> Path | None:
    """The profile Autoform ships for ``owner/name``, if any."""

    candidate = _BUILT_IN / (repository.lower().replace("/", "__") + ".toml")
    return candidate if candidate.is_file() else None


def load_profile(checkout: Path, repository: str, explicit: Path | None = None) -> Profile:
    """The first profile found: ``explicit``, the checkout's, Autoform's for ``repository``, else the defaults."""

    for path, label in (
        (explicit, None),
        (checkout / PROJECT_PROFILE, None),
        (built_in_profile_path(repository), "built-in profile for " + repository),
    ):
        if path is None:
            continue
        if path is explicit or path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as error:
                raise ProfileError(f"cannot read profile {path}: {error}") from None
            return parse_profile(text, source=label or str(path), base_dir=path.parent)
    return Profile()
