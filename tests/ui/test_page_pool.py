"""Render and check read node pages in workers exactly as their loop would."""

from __future__ import annotations

import contextlib
import hashlib
import json
import multiprocessing
import os
import random
import select
import signal
import subprocess
import sys
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

import autoform_cli.ui._page_pool as page_pool
import autoform_cli.ui.render as render_module
from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.ui.publishable import publishable_article
from autoform_cli.ui.render import _read_page, publication_issues, render_site
from tests.facts_fixture import write_annotated
from tests.ui.test_render import _project

_ROOT = Path(__file__).resolve().parents[2]
_DEEP = "the page nests lists deeper than the site's Markdown converter can read"


def _nested(levels: int) -> str:
    # The converter recurses about four frames for each level.
    return "".join("    " * depth + "- level\n" for depth in range(levels))


_BODIES = [
    "Let $f(x) = x^2$ on $\\mathbb{R}$, so $\\newcommand{\\sq}{x^2} \\sq \\geq 0$.",
    "A <b>bold</b> claim the site shows as typed.",
    "See [the base](../base.md) and [the paper](../../sources.md).",
    "A claim with a footnote.[^1] And a [reference][paper].\n\n[^1]: The note.\n\n[paper]: https://example.org/p",
    "```python\nprint({'a': 1})\n```\n\nThen *emphasis* and `code {x}`.",
    "A claim with a footnote.[^1] And a [reference][paper].\n\n[^1]: The note.\n\n[paper]: https://example.org/p",
    "Text with an attribute list.\n{: .wide }\n",
    "| a | b |\n| --- | --- |\n| $1$ | $2$ |",
]


def _vault(
    tmp_path: Path, *, chapters: int = 2, leaves: int = 5, extra: dict[str, str] | None = None, drafts: bool = True
) -> Path:
    """A blueprint whose pages hold each kind of text the reader must give
    back as the loop reads it: formulas and notation, raw HTML, a link into
    an unpublished folder, references, fences, identical pages, chapter
    narratives with stretches between their results, and a page that is no node.
    Unless ``drafts`` is False, a leaf links into the unpublished folder,
    which render allows and check refuses."""

    project = _project(tmp_path)
    blueprint = project / "blueprint"
    (blueprint / "sources.md").write_text("# Paper\n", encoding="utf-8")
    (blueprint / "notes.md").write_text("---\nkind: note\n---\n\n# Notes\n\nA <i>note</i>.\n", encoding="utf-8")
    roadmap = blueprint / "roadmap"
    unpublished = roadmap / "drafts"
    unpublished.mkdir()
    (unpublished / "README.md").write_text("---\npublished: false\n---\n\n# Drafts\n", encoding="utf-8")
    (unpublished / "idea.md").write_text("# Idea\n\nAn idea.\n", encoding="utf-8")
    bodies = dict(extra or {})
    if drafts:
        bodies.setdefault("chapter-0-leaf-2", "See [the idea](../drafts/idea.md), which is not published.")
    listed_chapters = []
    for c in range(chapters):
        chapter = f"chapter-{c}"
        listed_chapters.append(f"- [Chapter {c}]({chapter}/README.md)\n")
        folder = roadmap / chapter
        folder.mkdir()
        listed = []
        for k in range(leaves):
            name = f"{chapter}-leaf-{k}"
            body = bodies.pop(name, _BODIES[(c + k) % len(_BODIES)])
            write_annotated(
                folder / f"{name}.md",
                f"---\ndeclaration: theorem\n---\n\n# Leaf {c}.{k}\n\n{body}\n\n## Depends on\n\n- [Base](../base.md)\n",
            )
            listed.append(f"- [Leaf {c}.{k}]({name}.md)\n\nBetween the results, <u>a</u> stretch.\n")
        (folder / "README.md").write_text(
            f"---\n---\n\n# Chapter {c}\n\nThe chapter's narrative, with $a_{c}$.\n\n## Results\n\n" + "\n".join(listed),
            encoding="utf-8",
        )
    assert not bodies, bodies
    readme = roadmap / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n## Chapters\n\n" + "".join(listed_chapters), encoding="utf-8")
    return blueprint


def _site(out: Path) -> dict[str, str]:
    return {
        path.relative_to(out).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(out.rglob("*"))
        if path.is_file()
    }


def _rendered(blueprint: Path, out: Path) -> tuple[dict[str, str], list[str], list[str]]:
    """The site render writes at ``out``, by hash, and its warnings."""

    report = render_site(blueprint, out, lean_root=blueprint.parent)
    return _site(out), list(report.warnings), list(report.unresolved)


def _at_depth(extra: int, function, *args, **kwargs):
    """``function(*args, **kwargs)``, called ``extra`` frames deeper."""

    if extra:
        return _at_depth(extra - 1, function, *args, **kwargs)
    return function(*args, **kwargs)


class _InProcess:
    """An executor that runs each task as it is submitted, in this process
    and deeper in the stack than the loop, so each task must calibrate."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.submitted = 0
        self.shutdowns = 0

    def submit(self, fn, *args):
        self.submitted += 1
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # a task's failure belongs to its future
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdowns += 1


def _limited(frames: int):
    """A recursion limit ``frames`` above the caller, so that a page too deep
    for the converter fails within a hundred levels, not a thousand frames."""

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(limit - page_pool.headroom() + frames)
    yield
    sys.setrecursionlimit(limit)


@pytest.fixture
def shallow():
    yield from _limited(450)


@pytest.fixture
def shallower():
    yield from _limited(250)


@pytest.fixture
def pooled(monkeypatch: pytest.MonkeyPatch) -> list:
    """Readers that engage on these small vaults, and every reader made."""

    monkeypatch.setattr(page_pool, "MIN_ARTICLES", 1)
    monkeypatch.setattr(page_pool, "MIN_HEADROOM", 50)
    monkeypatch.setattr(page_pool, "MIN_PYTHON", (3, 10))
    monkeypatch.setattr(page_pool, "CHUNK", 3)
    monkeypatch.setattr(page_pool, "AHEAD", 2)
    monkeypatch.setattr(page_pool, "MAX_WORKERS", 2)
    monkeypatch.setattr(page_pool, "_processors", lambda: 2)
    made: list = []
    real = page_pool.reader

    def recording(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(page_pool, "reader", recording)
    return made


def _as_the_autoform_script(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["autoform", *args])
    return main()


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch, pooled) -> list[_InProcess]:
    executors: list[_InProcess] = []

    def make(workers: int) -> _InProcess:
        executors.append(_InProcess(workers))
        return executors[-1]

    monkeypatch.setattr(page_pool, "_executor", make)
    return executors


def test_publishable_article_reads_each_page_alike_in_any_order(tmp_path: Path) -> None:
    """Workers read pages in another order than the loop, after other pages,
    so the function must read each text alike whatever it read before, a
    page it fails on included."""

    blueprint = _vault(tmp_path)
    texts = [path.read_text(encoding="utf-8") for path in sorted(blueprint.rglob("*.md"))]
    expected = {text: publishable_article(text) for text in texts}
    limit = sys.getrecursionlimit()
    for order in (list(reversed(texts)), random.Random(7).sample(texts, len(texts))):
        for text in order:
            sys.setrecursionlimit(limit - page_pool.headroom() + 120)
            try:
                with pytest.raises(RecursionError):
                    publishable_article(_nested(60))
            finally:
                sys.setrecursionlimit(limit)
            assert publishable_article(text) == expected[text]


def test_publishable_article_reads_alike_under_any_hash_seed(tmp_path: Path) -> None:
    """Each worker is a new interpreter, with its own string hash seed."""

    blueprint = _vault(tmp_path)
    script = (
        "import json, sys; from pathlib import Path; from autoform_cli.ui.publishable import publishable_article; "
        "print(json.dumps([publishable_article(Path(p).read_text(encoding='utf-8')) for p in sys.argv[1:]]))"
    )
    pages = [str(path) for path in sorted(blueprint.rglob("*.md"))]
    outputs = [
        subprocess.run(
            [sys.executable, "-c", script, *pages],
            capture_output=True, text=True, check=True, cwd=_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(_ROOT)},
        ).stdout
        for seed in ("1", "2")
    ]
    assert outputs[0] == outputs[1] and json.loads(outputs[0])


@pytest.mark.parametrize("extra", [0, 1, 9])
def test_calibration_gives_the_target_headroom_in_this_process(extra: int) -> None:
    target = _at_depth(extra, page_pool.headroom)
    limit = sys.getrecursionlimit()
    assert _at_depth(17, page_pool._calibrated_headroom, target) == target
    assert sys.getrecursionlimit() == limit


def test_a_spawned_worker_calls_with_the_headroom_the_loop_has() -> None:
    executor = page_pool._executor(1)
    try:
        for extra in (0, 7, 23):
            target = _at_depth(extra, page_pool.headroom)
            assert executor.submit(page_pool._calibrated_headroom, target).result(timeout=120) == target
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_the_pool_publishes_the_site_and_warnings_the_loop_does(tmp_path: Path, shallow, pooled) -> None:
    blueprint = _vault(tmp_path, extra={"chapter-1-leaf-2": _nested(200)})
    serial = _rendered(blueprint, tmp_path / "out")
    assert pooled == [None]
    warnings = "\n".join(serial[1])
    assert f"/chapter-1-leaf-2: {_DEEP}" in warnings
    assert "the site shows it as typed" in warnings and "a folder the site leaves out" in warnings

    with page_pool.allowed():
        assert _rendered(blueprint, tmp_path / "out") == serial
    reader = pooled[-1]
    # Every node page but the one too deep was read in a worker.
    assert reader is not None and reader.misses == 1 and reader.hits >= 2 * 5
    assert multiprocessing.active_children() == []


def test_check_through_the_cli_warns_alike_with_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], shallow, in_process
) -> None:
    blueprint = _vault(tmp_path, extra={"chapter-0-leaf-1": _nested(200)}, drafts=False)
    # A script that hands main its arguments reads every page itself, since
    # each worker would import that script again.
    assert main(["check", str(blueprint)]) == 0
    serial = capsys.readouterr().out
    assert f"/chapter-0-leaf-1: {_DEEP}" in serial and not in_process

    assert _as_the_autoform_script(monkeypatch, "check", str(blueprint)) == 0
    assert capsys.readouterr().out == serial
    assert in_process and in_process[0].submitted


def test_render_through_the_cli_reads_in_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, shallow, pooled, in_process
) -> None:
    blueprint = _vault(tmp_path, extra={"chapter-0-leaf-3": _nested(200)})
    serial = _rendered(blueprint, tmp_path / "out")

    command = ["render", str(blueprint), "--output", str(tmp_path / "out"), "--lean-root", str(blueprint.parent)]
    # A script that hands main its arguments reads every page itself.
    assert main(command) == 0
    capsys.readouterr()
    assert not in_process

    assert _as_the_autoform_script(monkeypatch, *command) == 0
    report = capsys.readouterr().out.splitlines()
    assert report[1:] == [f"warning: {issue}" for issue in (*serial[2], *serial[1])]
    assert _site(tmp_path / "out") == serial[0]
    assert [reader is not None for reader in pooled] == [False, False, True]
    # A worker raised on the page too deep for it, and only that page was read again.
    assert pooled[-1].hits and pooled[-1].misses == 1
    assert in_process[0].shutdowns == 1


def test_a_library_caller_never_starts_a_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pooled) -> None:
    def refuse(workers: int):
        raise AssertionError("render started a pool without the CLI")

    monkeypatch.setattr(page_pool, "_executor", refuse)
    _rendered(_vault(tmp_path), tmp_path / "out")
    assert pooled == [None]


def test_starting_the_cli_imports_no_process_pool() -> None:
    # Render, check and dashboard load _page_pool, and only a pool that starts needs these.
    program = (
        "import sys, autoform_cli.__main__, autoform_cli.ui.render\n"
        "print(sorted({'multiprocessing', 'concurrent.futures.process'} & set(sys.modules)))\n"
    )
    started = subprocess.run(
        [sys.executable, "-c", program], env=os.environ | {"PYTHONPATH": str(_ROOT)}, capture_output=True, text=True
    )
    assert started.returncode == 0, started.stderr
    assert started.stdout == "[]\n"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="PYTHONSAFEPATH arrived in Python 3.11, below MIN_PYTHON")
def test_a_stdlib_name_in_the_consumers_repository_runs_in_no_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pooled
) -> None:
    blueprint = _vault(tmp_path, drafts=False)
    # The Pages workflow runs check and render from the consumer's repository,
    # and spawn starts each worker with python -c, which puts that first on sys.path.
    monkeypatch.chdir(blueprint.parent)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    planted = blueprint.parent / "selectors.py"
    planted.write_text(
        "import os\n"
        "open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'planted-ran'), 'w').close()\n"
        "raise ImportError('the consumer selectors.py ran')\n",
        encoding="utf-8",
    )

    assert _as_the_autoform_script(monkeypatch, "check", str(blueprint)) == 0
    capsys.readouterr()
    assert pooled[-1] is not None and pooled[-1].hits
    assert not planted.with_name("planted-ran").exists()
    assert "PYTHONSAFEPATH" not in os.environ
    assert multiprocessing.active_children() == []


def test_pythons_whose_pool_can_hang_read_every_page_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pooled
) -> None:
    newer = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro + 1)
    monkeypatch.setattr(page_pool, "MIN_PYTHON", newer)
    with page_pool.allowed():
        _rendered(_vault(tmp_path), tmp_path / "out")
    assert pooled == [None]


def test_a_pool_whose_workers_die_unread_still_stops() -> None:
    # The workers die before reading a task about four times a pipe's size.
    # The loop reads every page itself and the pool stops; before MIN_PYTHON,
    # whose pool would wait forever to finish writing that task, none starts.
    script = """
import multiprocessing, os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import autoform_cli.ui._page_pool as pool

pool.MIN_ARTICLES = 1
pool._processors = lambda: 2
pool._executor = lambda workers: ProcessPoolExecutor(
    workers, mp_context=multiprocessing.get_context("spawn"), initializer=os._exit, initargs=(1,))
pages = [(f"{index} " + "x" * 4096, Path(f"{index}.md"), Path(f"{index}.html")) for index in range(300)]
with pool.allowed():
    reader = pool.reader(iter(pages), lambda text, source, page: (text, []), articles=len(pages), read=pool._READ)
if reader is None:
    print("declined")
else:
    for page in pages:
        assert reader.take(*page) == (page[0], [], None)
    reader.close()
    print(reader.hits, reader.misses)
"""
    done = subprocess.run([sys.executable, "-c", script], cwd=_ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == (["0", "300"] if sys.version_info >= page_pool.MIN_PYTHON else ["declined"])


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_workers_leave_with_a_parent_killed_outright() -> None:
    script = """
import time
import autoform_cli.ui._page_pool as pool

executor = pool._executor(2)
for future in [executor.submit(time.sleep, 1) for _ in range(2)]:
    future.result()
print("started", flush=True)
time.sleep(600)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", script], cwd=_ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert select.select([parent.stdout], [], [], 120)[0] and parent.stdout.readline() == b"started\n"
        parent.kill()
        parent.wait()
        # The workers hold the parent's stdout, which ends once the last of them has left.
        assert select.select([parent.stdout], [], [], 60)[0] and os.read(parent.stdout.fileno(), 1) == b""
    finally:
        # Darwin refuses, with EPERM, a group whose members are all zombies.
        with contextlib.suppress(OSError):
            os.killpg(parent.pid, signal.SIGKILL)
        parent.stdout.close()


def test_a_patched_reader_in_render_sees_every_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_process) -> None:
    seen: list[str] = []

    def counted(text: str):
        seen.append(text)
        return publishable_article(text)

    monkeypatch.setattr(render_module, "publishable_article", counted)
    with page_pool.allowed():
        _rendered(_vault(tmp_path), tmp_path / "out")
    assert not in_process and len(seen) > 2 * 5


class _Breaking(_InProcess):
    """Its third task and every later one find the pool broken."""

    discovered: int | None = None

    def submit(self, fn, *args):
        if self.submitted < 2:
            return super().submit(fn, *args)
        self.submitted += 1
        executor = self

        class Broken(Future):
            def result(self, timeout=None):
                if executor.discovered is None:
                    executor.discovered = executor.submitted
                return super().result(timeout)

        future = Broken()
        future.set_exception(BrokenProcessPool("a worker died"))
        return future


class _Unread(_InProcess):
    """Its workers read nothing, as when each page raised in them."""

    def submit(self, fn, target, texts):
        self.submitted += 1
        future: Future = Future()
        future.set_result([None] * len(texts))
        return future


@pytest.mark.parametrize("case", ["start", "broken", "unread", "drift"])
def test_whatever_stops_the_pool_the_loop_reads_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shallow, pooled, case: str
) -> None:
    blueprint = _vault(tmp_path, chapters=3, leaves=8, extra={"chapter-1-leaf-0": _nested(200)})
    serial = _rendered(blueprint, tmp_path / "out")
    if case == "start":
        def failing(workers: int):
            raise OSError("cannot start a process")

        monkeypatch.setattr(page_pool, "_executor", failing)
    elif case == "broken":
        executors: list[_Breaking] = []
        monkeypatch.setattr(page_pool, "_executor", lambda workers: executors.append(_Breaking(workers)) or executors[-1])
    elif case == "unread":
        monkeypatch.setattr(page_pool, "_executor", _Unread)
    else:
        monkeypatch.setattr(page_pool, "_executor", _InProcess)
        recording = page_pool.reader

        def drifting(jobs, rewrite, **kwargs):
            # The jobs skip a page the loop publishes.
            return recording((job for index, job in enumerate(jobs) if index != 5), rewrite, **kwargs)

        monkeypatch.setattr(page_pool, "reader", drifting)

    with page_pool.allowed():
        assert _rendered(blueprint, tmp_path / "out") == serial
    reader = pooled[-1]
    assert reader is not None
    if case == "start":
        assert reader.hits == reader.misses == 0
    elif case == "broken":
        # Once a task finds the pool broken, no more are submitted.
        assert reader.misses > 1 and executors[0].submitted == executors[0].discovered
    elif case == "unread":
        assert reader.misses > 1
    else:
        # The pages before the one the jobs skipped, and none after it.
        assert reader.hits + reader.misses == 5


def test_a_rewrite_that_raises_raises_in_the_loop_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_process
) -> None:
    blueprint = _vault(tmp_path)
    real = render_module._rewrite_links
    rewritten: list[str] = []

    def rewrite(text: str, **kwargs):
        if "# Leaf 1.3" in text:
            raise ValueError("a link that cannot be rewritten")
        rewritten.append(text)
        return real(text, **kwargs)

    monkeypatch.setattr(render_module, "_rewrite_links", rewrite)
    with pytest.raises(ValueError, match="cannot be rewritten"):
        _rendered(blueprint, tmp_path / "serial")
    serial, rewritten[:] = list(rewritten), []

    with page_pool.allowed(), pytest.raises(ValueError, match="cannot be rewritten"):
        _rendered(blueprint, tmp_path / "pool")
    # Reading ahead stopped at the page that raised, and the loop raised it there.
    assert rewritten == serial
    assert in_process[0].shutdowns == 1

    # So a page the loop reaches first still fails first, though reading
    # ahead has already rewritten the later page.
    split = render_module.statement_and_notes

    def failing(text: str):
        if "Leaf 0.4" in text:
            raise RuntimeError("an earlier page fails")
        return split(text)

    monkeypatch.setattr(render_module, "statement_and_notes", failing)
    for out in ("serial", "pool"):
        with page_pool.allowed() if out == "pool" else contextlib.nullcontext():
            with pytest.raises(RuntimeError, match="an earlier page fails"):
                _rendered(blueprint, tmp_path / out)
    assert len(in_process) == 2


def test_an_interrupt_while_waiting_stops_the_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pooled) -> None:
    class Pending(_InProcess):
        def submit(self, fn, *args):
            self.submitted += 1
            return Future()

    executors: list[Pending] = []

    def make(workers: int) -> Pending:
        executors.append(Pending(workers))
        return executors[-1]

    def interrupted(futures, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(page_pool, "_executor", make)
    monkeypatch.setattr(page_pool, "wait", interrupted)
    with page_pool.allowed(), pytest.raises(KeyboardInterrupt):
        _rendered(_vault(tmp_path), tmp_path / "out")
    assert executors[0].shutdowns == 1
    assert not (tmp_path / "out").exists()


def _published(reader, text: str):
    # What render's published does with a page, from one frame: take it, or read it.
    prepared = reader.take(text, Path("page.md"), Path("page.md")) if reader is not None else None
    if prepared is None:
        return _read_page(text)
    return prepared[2] if prepared[2] is not None else _read_page(prepared[0])


def _limit_for(levels: int) -> int:
    """The deepest page that reads from this frame's callees, by level."""

    low, high = 1, levels
    while low < high:
        middle = (low + high + 1) // 2
        if _DEEP in "".join(_read_page(_nested(middle))[1]):
            high = middle - 1
        else:
            low = middle
    return low


def test_the_reader_measures_the_headroom_render_reads_pages_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_process, pooled
) -> None:
    """Render's loop calls the reader and _read_page from one frame, so the
    headroom the reader measures there is the headroom _read_page gives
    publishable_article, which workers then match."""

    blueprint = _vault(tmp_path)
    seen: list[int] = []

    def measuring(text: str):
        # From publishable_article's frame, one below _read_page's, a measure reads one less.
        seen.append(page_pool.headroom() + 1)
        return publishable_article(text)

    with monkeypatch.context() as patch:
        patch.setattr(render_module, "publishable_article", measuring)
        _rendered(blueprint, tmp_path / "out")
    with page_pool.allowed():
        _rendered(blueprint, tmp_path / "out")
    assert pooled[-1] is not None and pooled[-1].hits
    # Every page the loop reads, not the landing page's lead, which render reads elsewhere.
    assert max(set(seen), key=seen.count) == pooled[-1]._target


def test_a_chunk_reads_each_page_with_the_target_headroom(shallower) -> None:
    """Called deeper than its target, a chunk still reads each page as it
    reads from a frame with that headroom: none it reads there fails."""

    deepest = _limit_for(120)
    ladder = [_nested(deepest + step) for step in (-3, -2, -1, 0, 1, 2)]
    target = page_pool.headroom()
    expected = []
    for text in ladder:
        try:
            expected.append(publishable_article(text))
        except RecursionError:
            expected.append(None)
    assert expected[0] is not None and expected[-1] is None
    for extra in (0, 3, 11):
        assert _at_depth(extra, page_pool._read_chunk, target, ladder) == expected, extra


def test_pages_at_the_recursion_limit_read_alike_from_any_depth(shallower, in_process) -> None:
    """A page nested just deep enough to exhaust the converter reads
    differently with one frame less, and the converter swallows some of
    those errors. Pages a level apart around that limit, taken from frames
    at several depths, read in the worker as the loop reads them."""

    deepest = _limit_for(120)
    ladder = [_nested(deepest + step) for step in (-1, 0, 1, 2)]
    flips = set()
    for extra in range(8):
        serial = [_at_depth(extra, _published, None, text) for text in ladder]
        flips.add(tuple(_DEEP in "".join(found) for _, found, _ in serial))
        reader = page_pool.Reader(iter((text, Path("page.md"), Path("page.md")) for text in ladder),
                                  lambda text, source, page: (text, []), 2)
        try:
            assert [_at_depth(extra, _published, reader, text) for text in ladder] == serial, extra
        finally:
            reader.close()
        assert reader.hits + reader.misses == len(ladder)
    # The limit moved across the ladder, so some depth put a page within a frame of it.
    assert len(flips) > 1


def test_check_reads_pages_at_the_recursion_limit_alike(tmp_path: Path, monkeypatch, shallower, in_process) -> None:
    """The same through check's own loop, from frames at several depths."""

    wide = _vault(tmp_path / "wide", chapters=1, leaves=12, extra={
        f"chapter-0-leaf-{k}": _nested(16 + 6 * k) for k in range(12)
    })
    issues = publication_issues(load_graph(wide), wide)
    flagged = [k for k in range(12) if any(f"/chapter-0-leaf-{k}: {_DEEP}" in issue for issue in issues)]
    assert flagged and flagged[0] > 0
    first = 16 + 6 * flagged[0]
    ladder = _vault(tmp_path / "ladder", chapters=1, leaves=7, extra={
        f"chapter-0-leaf-{k}": _nested(first - 6 + k) for k in range(7)
    })
    graph = load_graph(ladder)
    for extra in range(5):
        with monkeypatch.context() as serial_only:
            serial_only.setattr(page_pool, "MIN_ARTICLES", 10**9)
            with page_pool.allowed():
                serial = _at_depth(extra, publication_issues, graph, ladder)
        with page_pool.allowed():
            assert _at_depth(extra, publication_issues, graph, ladder) == serial, extra
    assert len(in_process) == 5


def test_spawned_workers_read_pages_at_the_recursion_limit_alike(tmp_path: Path, shallower, pooled) -> None:
    ladder = _vault(tmp_path, chapters=1, leaves=12, extra={
        f"chapter-0-leaf-{k}": _nested(16 + 6 * k) for k in range(12)
    })
    graph = load_graph(ladder)
    serial = _at_depth(3, publication_issues, graph, ladder)
    with page_pool.allowed():
        assert _at_depth(3, publication_issues, graph, ladder) == serial
    assert pooled[-1] is not None and pooled[-1].hits
    assert multiprocessing.active_children() == []
