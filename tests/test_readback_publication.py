"""Publishing read-back cards: the lock, compare-and-swap, faults, unreadable cards, and platform refusal."""

from __future__ import annotations

import errno
import hashlib
import os
import pickle
import stat
import subprocess
import sys
import textwrap
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import readback
from autoform_cli.readback import (
    PreparedReadback,
    load_readbacks,
    planned_readback,
    prepare_readback,
    publish_readback,
    readback_conflicts,
    readback_findings,
    readback_path,
)
from autoform_cli.skeleton import evidence_hash_of
from tests.test_readback import _ARTICLE_ID, _blueprint, _declaration, _file_card, _report, _staged_names

_DECLARATION = "Skel.sup_unique"
_CARD_LIMIT = 4 * 1024 * 1024


def _filed(tmp_path: Path) -> tuple[Path, Path, str]:
    """A blueprint holding one card, that card's path, and the hash that replaces it."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    return blueprint, path, load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].file_hash


def _prepared(blueprint: Path, text: str, expected: str | None = None) -> PreparedReadback:
    declaration = _declaration()
    return prepare_readback(
        blueprint,
        article_id=_ARTICLE_ID,
        declaration=declaration,
        model="m",
        text=text,
        packet_text=declaration.blind_text(),
        expected_card_hash=expected,
    )


def _byte_hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _child_env(repo_root: Path) -> dict[str, str]:
    paths = (str(repo_root), os.environ.get("PYTHONPATH"))
    return {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, paths))}


def _unlocked(directory: Path) -> bool:
    """Whether another descriptor can take the card directory's lock right now."""

    fcntl = pytest.importorskip("fcntl")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)
    return True


class _Os:
    """The ``os`` module as publication sees it, with one call replaced."""

    def __getattr__(self, name: str):
        return getattr(os, name)


def _fail(monkeypatch, call: str, when=lambda *args, **kwargs: True) -> list[tuple]:
    """Make publication's ``os.<call>`` fail with EIO whenever ``when`` accepts its arguments.

    Only the readback module sees the failing call. Returns the arguments of
    each call that failed.
    """

    real = getattr(os, call)
    failed: list[tuple] = []

    def failing(*args, **kwargs):
        if when(*args, **kwargs):
            failed.append(args)
            raise OSError(errno.EIO, "injected input/output error")
        return real(*args, **kwargs)

    patched = _Os()
    setattr(patched, call, failing)
    patched.supports_dir_fd = os.supports_dir_fd | {failing}
    monkeypatch.setattr(readback, "os", patched)
    return failed


def _restrict(path: Path, mode: int) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    path.chmod(mode)


def _read_only(directory: Path) -> None:
    _restrict(directory, 0o555)


def _without_full_fsync(monkeypatch) -> None:
    """Flush with plain ``os.fsync``, as on Linux, so a test can watch or fail each flush."""

    fcntl = pytest.importorskip("fcntl")
    locking = types.SimpleNamespace(LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB, LOCK_UN=fcntl.LOCK_UN, flock=fcntl.flock)
    monkeypatch.setattr("autoform_cli.readback.fcntl", locking)


def _with_full_fsync(monkeypatch, full_fsync) -> None:
    """Give publication an ``fcntl`` with ``F_FULLFSYNC``, as on macOS, that runs ``full_fsync(descriptor)``."""

    fcntl = pytest.importorskip("fcntl")
    command = getattr(fcntl, "F_FULLFSYNC", 51)

    def call(descriptor: int, requested: int, *args) -> int:
        assert requested == command
        return full_fsync(descriptor)

    locking = types.SimpleNamespace(
        LOCK_EX=fcntl.LOCK_EX,
        LOCK_NB=fcntl.LOCK_NB,
        LOCK_UN=fcntl.LOCK_UN,
        flock=fcntl.flock,
        F_FULLFSYNC=command,
        fcntl=call,
    )
    monkeypatch.setattr("autoform_cli.readback.fcntl", locking)


def _kind(descriptor: int) -> str:
    return "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "card"


# --------------------------------------------------------------------------- #
# The lock and the rename
# --------------------------------------------------------------------------- #

_WATCHER = textwrap.dedent(
    """
    import os, sys
    path, stop = sys.argv[1], sys.argv[2]
    looks = missing = 0
    print("ready", flush=True)
    while not os.path.exists(stop):
        for _ in range(200):
            looks += 1
            try:
                os.stat(path)
            except FileNotFoundError:
                missing += 1
    print(looks, missing, flush=True)
    """
)


def test_a_reader_never_finds_the_card_path_empty_while_cards_are_replaced(tmp_path: Path) -> None:
    blueprint, path, expected = _filed(tmp_path)
    stop = tmp_path / "stop"
    # Another process looks at the card's path as fast as it can.
    watcher = subprocess.Popen(
        [sys.executable, "-c", _WATCHER, str(path), str(stop)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert watcher.stdout is not None and watcher.stdout.readline() == "ready\n"
        for round_ in range(60):
            _file_card(blueprint, f"Replacement {round_}.", expected_card_hash=expected)
            expected = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    finally:
        stop.touch()
        output, _ = watcher.communicate(timeout=30)
    looks, missing = map(int, output.split())
    assert looks > 0
    assert missing == 0


_RIVAL = textwrap.dedent(
    """
    import fcntl, pickle, sys, types
    from autoform_cli import readback

    def flock(descriptor, operation):
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            open(sys.argv[2], "a").close()
            raise

    readback.fcntl = types.SimpleNamespace(
        LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB, LOCK_UN=fcntl.LOCK_UN, flock=flock
    )
    with open(sys.argv[1], "rb") as stream:
        card = pickle.load(stream)
    try:
        readback.publish_readback(card)
    except ValueError as exc:
        print(exc)
    else:
        print("filed")
    """
)


def test_a_rival_process_publishing_other_content_waits_for_the_lock(
    tmp_path: Path, repo_root: Path, monkeypatch
) -> None:
    pytest.importorskip("fcntl")
    blueprint, path, expected = _filed(tmp_path)
    rival_card = tmp_path / "rival.pickle"
    rival_card.write_bytes(pickle.dumps(_prepared(blueprint, "Rival.", expected)))
    waiting = tmp_path / "waiting"
    rival: subprocess.Popen[str] | None = None
    check = readback._card_hash_at

    # Once this publisher has compared the card, another process publishes
    # different content over the same card, expecting the same hash.
    def check_then_start_rival(directory: int, filename: str, display_path: Path):
        nonlocal rival
        found = check(directory, filename, display_path)
        if filename == path.name and rival is None:
            rival = subprocess.Popen(
                [sys.executable, "-c", _RIVAL, str(rival_card), str(waiting)],
                stdout=subprocess.PIPE,
                text=True,
                env=_child_env(repo_root),
            )
            deadline = time.monotonic() + 10
            while not waiting.exists() and rival.poll() is None and time.monotonic() < deadline:
                time.sleep(0.005)
        return found

    monkeypatch.setattr("autoform_cli.readback._card_hash_at", check_then_start_rival)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert rival is not None
    output, _ = rival.communicate(timeout=30)
    assert waiting.exists()
    assert "changed before replacement" in output
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "Replacement."
    assert _staged_names(path.parent) == []


def test_the_card_is_renamed_into_place_while_the_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    replace_file = os.replace
    held: list[bool] = []

    # As the publication renames its card into place, another descriptor tries the lock.
    def try_the_lock_then_rename(*args, **kwargs) -> None:
        held.append(not _unlocked(path.parent))
        replace_file(*args, **kwargs)

    monkeypatch.setattr(os, "replace", try_the_lock_then_rename)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert held == [True]
    assert _unlocked(path.parent)


def test_a_child_process_started_during_publication_does_not_keep_the_lock(tmp_path: Path, monkeypatch) -> None:
    fcntl = pytest.importorskip("fcntl")
    blueprint, path, expected = _filed(tmp_path)
    publish = readback._publish_card
    children: list[subprocess.Popen[bytes]] = []

    # While the lock is held, the publisher starts a long-lived child that
    # inherits the locked descriptor.
    def publish_with_a_child(directory: int, *args, **kwargs):
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
                pass_fds=(directory,),
            )
        )
        return publish(directory, *args, **kwargs)

    monkeypatch.setattr("autoform_cli.readback._publish_card", publish_with_a_child)

    try:
        assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
        assert len(children) == 1 and children[0].poll() is None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(directory)
    finally:
        for child in children:
            child.communicate(timeout=30)


@pytest.mark.parametrize("how", ["replaced", "removed", "linked"])
def test_a_card_directory_replaced_before_it_is_locked_is_left_alone(tmp_path: Path, monkeypatch, how: str) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    moved = path.parent.with_name("moved")
    lock = readback._lock_card_directory
    replaced = False

    # Between the walk and the lock, the card directory moves away, and an
    # empty directory, nothing, or a link to the moved directory takes its name.
    def replace_then_lock(directory: int, display_path: Path) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            path.parent.rename(moved)
            if how == "replaced":
                path.parent.mkdir()
            elif how == "linked":
                path.parent.symlink_to(moved, target_is_directory=True)
        lock(directory, display_path)

    monkeypatch.setattr("autoform_cli.readback._lock_card_directory", replace_then_lock)

    # The publication starts over from the blueprint: the card it expects is
    # not in a new directory, and a link is never followed.
    refusal = "refusing a symlink" if how == "linked" else "changed before replacement"
    with pytest.raises(ValueError, match=refusal):
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert (moved / path.name).read_bytes() == original
    assert _staged_names(moved) == []
    if how != "linked":
        assert os.listdir(path.parent) == []


def test_a_card_directory_that_keeps_being_replaced_is_refused_before_staging(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    lock = readback._lock_card_directory
    moves: list[Path] = []

    def replace_then_lock(directory: int, display_path: Path) -> None:
        moves.append(path.parent.with_name(f"moved-{len(moves)}"))
        path.parent.rename(moves[-1])
        path.parent.mkdir()
        lock(directory, display_path)

    monkeypatch.setattr("autoform_cli.readback._lock_card_directory", replace_then_lock)

    with pytest.raises(ValueError, match="kept being replaced"):
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert len(moves) == 3
    assert (moves[0] / path.name).read_bytes() == original
    assert all(_staged_names(moved) == [] for moved in moves)
    assert os.listdir(path.parent) == []


@pytest.mark.parametrize("failing", ["lock", "unlock"])
def test_a_lock_call_that_fails_is_handled(tmp_path: Path, monkeypatch, failing: str) -> None:
    fcntl = pytest.importorskip("fcntl")
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    operation = fcntl.LOCK_EX | fcntl.LOCK_NB if failing == "lock" else fcntl.LOCK_UN

    def flock(descriptor: int, requested: int) -> None:
        if requested == operation:
            raise OSError(errno.ENOLCK, "no locks available")
        fcntl.flock(descriptor, requested)

    locking = types.SimpleNamespace(LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB, LOCK_UN=fcntl.LOCK_UN, flock=flock)
    monkeypatch.setattr("autoform_cli.readback.fcntl", locking)

    if failing == "lock":
        # A lock that cannot be taken stops the write before anything is staged.
        with pytest.raises(ValueError, match="cannot lock read-back directory for writing"):
            _file_card(blueprint, "Replacement.", expected_card_hash=expected)
        assert path.read_bytes() == original
    else:
        # Closing the descriptor still releases a lock that would not unlock.
        assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
        assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "Replacement."
    assert _staged_names(path.parent) == []
    assert _unlocked(path.parent)


# --------------------------------------------------------------------------- #
# Compare-and-swap by the bytes of the card
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("naming", ["nothing", "another card"])
def test_different_content_replaces_a_card_only_by_naming_its_hash(tmp_path: Path, naming: str) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    named = None if naming == "nothing" else "sha256:" + "1" * 64
    card = _prepared(blueprint, "Replacement.", named)
    if named is None:
        refusal = f"read-back already exists with different content: {path}; retry with expected_card_hash={expected!r}"
    else:
        refusal = f"read-back changed before replacement: expected {named!r}, found {expected!r}"

    # The conflict check and the write refuse alike, and name the hash of the card's bytes.
    assert readback_conflicts([card]) == [f"{_DECLARATION}: {refusal}"]
    with pytest.raises(ValueError) as refused:
        publish_readback(card)
    assert str(refused.value) == refusal
    assert expected == _byte_hash(original)
    assert path.read_bytes() == original
    assert _staged_names(path.parent) == []


def test_a_write_naming_the_wrong_hash_is_refused_before_anything_is_staged(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    open_ = os.open
    created: list[str] = []

    def counted(name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            created.append(name)
        return open_(name, flags, *args, **kwargs)

    patched = _Os()
    patched.open = counted
    patched.supports_dir_fd = os.supports_dir_fd | {counted}
    monkeypatch.setattr(readback, "os", patched)

    # The compare comes first, so a refused write never creates a staging file.
    with pytest.raises(ValueError, match="changed before replacement"):
        _file_card(blueprint, "Replacement.", expected_card_hash="sha256:" + "1" * 64)
    assert created == []
    assert _byte_hash(path.read_bytes()) == expected


@pytest.mark.parametrize("call", ["fstat", "read"])
def test_a_card_that_fails_to_read_during_the_compare_is_refused_with_its_path_and_reason(
    tmp_path: Path, monkeypatch, call: str
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    card = _prepared(blueprint, "Replacement.", expected)
    _fail(monkeypatch, call, lambda descriptor, *args: stat.S_ISREG(os.fstat(descriptor).st_mode))

    for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
        with pytest.raises(ValueError) as refused:
            attempt()
        assert str(refused.value) == f"cannot read existing read-back: {path}: injected input/output error"
    assert path.read_bytes() == original
    assert _staged_names(path.parent) == []
    assert _unlocked(path.parent)


def test_an_identical_card_is_refiled_without_writing_even_in_a_read_only_directory(tmp_path: Path) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    same, other = _prepared(blueprint, "First."), _prepared(blueprint, "Replacement.", expected)
    _read_only(path.parent)
    try:
        # Filing the same card again needs no write, and the conflict check agrees.
        assert readback_conflicts([same]) == []
        assert publish_readback(same) == path
        # A real replacement passes the compare-and-swap, then cannot write.
        assert readback_conflicts([other]) == []
        with pytest.raises(ValueError, match=f"cannot publish read-back: {path}: .*Permission denied"):
            publish_readback(other)
    finally:
        path.parent.chmod(0o755)
    assert path.read_bytes() == original
    assert _staged_names(path.parent) == []


def test_a_card_that_is_not_utf8_is_reported_and_replaced_by_the_hash_of_its_bytes(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, _DECLARATION)
    path.parent.mkdir(parents=True)
    garbled = b"\xff\xfe not utf-8\n"
    path.write_bytes(garbled)

    # The loader reports the card as invalid, under its own name, rather than missing.
    loaded = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)]
    assert not loaded.valid and loaded.path == path
    assert "card is not UTF-8 text" in loaded.validate()
    assert loaded.file_hash == _byte_hash(garbled)
    (finding,) = readback_findings(_report(), load_readbacks(blueprint), article_ids={"basics/sup-unique": _ARTICLE_ID})
    assert finding.code == "readback-invalid" and "card is not UTF-8 text" in finding.reason

    # A write names it by that hash, like any card, and replaces it.
    (conflict,) = readback_conflicts([_prepared(blueprint, "Replacement.")])
    assert f"expected_card_hash={loaded.file_hash!r}" in conflict
    card = _prepared(blueprint, "Replacement.", loaded.file_hash)
    assert readback_conflicts([card]) == []
    assert publish_readback(card) == path
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "Replacement."


def test_a_card_over_the_size_limit_is_reported_and_never_read_past_it(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, _DECLARATION)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * (2 * _CARD_LIMIT))
    read = os.read
    sizes: list[int] = []

    def counted(descriptor: int, size: int) -> bytes:
        block = read(descriptor, size)
        sizes.append(len(block))
        return block

    patched = _Os()
    patched.read = counted
    monkeypatch.setattr(readback, "os", patched)

    loaded = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)]
    assert f"card is over the {_CARD_LIMIT}-byte limit for a card file" in loaded.validate()
    assert loaded.text == "" and loaded.file_hash is None
    assert sum(sizes) == _CARD_LIMIT + 1
    sizes.clear()

    card = _prepared(blueprint, "Replacement.", "sha256:" + "1" * 64)
    for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
        with pytest.raises(ValueError, match="over the .*-byte limit .*; remove it to file a new card"):
            attempt()
        assert sum(sizes) == _CARD_LIMIT + 1
        sizes.clear()
    assert path.stat().st_size == 2 * _CARD_LIMIT
    assert _staged_names(path.parent) == []


def test_a_card_too_large_for_a_card_file_is_refused_before_it_is_written(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    packet = "x" * _CARD_LIMIT + "\n"

    with pytest.raises(ValueError, match=f"over the {_CARD_LIMIT}-byte limit"):
        planned_readback(
            blueprint,
            article_id=_ARTICLE_ID,
            declaration=_DECLARATION,
            skeleton_hash="sha256:" + "0" * 64,
            packet_hash=evidence_hash_of(packet),
            model="m",
            text="Fine.",
            packet_text=packet,
        )
    assert not (blueprint / "readbacks").exists()


def test_a_card_of_exactly_the_size_limit_is_filed_loaded_and_replaced_and_one_byte_more_is_not(
    tmp_path: Path,
) -> None:
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, _DECLARATION)
    largest = b"x" * _CARD_LIMIT

    # One byte over the limit is refused as the card is built.
    with pytest.raises(ValueError, match=f"over the {_CARD_LIMIT}-byte limit"):
        PreparedReadback(blueprint, _ARTICLE_ID, _DECLARATION, path, "x" * (_CARD_LIMIT + 1), None)

    # A card of exactly the limit is published, loaded whole, and replaced by naming its hash.
    card = PreparedReadback(blueprint, _ARTICLE_ID, _DECLARATION, path, largest.decode(), None)
    assert publish_readback(card) == path
    loaded = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)]
    assert loaded.file_hash == _byte_hash(largest)
    assert not any("byte limit" in error for error in loaded.validate())
    assert publish_readback(_prepared(blueprint, "Replacement.", loaded.file_hash)) == path

    # A file one byte over it is reported, and a write cannot replace it.
    path.write_bytes(largest + b"x")
    loaded = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)]
    assert f"card is over the {_CARD_LIMIT}-byte limit for a card file" in loaded.validate()
    assert loaded.file_hash is None
    with pytest.raises(ValueError, match="remove it to file a new card"):
        publish_readback(_prepared(blueprint, "Replacement.", _byte_hash(largest + b"x")))


@pytest.mark.parametrize("failing", ["open", "read"])
def test_a_card_that_cannot_be_read_is_reported(tmp_path: Path, monkeypatch, failing: str) -> None:
    blueprint, path, expected = _filed(tmp_path)
    if failing == "open":
        _read_only(path.parent)
        path.chmod(0o000)
        reason = "card cannot be read: Permission denied"
    else:
        _fail(monkeypatch, "read")
        reason = "card cannot be read: injected input/output error"
    try:
        loaded = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)]
        if failing == "open":
            # Nor can a write compare it.
            with pytest.raises(ValueError, match=f"cannot safely inspect existing read-back: {path}: Permission denied"):
                _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    finally:
        path.chmod(0o644)
        path.parent.chmod(0o755)
    assert reason in loaded.validate()
    assert loaded.file_hash is None
    assert _staged_names(path.parent) == []


@pytest.mark.parametrize(
    ("error", "reported"), [(errno.EIO, True), (errno.EMFILE, True), (errno.ENOENT, False), (errno.ELOOP, False)]
)
def test_a_card_whose_open_fails_is_reported_unless_nothing_or_a_link_is_there(
    tmp_path: Path, monkeypatch, error: int, reported: bool
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    open_ = os.open

    def failing(name, flags, *args, **kwargs):
        if name == path.name:
            raise OSError(error, os.strerror(error))
        return open_(name, flags, *args, **kwargs)

    patched = _Os()
    patched.open = failing
    patched.supports_dir_fd = os.supports_dir_fd | {failing}
    monkeypatch.setattr(readback, "os", patched)

    loaded = load_readbacks(blueprint)
    findings = readback_findings(_report(), loaded, article_ids={"basics/sup-unique": _ARTICLE_ID})
    if reported:
        assert f"card cannot be read: {os.strerror(error)}" in loaded[(_ARTICLE_ID, _DECLARATION)].validate()
        assert [finding.code for finding in findings] == ["readback-invalid"]
    else:
        # A card removed since the listing, or a link, is not there to read.
        assert loaded == {}
        assert [finding.code for finding in findings] == ["readback-missing"]


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_a_fifo_or_directory_at_a_card_path_is_reported_without_blocking_the_loader(tmp_path: Path, kind: str) -> None:
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("needs FIFOs")
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, _DECLARATION)
    path.parent.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    loaded: list[dict] = []

    loader = threading.Thread(target=lambda: loaded.append(load_readbacks(blueprint)), daemon=True)
    loader.start()
    loader.join(5)
    blocked = loader.is_alive()
    if blocked:
        # A loader stuck opening the FIFO is let go, so the test fails rather than hangs.
        os.close(os.open(path, os.O_WRONLY))
        loader.join()
    assert not blocked

    # Reported as an invalid card, as the writer refuses it, rather than as a missing one.
    assert "card path is not a regular file" in loaded[0][(_ARTICLE_ID, _DECLARATION)].validate()
    (finding,) = readback_findings(_report(), loaded[0], article_ids={"basics/sup-unique": _ARTICLE_ID})
    assert finding.code == "readback-invalid" and "card path is not a regular file" in finding.reason
    with pytest.raises(ValueError, match="read-back destination is not a regular file"):
        publish_readback(_prepared(blueprint, "First."))


@pytest.mark.parametrize("mode", [0o000, 0o311])
@pytest.mark.parametrize("unlistable", ["readbacks", "article"])
def test_a_card_directory_that_cannot_be_listed_stops_loading_rather_than_hiding_its_cards(
    tmp_path: Path, unlistable: str, mode: int
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    directory = path.parent if unlistable == "article" else path.parent.parent
    _restrict(directory, mode)
    try:
        with pytest.raises(ValueError) as refused:
            load_readbacks(blueprint)
    finally:
        directory.chmod(0o755)
    assert str(refused.value) == f"cannot list read-back directory {directory}: Permission denied"


@pytest.mark.parametrize("damage", ["not UTF-8", "over the limit", "unreadable"])
def test_a_long_named_card_that_cannot_be_read_is_reported_once_for_its_declaration(
    tmp_path: Path, monkeypatch, damage: str
) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = replace(_declaration(), name="Skel." + "α" * 40)
    path = _file_card(blueprint, "First.", declaration=declaration)
    # Too long to spell in a filename, so only the card's frontmatter names it.
    assert path.name.startswith("declaration--")
    if damage == "not UTF-8":
        path.write_bytes(path.read_bytes() + b"\xff\xfe")
        reason = "card is not UTF-8 text"
    elif damage == "over the limit":
        path.write_bytes(b"x" * (_CARD_LIMIT + 1))
        reason = f"card is over the {_CARD_LIMIT}-byte limit for a card file"
    else:
        _fail(monkeypatch, "read")
        reason = "card cannot be read: injected input/output error"

    findings = readback_findings(
        _report(declaration), load_readbacks(blueprint), article_ids={"basics/sup-unique": _ARTICLE_ID}
    )
    assert [(finding.declaration, finding.code) for finding in findings] == [(declaration.name, "readback-invalid")]
    assert reason in findings[0].reason


# --------------------------------------------------------------------------- #
# Links and missing directories on the way to a card
# --------------------------------------------------------------------------- #


def test_a_card_that_is_a_symlink_is_neither_followed_nor_replaced(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    outside = _file_card(_blueprint(tmp_path / "elsewhere"), "Outside.")
    original = outside.read_bytes()
    path = readback_path(blueprint, _ARTICLE_ID, _DECLARATION)
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)
    card = _prepared(blueprint, "Replacement.", _byte_hash(original))

    assert load_readbacks(blueprint) == {}
    for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
        with pytest.raises(ValueError, match=f"cannot safely inspect existing read-back: {path}"):
            attempt()
    assert path.is_symlink() and outside.read_bytes() == original
    assert _staged_names(path.parent) == []


@pytest.mark.parametrize("linked", ["readbacks", "article"])
def test_a_card_directory_reached_through_a_symlink_is_refused(tmp_path: Path, linked: str) -> None:
    blueprint = _blueprint(tmp_path)
    outside = _file_card(_blueprint(tmp_path / "elsewhere"), "Outside.")
    original = outside.read_bytes()
    target = outside.parent if linked == "article" else outside.parent.parent
    link = readback_path(blueprint, _ARTICLE_ID, _DECLARATION).parent
    if linked == "readbacks":
        link = link.parent
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    card = _prepared(blueprint, "Replacement.", _byte_hash(original))

    for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
        with pytest.raises(ValueError, match="refusing a symlink or unsafe component in the read-back path"):
            attempt()
    assert outside.read_bytes() == original
    assert sorted(os.listdir(outside.parent)) == [outside.name]


def test_a_link_put_where_a_card_directory_is_being_made_is_refused(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    mkdir = os.mkdir

    # Just as the walk makes the card directory, a link to another directory takes its name.
    def link_then_mkdir(name: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if name == _ARTICLE_ID:
            os.symlink(outside, name, dir_fd=dir_fd)
        mkdir(name, mode, dir_fd=dir_fd)

    patched = _Os()
    patched.mkdir = link_then_mkdir
    patched.supports_dir_fd = os.supports_dir_fd | {link_then_mkdir}
    monkeypatch.setattr(readback, "os", patched)

    with pytest.raises(
        ValueError, match=f"refusing a symlink or unsafe component in the read-back path: .*/readbacks/{_ARTICLE_ID}: "
    ):
        _file_card(blueprint, "First.")
    assert list(outside.iterdir()) == []


def test_a_card_directory_that_cannot_be_made_is_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    card = _prepared(blueprint, "First.")
    _read_only(blueprint)
    try:
        # With no card directory there is nothing to conflict with, and nothing is made.
        assert readback_conflicts([card]) == []
        with pytest.raises(ValueError) as refused:
            publish_readback(card)
    finally:
        blueprint.chmod(0o755)
    assert str(refused.value) == (
        f"cannot create read-back directory component: {card.blueprint / 'readbacks'}: Permission denied"
    )
    assert not (blueprint / "readbacks").exists()


@pytest.mark.parametrize("unreadable", ["readbacks", "article"])
def test_a_card_directory_that_cannot_be_opened_is_refused_with_the_reason_not_as_a_symlink(
    tmp_path: Path, unreadable: str
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    directory = path.parent if unreadable == "article" else path.parent.parent
    card = _prepared(blueprint, "Replacement.", expected)
    _restrict(directory, 0o311)
    try:
        for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
            with pytest.raises(ValueError) as refused:
                attempt()
            assert str(refused.value) == f"cannot open read-back directory component: {directory}: Permission denied"
    finally:
        directory.chmod(0o755)
    assert path.read_bytes() == original


def test_a_card_whose_article_id_would_leave_the_readbacks_directory_is_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    escape = blueprint / "readbacks" / ".." / "escape" / "Skel.sup_unique.md"
    card = PreparedReadback(blueprint, "../escape", _DECLARATION, escape, "First.\n", None)

    for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
        with pytest.raises(ValueError, match="invalid article_id for a read-back: '../escape'"):
            attempt()
    assert not (blueprint / "readbacks").exists() and not (blueprint / "escape").exists()


def test_a_blueprint_removed_before_publication_is_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    card = _prepared(blueprint, "First.")
    blueprint.rename(tmp_path / "moved")

    for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
        with pytest.raises(ValueError, match="cannot safely open blueprint directory"):
            attempt()
    assert not blueprint.exists()


# --------------------------------------------------------------------------- #
# A failing call at each step of a write
# --------------------------------------------------------------------------- #


def _is_staging_file(*args, **kwargs) -> bool:
    return bool(args[1] & os.O_CREAT)


def _is_a_file(descriptor: int, *args) -> bool:
    return not stat.S_ISDIR(os.fstat(descriptor).st_mode)


@pytest.mark.parametrize(
    ("call", "when"),
    [
        ("open", _is_staging_file),
        ("write", lambda *args: True),
        ("fchmod", lambda *args: True),
        ("fsync", _is_a_file),
        ("replace", lambda *args, **kwargs: True),
    ],
)
def test_a_call_that_fails_before_the_rename_leaves_the_card_and_no_staging_file(
    tmp_path: Path, monkeypatch, call: str, when
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    if call == "fsync":
        _without_full_fsync(monkeypatch)
    failed = _fail(monkeypatch, call, when)

    with pytest.raises(ValueError) as refused:
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert str(refused.value) == f"cannot publish read-back: {path}: [Errno {errno.EIO}] injected input/output error"
    assert len(failed) == 1
    assert path.read_bytes() == original
    assert _staged_names(path.parent) == []
    assert _unlocked(path.parent)


def test_a_staging_file_that_cannot_be_removed_is_left_once_the_rename_fails(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    replace_file, unlink = os.replace, os.unlink
    removals: list[str] = []

    def fail_replace(*args, **kwargs) -> None:
        raise OSError(errno.EIO, "injected input/output error")

    def fail_unlink(name: str, *, dir_fd: int | None = None) -> None:
        removals.append(name)
        raise OSError(errno.EIO, "injected input/output error")

    patched = _Os()
    patched.replace, patched.unlink = fail_replace, fail_unlink
    patched.supports_dir_fd = os.supports_dir_fd | {fail_unlink}
    monkeypatch.setattr(readback, "os", patched)

    # The error is the rename's, and names the file the one failed removal left.
    with pytest.raises(ValueError) as refused:
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert len(removals) == 1 and _staged_names(path.parent) == removals
    assert str(refused.value) == (
        f"cannot publish read-back: {path}: [Errno {errno.EIO}] injected input/output error; "
        f"its temporary file {path.with_name(removals[0])} could not be removed"
    )
    assert path.read_bytes() == original
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "First."
    assert replace_file is os.replace and unlink is os.unlink


def test_a_directory_that_cannot_be_flushed_after_the_rename_is_a_warning(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    _without_full_fsync(monkeypatch)
    _fail(monkeypatch, "fsync", lambda descriptor: stat.S_ISDIR(os.fstat(descriptor).st_mode))

    with pytest.warns(RuntimeWarning, match=f"read-back {path} was published, but its directory could not be flushed"):
        assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "Replacement."
    assert _staged_names(path.parent) == []


def test_the_card_is_flushed_once_written_and_its_mode_set_and_its_directory_once_it_is_renamed(
    tmp_path: Path, monkeypatch
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    _without_full_fsync(monkeypatch)
    steps: list[str] = []
    patched = _Os()

    def record(call: str, step):
        real = getattr(os, call)

        def recorded(*args, **kwargs):
            result = real(*args, **kwargs)
            if (name := step(*args, **kwargs)) and steps[-1:] != [name]:
                steps.append(name)
            return result

        setattr(patched, call, recorded)
        return recorded

    patched.supports_dir_fd = os.supports_dir_fd | {
        record("open", lambda name, flags, *args, **kwargs: "stage" if flags & os.O_CREAT else None)
    }
    record("write", lambda *args: "write")
    record("fchmod", lambda *args: "fchmod")
    record("fsync", lambda descriptor: f"flush {_kind(descriptor)}")
    record("replace", lambda *args, **kwargs: "rename")
    monkeypatch.setattr(readback, "os", patched)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert steps == ["stage", "write", "fchmod", "flush card", "rename", "flush directory"]


def test_every_card_is_left_readable_by_all_and_writable_by_its_owner_whatever_the_umask(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    previous = os.umask(0o077)
    try:
        path = _file_card(blueprint, "First.")
        first = stat.S_IMODE(path.stat().st_mode)
        path.chmod(0o600)
        _file_card(blueprint, "Replacement.", expected_card_hash=_byte_hash(path.read_bytes()))
    finally:
        os.umask(previous)
    assert first == stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.parametrize("full_fsync", ["works", "is refused"])
def test_each_flush_uses_full_fsync_where_the_platform_has_it_and_fsync_where_it_fails(
    tmp_path: Path, monkeypatch, full_fsync: str
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    fsync = os.fsync
    flushed: list[tuple[str, str]] = []

    def full(descriptor: int) -> int:
        if full_fsync == "is refused":
            raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
        flushed.append(("F_FULLFSYNC", _kind(descriptor)))
        return 0

    def plain(descriptor: int) -> None:
        flushed.append(("fsync", _kind(descriptor)))
        fsync(descriptor)

    _with_full_fsync(monkeypatch, full)
    patched = _Os()
    patched.fsync = plain
    monkeypatch.setattr(readback, "os", patched)

    # On macOS a plain fsync can leave the data in the drive's cache, so it is only the fallback.
    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    call = "F_FULLFSYNC" if full_fsync == "works" else "fsync"
    assert flushed == [(call, "card"), (call, "directory")]


def test_a_first_card_flushes_each_directory_it_makes_into_its_parent(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    _without_full_fsync(monkeypatch)
    fsync = os.fsync
    flushed: list[tuple[int, int]] = []

    def recorded(descriptor: int) -> None:
        fsync(descriptor)
        found = os.fstat(descriptor)
        flushed.append((found.st_dev, found.st_ino))

    patched = _Os()
    patched.fsync = recorded
    monkeypatch.setattr(readback, "os", patched)

    path = _file_card(blueprint, "First.")
    identities = [(found.st_dev, found.st_ino) for found in map(os.stat, (blueprint, path.parent.parent, path))]
    # Each new directory's name, then the card, then the card's name.
    assert flushed == [*identities, (os.stat(path.parent).st_dev, os.stat(path.parent).st_ino)]


def test_a_directory_made_for_a_first_card_that_cannot_be_flushed_into_its_parent_is_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    blueprint = _blueprint(tmp_path)
    _without_full_fsync(monkeypatch)
    parent = os.stat(blueprint).st_ino
    _fail(monkeypatch, "fsync", lambda descriptor: os.fstat(descriptor).st_ino == parent)

    with pytest.warns(RuntimeWarning, match="read-back directory .*/readbacks was made, but its parent directory"):
        path = _file_card(blueprint, "First.")
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "First."
    assert _staged_names(path.parent) == []


def test_short_writes_still_stage_the_whole_card(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    write = os.write
    calls = 0

    def write_a_little(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        return write(descriptor, bytes(data[:7]))

    patched = _Os()
    patched.write = write_a_little
    monkeypatch.setattr(readback, "os", patched)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    loaded = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)]
    assert loaded.valid and loaded.text == "Replacement."
    assert calls == -(-path.stat().st_size // 7)


def test_a_staging_name_already_taken_is_neither_used_nor_removed(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    taken = path.with_name(".autoform-readback-" + "0" * 24 + ".tmp")
    taken.write_bytes(b"Someone else's file.\n")
    monkeypatch.setattr("autoform_cli.readback.secrets.token_hex", lambda size: "0" * (2 * size))

    with pytest.raises(ValueError, match=f"cannot publish read-back: {path}: .*File exists"):
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert taken.read_bytes() == b"Someone else's file.\n"
    assert path.read_bytes() == original


# --------------------------------------------------------------------------- #
# Interruptions
# --------------------------------------------------------------------------- #


def test_an_interruption_between_directories_of_the_walk_closes_each_once(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    card = _prepared(blueprint, "Replacement.", expected)
    close = os.close
    interrupted = False

    # The interrupt lands just after the walk closes a directory it has left.
    def close_then_interrupt(descriptor: int) -> None:
        nonlocal interrupted
        leaving = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        close(descriptor)
        if leaving and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.readback.os.close", close_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        publish_readback(card)
    assert interrupted


def test_an_interruption_while_a_card_is_read_closes_its_descriptor_once(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    card = _prepared(blueprint, "Replacement.", expected)
    fdopen, read = os.fdopen, os.read
    interrupted = False

    # The interrupt lands as the card is read: inside a file object made on
    # its descriptor, which closes the descriptor as it is dropped, or just
    # after a read from the descriptor itself.
    def fdopen_then_interrupt(descriptor: int, *args, **kwargs):
        nonlocal interrupted
        interrupted = True
        fdopen(descriptor, *args, **kwargs).close()
        raise KeyboardInterrupt

    def read_then_interrupt(descriptor: int, size: int) -> bytes:
        nonlocal interrupted
        block = read(descriptor, size)
        if interrupted:
            return block
        interrupted = True
        raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.readback.os.fdopen", fdopen_then_interrupt)
    monkeypatch.setattr("autoform_cli.readback.os.read", read_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        readback_conflicts([card])
    assert interrupted


def test_an_interruption_as_the_staging_file_is_created_leaves_no_file(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    card = _prepared(blueprint, "Replacement.", expected)
    open_ = os.open

    # The interrupt lands just after the staging file is created, before its
    # descriptor is kept.
    def open_then_interrupt(name, flags, *args, **kwargs):
        descriptor = open_(name, flags, *args, **kwargs)
        if flags & os.O_CREAT:
            os.close(descriptor)
            raise KeyboardInterrupt
        return descriptor

    monkeypatch.setattr("autoform_cli.readback.os.open", open_then_interrupt)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_then_interrupt})

    with pytest.raises(KeyboardInterrupt):
        publish_readback(card)
    assert _staged_names(path.parent) == []
    assert path.read_bytes() == original


def test_an_interruption_as_any_step_of_a_write_returns_leaves_no_lock_or_staging_file(tmp_path: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    module = readback.__file__
    step = returns = 0

    # Python 3.10 can deliver an interrupt as any function returns, before its
    # caller keeps the result. Raising from a trace function on the n-th
    # return in the module lands one there, for each n in turn.
    def interrupt_on_the_nth_return(frame, event, arg):
        nonlocal returns
        if frame.f_code.co_filename != module:
            return None
        if event == "return":
            returns += 1
            if returns == step:
                raise KeyboardInterrupt
        return interrupt_on_the_nth_return

    while True:
        step += 1
        returns = 0
        blueprint, path, expected = _filed(tmp_path / str(step))
        original = path.read_bytes()
        card = _prepared(blueprint, "Replacement.", expected)
        sys.settrace(interrupt_on_the_nth_return)
        try:
            publish_readback(card)
        except KeyboardInterrupt:
            pass
        finally:
            sys.settrace(None)
        if returns < step:
            break
        assert path.read_bytes() in (original, card.content.encode("utf-8")), step
        assert _staged_names(path.parent) == [], step
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(directory)
    assert step > 10


# --------------------------------------------------------------------------- #
# The batch conflict check and unsupported platforms
# --------------------------------------------------------------------------- #


def test_the_conflict_check_reports_a_card_removed_since_its_hash_was_taken(tmp_path: Path) -> None:
    blueprint, path, expected = _filed(tmp_path)
    card = _prepared(blueprint, "Replacement.", expected)
    path.unlink()

    (conflict,) = readback_conflicts([card])
    assert "changed before replacement" in conflict and "found None" in conflict
    with pytest.raises(ValueError, match="changed before replacement"):
        publish_readback(card)
    assert not path.exists()


def test_the_conflict_check_is_refused_cleanly_where_publishing_is(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    card = _prepared(blueprint, "Replacement.", expected)
    # As on Windows: no O_DIRECTORY and no fcntl.
    monkeypatch.delattr(os, "O_DIRECTORY")
    monkeypatch.setattr("autoform_cli.readback.fcntl", None)

    with pytest.raises(ValueError, match="cannot safely publish"):
        readback_conflicts([card])
    with pytest.raises(ValueError, match="cannot safely publish"):
        publish_readback(card)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "missing", ["O_DIRECTORY", "O_NOFOLLOW", "open", "mkdir", "rename", "unlink", "fchmod", "fcntl"]
)
def test_a_platform_missing_a_call_publication_uses_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch, missing: str
) -> None:
    blueprint = _blueprint(tmp_path)
    card = _prepared(blueprint, "First.")
    if missing.startswith("O_") or missing == "fchmod":
        monkeypatch.delattr(os, missing)
    elif missing == "fcntl":
        monkeypatch.setattr("autoform_cli.readback.fcntl", None)
    else:
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {getattr(os, missing)})

    for attempt in (lambda: readback_conflicts([card]), lambda: publish_readback(card)):
        with pytest.raises(ValueError) as refused:
            attempt()
        assert str(refused.value) == "this platform cannot safely publish read-back cards"
    assert not (blueprint / "readbacks").exists()


def test_publication_needs_no_atomic_exchange_or_platform_specific_rename(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    # Neither Linux nor macOS, and no renameat2 or renameatx_np to call.
    monkeypatch.setattr("autoform_cli.skeleton.sys.platform", "freebsd14")
    monkeypatch.setattr("autoform_cli.skeleton.ctypes.CDLL", lambda *args, **kwargs: object())

    path = _file_card(blueprint, "First.")
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].file_hash
    assert readback_conflicts([_prepared(blueprint, "Replacement.", expected)]) == []
    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "Replacement."
