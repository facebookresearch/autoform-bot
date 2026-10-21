"""Opt-in subagent debrief: the form an agent fills in after a formalization attempt.

The agent that worked a leaf is the only witness to what the proof cost: the
finished Lean records what it wrote, not what it searched for and could not
find, nor what it tried and abandoned. Once the attempt is over (its result
integrated and its claim released, or the leaf abandoned), the same agent asks
``autoform debrief subagent form`` for the form, answers it from its own
session, and stores the answer with ``autoform debrief subagent record``.
Recording cannot change the attempt: the outcome is read from the roadmap
runtime, not from the agent.

This module also owns the store that :mod:`autoform_cli.debrief_orchestrator`
shares for the per-run orchestrator debrief.

Records are bounded and normalised to the form's schema, and created
exclusively without following symbolic links, so a record is complete or
absent and never overwritten.

Store layout (see :mod:`autoform_cli.debrief_store` for the file discipline)::

    records/<attempt_id>.json   one DebriefRecord per attempt, schema_version 1
    views/                      Markdown rendered from validated records (derived)

:func:`load_records` is the typed read API for downstream consumers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import debrief_store as store
from ._repository import git
from .runtime import RuntimeNode, load_runtime_graph, resolve_runtime_paths

SCHEMA_VERSION = 1
SUBAGENT_DEBRIEF_ENV = "AUTOFORM_SUBAGENT_DEBRIEF"
DEBRIEF_DIR_ENV = "AUTOFORM_DEBRIEF_DIR"

RECORDS, VIEWS = "records", "views"
PHASES = frozenset({"statement", "proof"})
OUTCOMES = frozenset({"succeeded", "not-succeeded"})

MAX_ANSWER_BYTES = 256 * 1024
MAX_ITEMS = 20
MAX_TEXT = 600
MAX_GOAL = 2000
MAX_NOTE = 2000

DEFAULT_QUESTION = """This attempt is finished and its outcome is already recorded in the roadmap —
**nothing you say now changes it**, and there is nothing left to fix. This is a
separate question about tooling, not about the proof.

You have just worked on `{node_id}`. Outcome: `{outcome}`.

We maintain project proof infrastructure — helper lemmas, simp and Aesop rules,
wrapper tactics, imports, and search/tooling support — and want to know what to
add or improve. You are the only witness to what this proof actually cost: the
finished file records what you wrote, not what you searched for and could not
find, nor what you tried and abandoned.

Answer with a single JSON object and no other text. Do not wrap it in a Markdown code fence.

{
  "friction": [
    {
      "file": "path/to/File.lean",
      "line": 111,
      "lines": 7,
      "what": "one sentence: what this block establishes",
      "why_hard": "lemma-search | lemma-name-guessing | repeated-boilerplate | side-goal-plumbing | rewrite-plumbing | other",
      "goal": "example ... := by sorry   (best effort, need not compile)"
    }
  ],
  "searched_for": [
    {"wanted": "what missing fact or infrastructure you needed",
     "searched": "the grep/rg terms or Mathlib names you tried",
     "why_hard": "missing-infrastructure | repeated-failed-search | misleading-name | inaccessible-import | complex-workaround | other",
     "found": false, "resolution": "what you eventually found or had to do instead"}
  ],
  "infrastructure_proposals": [
    {
      "kind": "tactic-idea | helper-lemma | simp-rule | aesop-rule | wrapper-tactic | import-export | search-tooling | other",
      "name": "snake_case_slug or null",
      "what": "one sentence: the reusable infrastructure or rough automation idea that might help",
      "evidence": "friction 1 | searched_for 2 | other"
    }
  ],
  "copied_code": [
    {
      "kind": "private-declaration | declaration | inline",
      "source_file": "path/to/File/It/Came/From.lean",
      "source_declaration": "its Lean name there, or null for code inside a proof",
      "copy_file": "path/to/Your/File.lean",
      "copy_declaration": "the Lean name of your copy, or null if you inlined it",
      "what": "one sentence: what the copied code states or does"
    }
  ],
  "nothing_to_report": false
}

Rules for `friction`:
- Only blocks of **5 or more lines** where you knew what to do and it still
  took that long. Skip short steps where you named the right lemma first time.
- Skip blocks that were hard *mathematically*. Infrastructure can remove routine
  proof work; it cannot replace the argument.
- Friction is worth recording even when you do not know the right improvement.

Rules for `infrastructure_proposals`:
- A proposal may be any reusable infrastructure improvement. It need not
  correspond to a `friction` block. `evidence` is provenance, not a claim that
  the proposal is known to work; list every relevant entry rather than
  duplicating a proposal.
- Speculative tactic ideas are welcome: a suggestive name plus the proof pattern
  it might automate is enough.
- A `helper-lemma` should describe its approximate statement and where it would
  be reused. An `import-export` proposal should name the missing module.
- An empty `infrastructure_proposals` array is perfectly valid.

Rules for `copied_code`:
- List every piece of this project's own code you copied into your work,
  verbatim or adapted (renamed, specialised, reformatted), because you could not
  use it where it was: a `private` declaration (`private-declaration`), a
  declaration in a module you could not import (`declaration`), or steps inside
  another proof (`inline`). Mathlib is not this project's code.
- This is a record of duplication that exists now, not a wish: the lead raises
  each copied piece into one shared declaration and points every copy at it.
  A lemma you would have liked in a more general form, or that does not exist
  yet, is a `helper-lemma` in `infrastructure_proposals` instead.
- Give file paths relative to the project root, and the names as written in
  the source, without `private`.

Rules for `searched_for`:
- This is a friction report, **not a history of searches**. Include an entry
  only when needed infrastructure was absent or inaccessible, several searches
  or name guesses failed, or the search ended in a materially more complex
  workaround. Omit lookups that succeeded on the first attempt.

If you have no useful notes, that is a complete and valid answer: return
`"nothing_to_report": true` with every array empty. Do not invent entries to
fill the schema. Copied code is never nothing to report: list it."""

_ATTEMPT_ID = re.compile(r"\A[0-9a-f]{32}\Z")


class DebriefError(ValueError):
    """The debrief cannot be produced or recorded as asked."""


class RecordError(ValueError):
    """A stored record is not a valid record of the current schema."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """Subagent debriefs opted in, on a platform that can store the answer."""
    opted_in = (os.environ.get(SUBAGENT_DEBRIEF_ENV) or "").strip().lower() in {"1", "true", "yes"}
    return opted_in and store.directory_binding.DIRECTORY_BINDING_SUPPORTED


def _git_dir(project: Path, flag: str) -> Path | None:
    output = git(project, "rev-parse", "--path-format=absolute", flag)
    path = Path(output) if output else None
    return path.resolve() if path is not None and path.is_absolute() and path.is_dir() else None


def project_dir(project_or_blueprint: str | Path) -> Path:
    """The project root; a directory without a roadmap yet (as during Setup) is the project itself."""
    candidate = Path(project_or_blueprint).expanduser().resolve()
    if (candidate / "roadmap").is_dir() or (candidate / "blueprint" / "roadmap").is_dir():
        return resolve_runtime_paths(project_or_blueprint).project_root.resolve()
    if candidate.is_dir():
        return candidate
    raise DebriefError(f"project directory does not exist: {candidate}")


def debrief_root(project_or_blueprint: str | Path) -> Path:
    """Resolve the store directory, which must not lie in the working tree.

    Git's common directory is the exception: it is inside a main checkout but
    untracked, and shared by every worktree, so all agents of one repository
    record into the same store.
    """
    project = project_dir(project_or_blueprint)
    common = _git_dir(project, "--git-common-dir")
    top = _git_dir(project, "--show-toplevel")
    configured = (os.environ.get(DEBRIEF_DIR_ENV) or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise DebriefError(f"{DEBRIEF_DIR_ENV} must be an absolute path: {configured!r}")
    elif common is not None:
        path = common / "autoform" / "debriefs"
    else:
        state = Path(os.environ.get("XDG_STATE_HOME") or "")
        if not state.is_absolute():
            state = Path.home() / ".local" / "state"
        digest = hashlib.sha256(str(project).encode("utf-8", "surrogateescape")).hexdigest()[:12]
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-.") or "project"
        path = state / "autoform" / "debriefs" / f"{name}-{digest}"
    root = path.resolve()
    tree = top or project
    in_tree = root == tree or tree in root.parents
    in_git = common is not None and (root == common or common in root.parents)
    if in_tree and not in_git:
        raise DebriefError(f"debrief store {root} must be outside the working tree {tree}")
    return root


def build_question(node_id: str, outcome: str) -> str:
    return DEFAULT_QUESTION.replace("{node_id}", node_id).replace("{outcome}", outcome)


# ---------------------------------------------------------------------------
# Normalisation: every stored string is bounded and encodable
# ---------------------------------------------------------------------------


def _clean(text: str, limit: int) -> str:
    text = text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return _clean(str(value).strip(), limit)
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        encoded = type(value).__name__
    return _clean(encoded, limit)


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        # isdigit() also accepts digits that int() rejects, such as "²".
        value = int(value) if value.isascii() and value.isdigit() and len(value) <= 8 else None
    if isinstance(value, int) and 0 <= value <= 10**7:
        return value
    return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:MAX_ITEMS]


# ---------------------------------------------------------------------------
# Typed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Friction:
    file: str
    line: int | None
    lines: int | None
    what: str
    why_hard: str
    goal: str


@dataclass(frozen=True, slots=True)
class SearchReport:
    wanted: str
    searched: str
    why_hard: str
    found: bool | None
    resolution: str


@dataclass(frozen=True, slots=True)
class Proposal:
    kind: str
    name: str
    what: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CopiedCode:
    kind: str
    source_file: str
    source_declaration: str
    copy_file: str
    copy_declaration: str
    what: str


@dataclass(frozen=True, slots=True)
class DebriefAnswer:
    """The agent's answer, normalised to the form's schema and bounded."""

    friction: tuple[Friction, ...] = ()
    searched_for: tuple[SearchReport, ...] = ()
    infrastructure_proposals: tuple[Proposal, ...] = ()
    copied_code: tuple[CopiedCode, ...] = ()
    nothing_to_report: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DebriefAnswer:
        """Normalise an answer, fresh or stored; unknown keys are dropped."""
        return cls(
            friction=tuple(
                Friction(
                    file=_text(item.get("file"), 512),
                    line=_count(item.get("line")),
                    lines=_count(item.get("lines")),
                    what=_text(item.get("what")),
                    why_hard=_text(item.get("why_hard"), 64),
                    goal=_text(item.get("goal"), MAX_GOAL),
                )
                for item in _items(data.get("friction"))
            ),
            searched_for=tuple(
                SearchReport(
                    wanted=_text(item.get("wanted")),
                    searched=_text(item.get("searched")),
                    why_hard=_text(item.get("why_hard"), 64),
                    found=_flag(item.get("found")),
                    resolution=_text(item.get("resolution")),
                )
                for item in _items(data.get("searched_for"))
            ),
            infrastructure_proposals=tuple(
                Proposal(
                    kind=_text(item.get("kind"), 64),
                    name=_text(item.get("name"), 128),
                    what=_text(item.get("what")),
                    evidence=_text(item.get("evidence")),
                )
                for item in _items(data.get("infrastructure_proposals"))
            ),
            copied_code=tuple(
                CopiedCode(
                    kind=_text(item.get("kind"), 64),
                    source_file=_text(item.get("source_file"), 512),
                    source_declaration=_text(item.get("source_declaration"), 512),
                    copy_file=_text(item.get("copy_file"), 512),
                    copy_declaration=_text(item.get("copy_declaration"), 512),
                    what=_text(item.get("what")),
                )
                for item in _items(data.get("copied_code"))
            ),
            nothing_to_report=data.get("nothing_to_report") is True,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "friction": [_fields(item) for item in self.friction],
            "searched_for": [_fields(item) for item in self.searched_for],
            "infrastructure_proposals": [_fields(item) for item in self.infrastructure_proposals],
            "copied_code": [_fields(item) for item in self.copied_code],
            "nothing_to_report": self.nothing_to_report,
        }


@dataclass(frozen=True, slots=True)
class DebriefRecord:
    """One finished attempt's debrief, with its outcome as the roadmap runtime reported it."""

    attempt_id: str
    node_id: str
    article_id: str
    article_revision: str
    source_revision: str
    phase: str
    outcome: str
    note: str
    worker_id: str
    recorded_at: str
    answer: DebriefAnswer

    def __post_init__(self) -> None:
        if not _ATTEMPT_ID.match(self.attempt_id):
            raise RecordError(f"attempt_id must be 32 lowercase hex digits: {self.attempt_id!r}")
        if self.phase not in PHASES:
            raise RecordError(f"unknown phase: {self.phase!r}")
        if self.outcome not in OUTCOMES:
            raise RecordError(f"unknown outcome: {self.outcome!r}")

    @property
    def outcome_label(self) -> str:
        return _outcome_label(self.phase, self.outcome, self.note)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            **{name: getattr(self, name) for name in self.__slots__ if name != "answer"},
            "answer": self.answer.to_json(),
        }

    @classmethod
    def from_json(cls, data: Any) -> DebriefRecord:
        if not isinstance(data, dict):
            raise RecordError("record must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise RecordError(f"unsupported schema_version: {data.get('schema_version')!r}")
        answer = data.get("answer")
        if not isinstance(answer, dict):
            raise RecordError("answer must be an object")
        limits = {"note": MAX_NOTE, "node_id": 256, "worker_id": 128}
        fields = {}
        for name in cls.__slots__:
            if name == "answer":
                continue
            value = data.get(name)
            if not isinstance(value, str):
                raise RecordError(f"{name} must be a string")
            fields[name] = _text(value, limits.get(name, 128))
        return cls(**fields, answer=DebriefAnswer.from_json(answer))


def _outcome_label(phase: str, outcome: str, note: str) -> str:
    if outcome == "succeeded":
        return f"{phase} formalized"
    return f"{phase} not formalized: {note or 'no reason given'}"


def _fields(item: Any) -> dict[str, Any]:
    return {name: getattr(item, name) for name in item.__slots__}


def _encode(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode("ascii")


def _decode(data: bytes) -> Any:
    try:
        return json.loads(data.decode("ascii"))
    # ValueError covers JSONDecodeError, UnicodeDecodeError and over-long integers.
    except (ValueError, RecursionError) as error:
        raise RecordError(f"not an ASCII JSON record: {error}") from error


def parse_answer(text: str) -> dict[str, Any]:
    """Parse a JSON-object answer."""
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise DebriefError(f"the answer is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise DebriefError("the answer must be a single JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Form and record
# ---------------------------------------------------------------------------


def _select(project_or_blueprint: str | Path, selector: str):
    runtime = load_runtime_graph(project_or_blueprint)
    matches = [
        node
        for node in runtime.nodes
        if node.id == selector or (node.article_id is not None and node.article_id == selector)
    ]
    if len(matches) != 1:
        raise DebriefError(f"{selector!r} must match exactly one article; it matches {len(matches)}")
    return runtime.source_revision, matches[0]


def _worktree_roots(project: Path) -> list[Path]:
    """The project and every worktree of its repository, deepest first."""
    roots = {project}
    for line in (git(project, "worktree", "list", "--porcelain") or "").splitlines():
        if line.startswith("worktree "):
            roots.add(Path(line.removeprefix("worktree ")).resolve())
    return sorted(roots, key=lambda root: len(root.parts), reverse=True)


def _project_path(path: str, roots: list[Path]) -> str:
    """A reported path relative to its worktree, so copies reported from different worktrees match.

    Slot worktrees may live inside the main checkout, so the deepest worktree
    that holds an absolute path is the one it is relative to.
    """
    if not path:
        return path
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix().removeprefix("./")
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return path
    for root in roots:
        if root in resolved.parents:
            return resolved.relative_to(root).as_posix()
    return path


def _outcome(node: RuntimeNode, phase: str) -> str:
    if phase not in PHASES:
        raise DebriefError(f"phase must be one of {sorted(PHASES)}: {phase!r}")
    done = node.status.proved if phase == "proof" else node.status.stated
    return "succeeded" if done else "not-succeeded"


def form(
    project_or_blueprint: str | Path,
    selector: str,
    *,
    phase: str,
    note: str = "",
) -> str:
    """The form for one finished attempt, with the runtime's outcome filled in."""
    _, node = _select(project_or_blueprint, selector)
    # Article names may hold control characters; keep them from forging form lines.
    shown = "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in node.id)
    return build_question(shown, _outcome_label(phase, _outcome(node, phase), _text(note, MAX_NOTE)))


def record(
    project_or_blueprint: str | Path,
    selector: str,
    answer_text: str,
    *,
    phase: str,
    worker_id: str | None,
    note: str = "",
) -> tuple[DebriefRecord, Path]:
    """Validate an answer and store it as a new record; return it and its path."""
    if not (worker_id or "").strip():
        raise DebriefError("--worker-id or AUTOFORM_WORKER_ID is required")
    if len(answer_text.encode("utf-8", "surrogatepass")) > MAX_ANSWER_BYTES:
        raise DebriefError(f"the answer exceeds {MAX_ANSWER_BYTES} bytes")
    answer = DebriefAnswer.from_json(parse_answer(answer_text))
    source_revision, node = _select(project_or_blueprint, selector)
    if answer.copied_code:
        roots = _worktree_roots(project_dir(project_or_blueprint))
        answer = replace(
            answer,
            copied_code=tuple(
                replace(
                    item,
                    source_file=_project_path(item.source_file, roots),
                    copy_file=_project_path(item.copy_file, roots),
                )
                for item in answer.copied_code
            ),
        )
    outcome = _outcome(node, phase)
    entry = DebriefRecord(
        attempt_id=uuid.uuid4().hex,
        node_id=_text(node.id, 256),
        article_id=node.article_id or "",
        article_revision=node.source_sha256 or "",
        source_revision=source_revision,
        phase=phase,
        outcome=outcome,
        note=_text(note, MAX_NOTE),
        worker_id=_text(worker_id, 128),
        recorded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        answer=answer,
    )
    # ASCII escaping can triple the answer's size (each astral character becomes 12 bytes).
    data = _encode(entry.to_json())
    if len(data) > store.MAX_FILE_BYTES:
        raise DebriefError("the answer is too large to store; shorten it")
    root = debrief_root(project_or_blueprint)
    name = f"{entry.attempt_id}.json"
    with store.open_root(root) as root_fd, store.open_subdir(root_fd, RECORDS) as records:
        store.create_exclusive(records, name, data)
    return entry, root / RECORDS / name


def load_records(root: Path) -> list[DebriefRecord]:
    """Every valid record, oldest first. Invalid or mismatched files are skipped."""
    records: list[DebriefRecord] = []
    with store.open_root(root) as root_fd, store.open_subdir(root_fd, RECORDS) as records_fd:
        for name in store.list_names(records_fd, ".json"):
            try:
                entry = DebriefRecord.from_json(_decode(store.read_bounded(records_fd, name)))
            except (OSError, RecordError):
                continue
            if f"{entry.attempt_id}.json" == name:
                records.append(entry)
    return sorted(records, key=lambda entry: (entry.recorded_at, entry.attempt_id))


# ---------------------------------------------------------------------------
# Derived Markdown views
# ---------------------------------------------------------------------------

_MARKDOWN_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")


def _md(value: Any, empty: str = "Not reported") -> str:
    """Agent text as one inert Markdown line: every ASCII punctuation mark is escaped."""
    text = " ".join(str(value).split()) if value is not None and value != "" else empty
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", text)


def _code_block(text: str, language: str = "") -> list[str]:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", text, fence]


def render_record_markdown(entry: DebriefRecord) -> str:
    answer = entry.answer
    lines = [
        f"# Proof debrief: {_md(entry.node_id)}",
        "",
        f"- **Outcome:** {_md(entry.outcome_label)}",
        f"- **Article revision:** {_md(entry.article_revision)}",
        f"- **Recorded at:** {_md(entry.recorded_at)}",
        f"- **Worker:** {_md(entry.worker_id)}",
        "",
        "## At a glance",
        "",
        f"- Friction blocks: {len(answer.friction)}",
        f"- Search reports: {len(answer.searched_for)}",
        f"- Infrastructure proposals: {len(answer.infrastructure_proposals)}",
        f"- Copied code: {len(answer.copied_code)}",
        f"- Nothing to report: {'yes' if answer.nothing_to_report else 'no'}",
        "",
        "## Infrastructure proposals",
        "",
    ]
    if not answer.infrastructure_proposals:
        lines += ["No infrastructure proposals were reported.", ""]
    for index, proposal in enumerate(answer.infrastructure_proposals, 1):
        lines += [
            f"### {index}. {_md(proposal.name or proposal.kind, 'Infrastructure improvement')}",
            "",
            f"- **Kind:** {_md(proposal.kind)}",
            f"- **Improvement:** {_md(proposal.what)}",
            f"- **Evidence:** {_md(proposal.evidence)}",
            "",
        ]
    lines += ["## Copied code", ""]
    if not answer.copied_code:
        lines += ["No copied code was reported.", ""]
    for index, item in enumerate(answer.copied_code, 1):
        lines += [
            f"### {index}. {_md(item.copy_declaration or item.what, 'Copied code')}",
            "",
            f"- **Kind:** {_md(item.kind)}",
            f"- **Copied from:** {_md(f'{item.source_file} {item.source_declaration}'.strip())}",
            f"- **Copy:** {_md(f'{item.copy_file} {item.copy_declaration}'.strip())}",
            f"- **What:** {_md(item.what)}",
            "",
        ]
    lines += ["## Mechanical friction", ""]
    if not answer.friction:
        lines += ["No qualifying mechanical blocks were reported.", ""]
    for index, item in enumerate(answer.friction, 1):
        location = f"{item.file or '?'}:{item.line if item.line is not None else '?'}"
        lines += [
            f"### {index}. {_md(item.what, 'Reported block')}",
            "",
            f"- **Location:** {_md(location)}",
            f"- **Block size:** {item.lines if item.lines is not None else '?'} lines",
            f"- **Difficulty:** {_md(item.why_hard)}",
            "",
        ]
        if item.goal:
            lines += ["**Goal sketch:**", "", *_code_block(item.goal, "lean"), ""]
    lines += ["## Searches and missing infrastructure", ""]
    if not answer.searched_for:
        lines += ["No searches were reported.", ""]
    for index, item in enumerate(answer.searched_for, 1):
        found = "unknown" if item.found is None else ("yes" if item.found else "no")
        lines += [
            f"### {index}. {_md(item.wanted, 'Search')}",
            "",
            f"- **Found:** {found}",
            f"- **Difficulty:** {_md(item.why_hard)}",
            f"- **Searched:** {_md(item.searched)}",
            f"- **Resolution:** {_md(item.resolution)}",
            "",
        ]
    return "\n".join(lines)


def render(root: Path) -> list[Path]:
    """Rebuild ``<root>/views`` from validated records; return the report paths."""
    out = root / VIEWS
    entries = load_records(root)
    written: list[Path] = []
    index = ["# Proof debriefs", "", "Rendered from validated debrief records. Oldest first.", ""]
    with store.open_root(out) as out_fd, store.open_subdir(out_fd, "reports") as reports:
        for entry in entries:
            name = f"{entry.attempt_id}.md"
            store.replace_atomic(reports, name, render_record_markdown(entry).encode("utf-8"))
            written.append(out / "reports" / name)
            index.append(f"- [{_md(entry.node_id)} — {_md(entry.outcome_label)}](reports/{name})")
        for stale in set(store.list_names(reports, ".md")) - {f"{entry.attempt_id}.md" for entry in entries}:
            store.remove(reports, stale)
        if not entries:
            index.append("No debrief records found.")
        store.replace_atomic(out_fd, "README.md", ("\n".join(index) + "\n").encode("utf-8"))
    return written


__all__ = [
    "CopiedCode",
    "DebriefAnswer",
    "DebriefError",
    "DebriefRecord",
    "Friction",
    "Proposal",
    "RecordError",
    "SearchReport",
    "build_question",
    "debrief_root",
    "enabled",
    "form",
    "load_records",
    "record",
    "render",
]
