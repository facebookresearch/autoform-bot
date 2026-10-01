"""Publishing read-back cards: compare-and-swap, withdrawal, locking, and platform refusal."""

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
from pathlib import Path

import pytest

from autoform_cli import readback
from autoform_cli.readback import (
    PreparedReadback,
    _card_hash_at,
    load_readbacks,
    prepare_readback,
    publish_readback,
    readback_conflicts,
)
from tests.test_readback import _ARTICLE_ID, _blueprint, _declaration, _file_card, _staged_names

_DECLARATION = "Skel.sup_unique"


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


def _save(tmp_path: Path, path: Path, data: bytes) -> None:
    """Save ``data`` at ``path`` the way an editor does: write it elsewhere, then rename it over."""

    scratch = tmp_path / "editor-save"
    scratch.write_bytes(data)
    os.replace(scratch, path)


def _child_env(repo_root: Path) -> dict[str, str]:
    paths = (str(repo_root), os.environ.get("PYTHONPATH"))
    return {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, paths))}


# --------------------------------------------------------------------------- #
# Exchange and lock
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

    # Once this publisher has compared the card, another process publishes
    # different content over the same card, expecting the same hash.
    def check_then_start_rival(directory: int, filename: str, display_path: Path):
        nonlocal rival
        found = _card_hash_at(directory, filename, display_path)
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


def test_the_published_card_is_confirmed_before_the_lock_is_released(tmp_path: Path, monkeypatch) -> None:
    fcntl = pytest.importorskip("fcntl")
    blueprint, path, expected = _filed(tmp_path)
    identity = readback._card_identity
    held: list[bool] = []

    # As the publication confirms its card, another descriptor tries the lock.
    def try_the_lock_then_confirm(*args):
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(True)
        else:
            held.append(False)
        finally:
            os.close(directory)
        return identity(*args)

    monkeypatch.setattr("autoform_cli.readback._card_identity", try_the_lock_then_confirm)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert held == [True]


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


def test_a_card_directory_replaced_before_it_is_locked_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    moved = path.parent.with_name("moved")
    lock = readback._lock_card_directory
    replaced = False

    # Between the walk and the lock, the card directory moves away and an
    # empty directory takes its name.
    def replace_then_lock(directory: int, display_path: Path) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            path.parent.rename(moved)
            path.parent.mkdir()
        lock(directory, display_path)

    monkeypatch.setattr("autoform_cli.readback._lock_card_directory", replace_then_lock)

    with pytest.raises(ValueError, match="changed before replacement"):
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert (moved / path.name).read_bytes() == original
    assert _staged_names(moved) == []
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


# --------------------------------------------------------------------------- #
# What is compared and confirmed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("how", ["replaced", "rewritten"])
def test_a_staging_file_changed_before_the_exchange_is_not_published(tmp_path: Path, monkeypatch, how: str) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    planted = b"Planted.\n"
    changed = False

    # Once the card is compared, someone renames another file over the staged
    # card, or rewrites the staged card in place.
    def check_then_change(directory: int, filename: str, display_path: Path):
        nonlocal changed
        found = _card_hash_at(directory, filename, display_path)
        if filename == path.name and not changed:
            changed = True
            (staged,) = _staged_names(path.parent)
            if how == "replaced":
                _save(tmp_path, path.with_name(staged), planted)
            else:
                with open(path.with_name(staged), "r+b") as stream:
                    stream.truncate()
                    stream.write(planted)
        return found

    monkeypatch.setattr("autoform_cli.readback._card_hash_at", check_then_change)

    with pytest.raises(ValueError) as refused:
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    # Nothing was exchanged, so nothing was withdrawn.
    assert str(refused.value) == f"read-back staging file changed before publication: {path}"
    assert path.read_bytes() == original
    (left,) = _staged_names(path.parent)
    assert path.with_name(left).read_bytes() == planted


def test_a_card_rewritten_in_place_before_it_is_confirmed_is_not_reported(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    identity = readback._card_identity
    rewrite = b"Rewritten in place.\n"

    # After the exchange, before the publication is confirmed, someone
    # rewrites the new card through its own inode.
    def rewrite_then_confirm(*args):
        with open(path, "r+b") as stream:
            stream.truncate()
            stream.write(rewrite)
        return identity(*args)

    monkeypatch.setattr("autoform_cli.readback._card_identity", rewrite_then_confirm)

    with pytest.raises(ValueError, match="cannot confirm the published read-back"):
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert path.read_bytes() == rewrite


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_put_at_the_card_path_before_confirmation_is_refused_without_blocking(
    tmp_path: Path, monkeypatch
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    identity = readback._card_identity
    refusals: list[str] = []

    # After the exchange, before the publication is confirmed, a FIFO takes the card's name.
    def fifo_then_confirm(*args):
        path.unlink()
        os.mkfifo(path)
        return identity(*args)

    monkeypatch.setattr("autoform_cli.readback._card_identity", fifo_then_confirm)

    def file() -> None:
        try:
            _file_card(blueprint, "Replacement.", expected_card_hash=expected)
        except ValueError as exc:
            refusals.append(str(exc))

    publisher = threading.Thread(target=file)
    publisher.start()
    publisher.join(5)
    blocked = publisher.is_alive()
    if blocked:
        os.close(os.open(path, os.O_WRONLY))
        publisher.join()
    assert not blocked
    assert len(refusals) == 1 and "cannot confirm the published read-back" in refusals[0]
    assert stat.S_ISFIFO(os.lstat(path).st_mode)


@pytest.mark.parametrize("saved_bytes", ["other", "same"])
def test_refiling_identical_content_does_not_confirm_a_card_saved_over_it(
    tmp_path: Path, monkeypatch, saved_bytes: str
) -> None:
    blueprint, path, _ = _filed(tmp_path)
    edit = b"An editor's work.\n" if saved_bytes == "other" else path.read_bytes()
    saved = False

    # Right after the card is found to hold the same content, an editor saves over it.
    def check_then_save(directory: int, filename: str, display_path: Path):
        nonlocal saved
        found = _card_hash_at(directory, filename, display_path)
        if filename == path.name and not saved:
            saved = True
            _save(tmp_path, path, edit)
        return found

    monkeypatch.setattr("autoform_cli.readback._card_hash_at", check_then_save)

    with pytest.raises(ValueError, match="cannot confirm the published read-back"):
        _file_card(blueprint, "First.")
    assert path.read_bytes() == edit


# --------------------------------------------------------------------------- #
# Withdrawing a card after a collision
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("then", ["saves", "deletes"])
def test_the_newest_editor_state_stays_at_the_card_path_when_a_card_is_withdrawn(
    tmp_path: Path, monkeypatch, then: str
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    first, second = b"An editor's first save.\n", b"The editor's second save.\n"
    steps = 0

    # An editor saves once after the card is compared, then saves again, or
    # deletes the card, right after the exchange.
    def check_then_edit(directory: int, filename: str, display_path: Path):
        nonlocal steps
        found = _card_hash_at(directory, filename, display_path)
        if steps == 0 and filename == path.name:
            steps = 1
            _save(tmp_path, path, first)
        elif steps == 1 and filename != path.name:
            steps = 2
            if then == "saves":
                _save(tmp_path, path, second)
            else:
                path.unlink()
        return found

    monkeypatch.setattr("autoform_cli.readback._card_hash_at", check_then_edit)

    with pytest.raises(ValueError, match="changed concurrently") as refused:
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert steps == 2
    (kept,) = _staged_names(path.parent)
    assert path.with_name(kept).read_bytes() == first
    message = str(refused.value)
    if then == "saves":
        assert path.read_bytes() == second
        assert f"that later save was kept at {path}" in message
    else:
        assert not path.exists()
        assert f"{path} was deleted while this write withdrew its card: the deletion stands" in message
    assert f"preserved at {path.with_name(kept)}" in message


def test_a_card_that_cannot_be_put_back_is_preserved_and_named(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    edit = b"An editor's work.\n"
    rename = readback.atomic_rename
    saved = False
    renames = 0

    def check_then_save(directory: int, filename: str, display_path: Path):
        nonlocal saved
        found = _card_hash_at(directory, filename, display_path)
        if filename == path.name and not saved:
            saved = True
            _save(tmp_path, path, edit)
        return found

    # The exchange goes through; the one that would put the editor's card back fails.
    def fail_after_the_exchange(*args, **kwargs):
        nonlocal renames
        renames += 1
        if renames > 1:
            raise OSError(errno.EIO, "input/output error")
        return rename(*args, **kwargs)

    monkeypatch.setattr("autoform_cli.readback._card_hash_at", check_then_save)
    monkeypatch.setattr("autoform_cli.readback.atomic_rename", fail_after_the_exchange)

    with pytest.raises(ValueError, match="changed concurrently") as refused:
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    (kept,) = _staged_names(path.parent)
    assert path.with_name(kept).read_bytes() == edit
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "Replacement."
    message = str(refused.value)
    assert (
        f"the card this write replaced could not be put back ([Errno {errno.EIO}] input/output error) "
        f"and was preserved at {path.with_name(kept)}; {path} holds the new card"
    ) in message


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


def test_an_interruption_as_the_staging_file_is_removed_still_removes_it(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, _ = _filed(tmp_path)
    original = path.read_bytes()
    unlink = readback._unlink_quietly
    interrupted = False

    # The interrupt lands as a refused write is about to remove its staging file.
    def interrupt_first_unlink(directory: int, filename: str) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        unlink(directory, filename)

    monkeypatch.setattr("autoform_cli.readback._unlink_quietly", interrupt_first_unlink)

    with pytest.raises(KeyboardInterrupt):
        _file_card(blueprint, "Replacement.", expected_card_hash="sha256:" + "1" * 64)
    assert interrupted
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


@pytest.mark.parametrize("lacking", ["platform", "call"])
def test_an_unsupported_platform_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch, lacking: str
) -> None:
    blueprint = _blueprint(tmp_path)
    card = _prepared(blueprint, "First.")
    if lacking == "platform":
        monkeypatch.setattr("autoform_cli.skeleton.sys.platform", "freebsd14")
    else:
        monkeypatch.setattr("autoform_cli.skeleton.ctypes.CDLL", lambda *args, **kwargs: object())

    with pytest.raises(ValueError, match="cannot safely publish"):
        readback_conflicts([card])
    with pytest.raises(ValueError, match="cannot safely publish"):
        publish_readback(card)
    assert not (blueprint / "readbacks").exists()
