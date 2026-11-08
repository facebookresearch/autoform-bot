"""Reading a large site's node pages in worker processes.

Most of a large render or check goes to
:func:`~autoform_cli.ui.publishable.publishable_article`, a pure function of a
page's text. A :class:`Reader` hands each node page, its links already
rewritten, to a spawn pool a bounded window ahead of the loop that publishes
it, and the loop takes the result where it would have called the function
itself. A page a worker could not read, or a pool that broke or would not
start, leaves the loop to read the page as before, so the site and the
warnings are the same either way. Only the CLI, started as the autoform
script or with python -m, allows a pool: spawn starts each worker by importing
the main module again, and a library caller, or a script that hands main its
arguments, may not guard its own call.

Python-Markdown and pymdown-extensions catch some exceptions broadly, so a
page nested deep enough to exhaust the recursion limit can read differently,
without raising, at another depth. Each worker therefore sets its recursion
limit so that it calls the function with the headroom the loop has.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import BrokenExecutor, Future, wait
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .publishable import publishable_article

# Render, check and dashboard load this module, so multiprocessing and the
# process pool are imported only where a pool starts or runs.
if TYPE_CHECKING:
    from concurrent.futures import ProcessPoolExecutor

#: Smaller sites read their pages in one process, without the workers'
#: start-up and memory.
MIN_ARTICLES = 4000
#: The loop publishing the results keeps up with about this many workers.
MAX_WORKERS = 4
#: Pages per task, and tasks per worker read ahead of the loop.
CHUNK = 64
AHEAD = 4
#: Too close to the recursion limit to match the loop's headroom safely.
MIN_HEADROOM = 200
#: Before this, a pool whose worker died with a task still on its way to it
#: could wait forever in shutdown (gh-94777).
MIN_PYTHON = (3, 11, 5)

_ALLOWED: ContextVar[bool] = ContextVar("_ALLOWED", default=False)
# What workers run: a test that patches render's copy reads every page itself.
_READ = publishable_article

Read = tuple[str, tuple[str, ...], tuple[str, ...]]


@contextmanager
def allowed(allow: bool = True) -> Iterator[None]:
    """Let render and check read node pages in worker processes, if ``allow``,
    until the block ends."""

    token = _ALLOWED.set(allow)
    try:
        yield
    finally:
        _ALLOWED.reset(token)


def headroom() -> int:
    """How deep calls from the caller's frame can nest before RecursionError."""

    depth = 0

    def down() -> None:
        nonlocal depth
        depth += 1
        down()

    try:
        down()
    except RecursionError:
        pass
    return depth


def reader(
    jobs: Iterator[tuple[str, Path, Path]],
    rewrite: Callable[[str, Path, Path], tuple[str, list[str]]],
    *,
    articles: int,
    read: Callable[[str], Read],
) -> Reader | None:
    """A :class:`Reader` for ``jobs``, the node pages as ``(text, source, page)``
    in the order the loop publishes them, or None, when the loop should read
    every page itself: outside :func:`allowed`, for fewer than
    :data:`MIN_ARTICLES` pages, with fewer than two processors, before
    :data:`MIN_PYTHON`, off POSIX, off the main thread, or when ``read``,
    render's copy of the function, is not the one workers run."""

    if not _ALLOWED.get() or articles < MIN_ARTICLES or read is not _READ:
        return None
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        return None
    if sys.version_info < MIN_PYTHON or not sys.executable:
        return None
    import multiprocessing

    if multiprocessing.current_process().daemon:
        return None
    workers = min(_processors(), MAX_WORKERS)
    if workers < 2:
        return None
    return Reader(jobs, rewrite, workers)


@dataclass(eq=False)
class _Page:
    text: str
    source: Path
    page: Path
    rewritten: str
    dropped: list[str]
    future: Future[list[Read | None]] | None = None
    index: int = 0


class Reader:
    """The node pages, read in workers a bounded window ahead of the loop.

    :meth:`take` is called with each page as the loop reaches it. It returns
    the page's rewritten text, the links the rewrite dropped, and the read,
    or None in place of the read for the loop to compute; or None altogether,
    when the loop must rewrite and read the page itself. A rewrite that
    raises stops the reading ahead, so the loop raises it in order.
    """

    def __init__(
        self,
        jobs: Iterator[tuple[str, Path, Path]],
        rewrite: Callable[[str, Path, Path], tuple[str, list[str]]],
        workers: int,
    ) -> None:
        self._jobs = jobs
        self._rewrite = rewrite
        self._workers = workers
        self._window: deque[_Page] = deque()
        self._chunk: list[_Page] = []
        self._executor: ProcessPoolExecutor | None = None
        # PYTHONSAFEPATH as it was before the pool set it, while it is set.
        self._safe_path: tuple[str | None] | None = None
        self._target = 0
        self._started = self._broken = self._exhausted = self._closed = False
        self.hits = self.misses = 0

    def take(self, text: str, source: Path, page: Path) -> tuple[str, list[str], Read | None] | None:
        if self._closed:
            return None
        if not self._started:
            self._started = True
            # Measured here: the loop calls this and _read_page from the same frame,
            # and _read_page calls publishable_article.
            self._target = headroom()
            if self._target < MIN_HEADROOM:
                self.close()
                return None
            # Spawn starts each worker, and the resource tracker, with python -c,
            # which puts the working directory first on sys.path, and render and
            # check run from the consumer's repository: its own selectors.py
            # would run in every process the pool starts. PYTHONSAFEPATH arrived
            # in Python 3.11, below MIN_PYTHON.
            self._safe_path = (os.environ.get("PYTHONSAFEPATH"),)
            os.environ["PYTHONSAFEPATH"] = "1"
            try:
                self._executor = _executor(self._workers)
            except Exception:
                self.close()
                return None
        self._fill()
        if not self._window:
            self.close()
            return None
        head = self._window.popleft()
        if (head.source, head.page, head.text) != (source, page, text):
            self.close()
            return None
        read = None
        if not self._broken:
            if head.future is None:
                self._submit()
            read = self._result(head)
        if read is None:
            self.misses += 1
        else:
            self.hits += 1
        return head.rewritten, head.dropped, read

    def close(self) -> None:
        """Stop the workers; the loop reads every later page itself."""

        self._closed = True
        executor, self._executor = self._executor, None
        if executor is not None:
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception:  # the loop no longer needs anything from it
                pass
        if self._safe_path is not None:
            (before,), self._safe_path = self._safe_path, None
            if before is None:
                os.environ.pop("PYTHONSAFEPATH", None)
            else:
                os.environ["PYTHONSAFEPATH"] = before

    def _fill(self) -> None:
        ahead = AHEAD * self._workers * CHUNK
        while not self._exhausted and len(self._window) < ahead:
            try:
                text, source, page = next(self._jobs)
                rewritten, dropped = self._rewrite(text, source, page)
            except Exception:  # StopIteration included; the loop raises anything else itself
                self._exhausted = True
                break
            entry = _Page(text, source, page, rewritten, dropped)
            self._window.append(entry)
            if not self._broken:
                self._chunk.append(entry)
                if len(self._chunk) >= CHUNK:
                    self._submit()

    def _submit(self) -> None:
        chunk, self._chunk = self._chunk, []
        if not chunk or self._broken or self._executor is None:
            return
        try:
            future = self._executor.submit(_read_chunk, self._target, [entry.rewritten for entry in chunk])
        except Exception:
            self._broken = True
            return
        for index, entry in enumerate(chunk):
            entry.future, entry.index = future, index

    def _result(self, entry: _Page) -> Read | None:
        future = entry.future
        if future is None:
            return None
        try:
            # Polled, as lean_probe's pool is: an interrupt inside an unbounded
            # wait could leave the future's lock held.
            while not future.done():
                wait([future], timeout=0.05)
            results = future.result()
        except BrokenExecutor:
            self._broken = True
            return None
        except Exception:
            return None
        return results[entry.index]


def _processors() -> int:
    if hasattr(os, "process_cpu_count"):
        return os.process_cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def _executor(workers: int) -> ProcessPoolExecutor:
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    return ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"), initializer=_start)


def _start() -> None:
    import multiprocessing

    # Ctrl-C reaches the whole process group; the parent stops the pool.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass
    # A parent killed outright stops nothing: leave with it, rather than wait
    # forever for work while holding its output open.
    parent = multiprocessing.parent_process()
    if parent is not None:
        threading.Thread(target=_leave_with, args=(parent.sentinel,), daemon=True).start()


def _leave_with(sentinel: int) -> None:
    import multiprocessing.connection

    multiprocessing.connection.wait([sentinel])
    os._exit(1)


def _calibrate(target: int) -> bool:
    """Set the recursion limit so that the caller's callees have ``target``
    headroom, as measured by :func:`headroom` from the caller's frame."""

    for _ in range(3):
        # From here, one frame below the caller, the caller's target reads one less.
        gap = target - 1 - headroom()
        if not gap:
            return True
        try:
            sys.setrecursionlimit(sys.getrecursionlimit() + gap)
        except (ValueError, RecursionError):
            return False
    return target - 1 == headroom()


def _read_chunk(target: int, texts: list[str]) -> list[Read | None]:
    """:data:`_READ` of each of ``texts``, called with ``target`` headroom, or
    None for each text it raised on, which the loop then reads itself."""

    limit = sys.getrecursionlimit()
    try:
        if not _calibrate(target):
            return [None] * len(texts)
        results: list[Read | None] = []
        for text in texts:  # not a comprehension: before Python 3.12 that is a frame of its own
            try:
                results.append(_READ(text))
            except Exception:
                results.append(None)
        return results
    finally:
        sys.setrecursionlimit(limit)


def _calibrated_headroom(target: int) -> int:
    """The headroom :func:`_read_chunk` gives what it calls, for tests."""

    limit = sys.getrecursionlimit()
    try:
        _calibrate(target)
        return headroom()
    finally:
        sys.setrecursionlimit(limit)
