"""Bridge scheduler work items to the backend-neutral prover execution layer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import re

from autoform_cli.lean import SourceIndex, index_project
from autoform_cli.runtime import RuntimeNode, load_runtime_graph
from servers.prover import ProofResult, ProverAdapter
from servers.prover.claude_adapter import ClaudeAdapter
from servers.prover.codex_adapter import CodexAdapter
from servers.prover.driver import prove
from servers.prover.muse_adapter import MuseAdapter
from servers.prover.verify import Baseline, _declaration_bounds, restore_baseline
from servers.lean_client import LeanRuntimeClient, LeanRuntimeError

from .scheduler import AttemptResult, CancellationSignal, WorkItem, WorkPhase

AdapterFactory = Callable[[], ProverAdapter]

_IGNORED_PARTS = frozenset(
    {".git", ".hg", ".lake", ".sl", ".venv", "__pycache__", "build", "lake-packages"}
)
_DIAGNOSTIC_SUMMARY = re.compile(r"^Diagnostics: (\d+) error\(s\), (\d+) warning\(s\)(?:\n|$)")


def backend_factory(name: str, *, timeout: float = 30 * 60.0) -> AdapterFactory:
    """Return a fresh dependency-free CLI adapter for ``name``."""

    normalized = name.strip().casefold()
    factories: dict[str, AdapterFactory] = {
        "claude": lambda: ClaudeAdapter(max_wait_seconds=timeout),
        "codex": lambda: CodexAdapter(max_wait_seconds=timeout),
        "muse": lambda: MuseAdapter(max_wait_seconds=timeout),
    }
    try:
        return factories[normalized]
    except KeyError as error:
        choices = ", ".join(sorted(factories))
        raise ValueError(f"unknown backend {name!r}; expected one of: {choices}") from error


class ProverExecutor:
    """Execute statement or proof work without committing or pushing changes."""

    def __init__(
        self,
        project_dir: str | Path,
        adapter_factory: AdapterFactory,
        *,
        max_steers: int = 3,
    ) -> None:
        if max_steers < 0:
            raise ValueError("max_steers must be nonnegative")
        self.project_dir = Path(project_dir).expanduser().resolve()
        self._adapter_factory = adapter_factory
        self.max_steers = max_steers

    def __call__(self, item: WorkItem, cancelled: CancellationSignal) -> AttemptResult:
        adapter = self._adapter_factory()
        prompt = _work_prompt(item)
        if item.phase is WorkPhase.PROOF:
            result = prove(
                adapter,
                item.node,
                prompt,
                str(self.project_dir),
                max_steers=self.max_steers,
                cancel_event=cancelled,
            )
            if not result.proved:
                return _attempt_result(result)
            return self._verify_proof_transition(item)
        return self._execute_statement(adapter, item, prompt, cancelled)

    def _execute_statement(
        self,
        adapter: ProverAdapter,
        item: WorkItem,
        prompt: str,
        cancelled: CancellationSignal,
    ) -> AttemptResult:
        """Run a statement-authoring turn and verify it through a fresh projection."""

        if cancelled.is_set():
            return AttemptResult.cancelled("statement run cancelled before launch")

        baseline = _capture_statement_baseline(self.project_dir)
        baseline_index = index_project(self.project_dir)
        article_path, article_content = _capture_article(self.project_dir, item.node.article_path)
        article_candidate: bytes | None = None
        keep_changes = False
        try:
            adapter.bind_cancel_event(cancelled)
            run = adapter.start(item.node.id, prompt, str(self.project_dir))
            events = iter(adapter.events(run))
            try:
                for _event in events:
                    if cancelled.is_set():
                        return AttemptResult.cancelled("statement run cancelled")
            finally:
                close = getattr(events, "close", None)
                if callable(close):
                    close()
            backend_result = adapter.result(run)
            if not backend_result.proved:
                return _attempt_result(backend_result)

            refreshed = load_runtime_graph(self.project_dir, lean_root=self.project_dir)
            node = refreshed.get(item.node.id)
            if node is None:
                return AttemptResult.failed("statement run removed its roadmap node")
            if not node.status.stated:
                return AttemptResult.retry(
                    "backend claimed statement completion, but the Markdown runtime still reports it unstated"
                )
            transition_error = _statement_transition_error(
                item.node,
                node,
                baseline,
                baseline_index,
                self.project_dir,
            )
            if transition_error:
                return AttemptResult.retry(
                    f"backend claimed statement completion, but changed work outside the selected statement: "
                    f"{transition_error}"
                )
            verification_error = _verify_statement(node, self.project_dir)
            if verification_error:
                return AttemptResult.retry(
                    f"backend claimed statement completion, but Lean verification failed: {verification_error}"
                )
            keep_changes = True
            return AttemptResult.succeeded(
                "statement formalization verified by a fresh runtime and compiled Lean declaration"
            )
        finally:
            if not keep_changes:
                _observe_statement_candidates(baseline)
                try:
                    article_candidate = article_path.read_bytes()
                except OSError:
                    article_candidate = None
                restore_baseline(baseline)
                _restore_article(article_path, article_content, article_candidate)

    def _verify_proof_transition(self, item: WorkItem) -> AttemptResult:
        """Require a proved backend result to advance the authoritative runtime."""

        if item.node.status.proved:
            return AttemptResult.failed("proof work item was already proved before execution")
        refreshed = load_runtime_graph(self.project_dir, lean_root=self.project_dir)
        node = refreshed.get(item.node.id)
        if node is None:
            return AttemptResult.failed("proof run removed its roadmap node")
        if not node.status.proved:
            return AttemptResult.retry(
                "backend proved the Lean target, but the authoritative runtime still reports it unproved"
            )
        transition_error = _proof_transition_error(item.node, node)
        if transition_error:
            return AttemptResult.failed(
                f"proof run changed metadata outside the selected proof transition: {transition_error}"
            )
        return AttemptResult.succeeded("proof verified by an authoritative runtime transition to proved")


def _preserved_metadata_error(before: RuntimeNode, after: RuntimeNode) -> str:
    preserved = (
        "article_path",
        "declaration",
        "lean_targets",
        "statement_dependencies",
        "proof_dependencies",
        "dependencies",
    )
    changed = [field for field in preserved if getattr(before, field) != getattr(after, field)]
    return f"changed target metadata: {changed}" if changed else ""


def _proof_transition_error(before: RuntimeNode, after: RuntimeNode) -> str:
    metadata_error = _preserved_metadata_error(before, after)
    if metadata_error:
        return metadata_error
    if before.assertions.proof_formalized or not after.assertions.proof_formalized:
        return "proof_formalized did not transition from false to true"
    return ""


def _statement_transition_error(
    before: RuntimeNode,
    after: RuntimeNode,
    baseline: Baseline,
    baseline_index: SourceIndex,
    project_dir: Path,
) -> str:
    metadata_error = _preserved_metadata_error(
        before,
        replace(after, lean_targets=before.lean_targets),
    )
    if metadata_error:
        return metadata_error
    if before.assertions.statement_formalized or not after.assertions.statement_formalized:
        return "statement_formalized did not transition from false to true"

    current = _capture_statement_baseline(project_dir).files
    target_files = {target.source_file for target in after.lean_targets if target.source_file}
    allowed_files = {*target_files, before.article_path}
    protected_changes = sorted(
        relative
        for relative in current.keys() | baseline.files.keys()
        if relative not in allowed_files and current.get(relative) != baseline.files.get(relative)
    )
    if protected_changes:
        return f"changed non-target Lean/config inputs: {protected_changes}"

    article_error = _article_transition_error(
        baseline.files.get(before.article_path),
        current.get(before.article_path),
    )
    if article_error:
        return article_error

    current_index = index_project(project_dir)
    claimed = {target.declaration for target in after.lean_targets}
    added = set(current_index.declarations) - set(baseline_index.declarations)
    removed = set(baseline_index.declarations) - set(current_index.declarations)
    if added != claimed or removed:
        return f"declaration delta does not match claimed targets (added={sorted(added)}, removed={sorted(removed)})"

    for relative in sorted(target_files):
        candidate = current.get(relative)
        if candidate is None:
            return f"Lean target disappeared: {relative}"
        original = baseline.files.get(relative, b"")
        try:
            lines = candidate.decode("utf-8").splitlines(keepends=True)
            ranges = sorted(
                (
                    _declaration_bounds(project_dir, target.declaration, relative, index=current_index)
                    for target in after.lean_targets
                    if target.source_file == relative
                ),
                reverse=True,
            )
        except (OSError, UnicodeError, ValueError) as error:
            return str(error)
        for start, end in ranges:
            del lines[start:end]
        if "".join(lines).encode("utf-8") != original:
            return f"changed bytes outside claimed declarations in target file: {relative}"
    return ""


def _article_transition_error(before: bytes | None, after: bytes | None) -> str:
    if before is None or after is None:
        return "selected roadmap article disappeared"
    try:
        before_projection = _article_without_statement_fields(before)
        after_projection = _article_without_statement_fields(after)
    except UnicodeError as error:
        return f"selected roadmap article is not UTF-8: {error}"
    except ValueError as error:
        return str(error)
    if before_projection != after_projection:
        return "selected roadmap article changed outside statement/lean frontmatter"
    return ""


def _article_without_statement_fields(content: bytes) -> tuple[tuple[str, ...], str]:
    text = content.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return (), text
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as error:
        raise ValueError("selected roadmap article has unterminated frontmatter") from error
    preserved = tuple(
        line
        for line in lines[1:end]
        if line.split(":", 1)[0].strip() not in {"lean", "statement"}
    )
    return preserved, "".join(lines[end + 1 :])


def _capture_statement_baseline(project_dir: Path) -> Baseline:
    """Snapshot project files whose mutation could escape a statement attempt."""

    files: dict[str, bytes] = {}
    for path in sorted(project_dir.rglob("*")):
        relative = path.relative_to(project_dir)
        if _IGNORED_PARTS.intersection(relative.parts) or not path.is_file() or path.is_symlink():
            continue
        files[relative.as_posix()] = path.read_bytes()
    return Baseline(root=project_dir, files=files)


def _observe_statement_candidates(baseline: Baseline) -> None:
    """Record every changed project file for compare-and-swap rollback."""

    current = _capture_statement_baseline(baseline.root).files
    baseline.observed_candidates.clear()
    for relative in current.keys() | baseline.files.keys():
        candidate = current.get(relative)
        if candidate != baseline.files.get(relative):
            baseline.observed_candidates[relative] = candidate


def _capture_article(project_dir: Path, article: str) -> tuple[Path, bytes]:
    path = (project_dir / article).resolve()
    try:
        path.relative_to(project_dir)
    except ValueError as error:
        raise ValueError(f"roadmap article escapes the project root: {article}") from error
    return path, path.read_bytes()


def _restore_article(path: Path, content: bytes, observed: bytes | None) -> None:
    """Restore the article only while it still contains attempt-observed bytes."""

    try:
        current = path.read_bytes()
    except OSError:
        current = None
    if current != observed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _diagnostics_are_clean(value: object) -> bool:
    if value == "No diagnostics — file compiles cleanly.":
        return True
    if not isinstance(value, str):
        return False
    summary = _DIAGNOSTIC_SUMMARY.match(value)
    return summary is not None and int(summary.group(1)) == 0


def _verify_statement(node: RuntimeNode, project_dir: Path) -> str:
    """Return an error unless every authored declaration resolves and compiles."""

    targets = list(node.lean_targets)
    if not targets or any(not target.source_file for target in targets):
        return f"runtime node has no resolvable local Lean declaration: {node.id}"
    try:
        index = index_project(project_dir)
    except (OSError, UnicodeError, ValueError) as error:
        return f"could not index Lean project: {error}"

    files: list[str] = []
    for target in targets:
        declaration = index.find(target.declaration)
        if declaration is None or declaration.path.as_posix() != target.source_file:
            return f"target declaration does not resolve in {target.source_file}: {target.declaration}"
        if target.source_file not in files:
            files.append(target.source_file)

    client = LeanRuntimeClient()
    for source_file in files:
        try:
            diagnostics = client.request(
                "lsp.diagnostics",
                {"project_dir": str(project_dir), "file_path": source_file},
            )
        except LeanRuntimeError as error:
            return f"Lean verification failed for {source_file}: {error}"
        if not _diagnostics_are_clean(diagnostics):
            return f"Lean diagnostics were not a recognized clean result for {source_file}: {diagnostics!r}"
    return ""


def _work_prompt(item: WorkItem) -> str:
    node = item.node
    lean_targets = ", ".join(
        target.source_file or target.declaration for target in node.lean_targets
    ) or "not authored yet"
    dependencies = ", ".join(node.dependencies) or "none"
    action = (
        "Formalize and compile the declaration statement. Update the roadmap article's "
        "statement metadata only after Lean accepts it."
        if item.phase is WorkPhase.STATEMENT
        else "Complete the Lean proof without changing the declaration statement."
    )
    return "\n".join(
        (
            f"Autoform work item: {node.id}",
            f"Phase: {item.phase.value}",
            f"Roadmap article: {node.article_path}",
            f"Declaration intent: {node.declaration or 'unspecified'}",
            f"Lean targets: {lean_targets}",
            f"Dependencies: {dependencies}",
            "",
            action,
            "Use the shared Lean tools to verify every edit.",
            "Do not commit, push, open a pull request, alter setup/roadmap structure, or edit website output.",
            "Report success only after the authored project state proves the phase is complete.",
        )
    )


def _attempt_result(result: ProofResult) -> AttemptResult:
    if result.meta.get("sub_status") == "cancelled":
        return AttemptResult.cancelled(result.reason or "backend run cancelled")
    if result.proved:
        return AttemptResult.succeeded(result.reason or "backend result independently verified")
    if result.meta.get("sub_status") in {"backend_error", "timeout"}:
        return AttemptResult.retry(result.reason or "transient backend failure")
    return AttemptResult.failed(result.reason or "backend could not complete the work item")


__all__ = ["AdapterFactory", "ProverExecutor", "backend_factory"]
