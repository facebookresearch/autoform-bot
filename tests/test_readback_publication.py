"""Publishing read-back cards: the lock, compare-and-swap, faults, unreadable cards, and platform refusal."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import sys
import threading
import types
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import readback
from autoform_cli.readback import (
    PreparedReadback,
    load_readbacks,
    publish_readback,
    readback_for,
    readback_path,
)
from tests.test_readback import _ARTICLE_ID, _blueprint, _declaration, _file_card, _prepared, _staged_names

_DECLARATION = "Skel.sup_unique"
_CARD_LIMIT = 4 * 1024 * 1024


def _filed(tmp_path: Path) -> tuple[Path, Path, str]:
    """A blueprint holding one card, that card's path, and the hash that replaces it."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    return blueprint, path, load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].file_hash


def _byte_hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


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


def _patch_os(monkeypatch, **calls) -> None:
    """Give publication an ``os`` with ``calls`` replaced."""

    patched = _Os()
    for name, call in calls.items():
        setattr(patched, name, call)
    monkeypatch.setattr(readback, "os", patched)


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

    _patch_os(monkeypatch, **{call: failing})
    return failed


def _restrict(path: Path, mode: int) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    path.chmod(mode)


def _fcntl(monkeypatch, **calls) -> None:
    """Give publication an ``fcntl`` with only the real locking calls, and ``calls`` replacing them."""

    fcntl = pytest.importorskip("fcntl")
    locking = {"LOCK_EX": fcntl.LOCK_EX, "LOCK_NB": fcntl.LOCK_NB, "LOCK_UN": fcntl.LOCK_UN, "flock": fcntl.flock}
    monkeypatch.setattr("autoform_cli.readback.fcntl", types.SimpleNamespace(**{**locking, **calls}))


# --------------------------------------------------------------------------- #
# The lock and the rename
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("content", ["Replacement.", "Rival."])
def test_a_second_publisher_waits_for_the_lock_then_compares_against_the_first_card(
    tmp_path: Path, monkeypatch, content: str
) -> None:
    fcntl = pytest.importorskip("fcntl")
    blueprint, path, expected = _filed(tmp_path)
    waiting, results, held = threading.Event(), [], []
    check, replace_file = readback._card_hash_at, os.replace

    def flock(descriptor: int, operation: int) -> None:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            waiting.set()
            raise

    def publish_second() -> None:
        try:
            results.append(_file_card(blueprint, content, expected_card_hash=expected))
        except ValueError as exc:
            results.append(str(exc))

    second = threading.Thread(target=publish_second)

    # Once the first publisher has compared the card, a second one, expecting the same card, starts and waits.
    def check_then_start_second(checked: Path):
        found = check(checked)
        if second.ident is None:
            second.start()
            assert waiting.wait(10)
        return found

    def try_the_lock_then_rename(*args, **kwargs) -> None:
        held.append(not _unlocked(path.parent))
        replace_file(*args, **kwargs)

    _fcntl(monkeypatch, flock=flock)
    monkeypatch.setattr("autoform_cli.readback._card_hash_at", check_then_start_second)
    monkeypatch.setattr(os, "replace", try_the_lock_then_rename)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    second.join()
    # The same content needs no write; other content no longer finds the card it named.
    refusal = f"read-back changed before replacement: expected {expected!r}, found {_byte_hash(path.read_bytes())!r}"
    assert results == [path if content == "Replacement." else refusal]
    assert held == [True] and _unlocked(path.parent)
    assert load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)].text == "Replacement."
    assert _staged_names(path.parent) == []


def test_a_child_process_started_during_publication_does_not_keep_the_lock(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("fcntl")
    blueprint, path, expected = _filed(tmp_path)
    lock = readback._lock_card_directory
    children: list[subprocess.Popen[bytes]] = []

    # Once the lock is held, the publisher starts a long-lived child that
    # inherits the locked descriptor.
    def lock_then_start_a_child(directory: int, display_path: Path) -> None:
        lock(directory, display_path)
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
                pass_fds=(directory,),
            )
        )

    monkeypatch.setattr("autoform_cli.readback._lock_card_directory", lock_then_start_a_child)

    try:
        assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
        assert len(children) == 1 and children[0].poll() is None
        assert _unlocked(path.parent)
    finally:
        for child in children:
            child.communicate(timeout=30)


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

    _fcntl(monkeypatch, flock=flock)

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


@pytest.mark.parametrize("naming", ["nothing", "another card", "a removed card"])
def test_different_content_replaces_a_card_only_by_naming_its_hash(tmp_path: Path, monkeypatch, naming: str) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    named = {"nothing": None, "another card": "sha256:" + "1" * 64, "a removed card": expected}[naming]
    card = _prepared(blueprint, "Replacement.", named)
    if naming == "nothing":
        refusal = f"read-back already exists with different content: {path}; retry with expected_card_hash={expected!r}"
    else:
        found = "no card" if naming == "a removed card" else repr(expected)
        refusal = f"read-back changed before replacement: expected {named!r}, found {found}"
        if naming == "a removed card":
            path.unlink()
    open_, created = os.open, []

    def counted(name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            created.append(name)
        return open_(name, flags, *args, **kwargs)

    _patch_os(monkeypatch, open=counted)

    # The write refuses, names the hash of the card's bytes, and creates nothing.
    with pytest.raises(ValueError) as refused:
        publish_readback(card)
    assert str(refused.value) == refusal and created == []
    assert expected == _byte_hash(original)
    assert (path.read_bytes() if path.exists() else None) == (None if naming == "a removed card" else original)


@pytest.mark.parametrize("call", ["fstat", "read"])
def test_a_card_that_fails_to_read_during_the_compare_is_refused_with_its_path_and_reason(
    tmp_path: Path, monkeypatch, call: str
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    _fail(monkeypatch, call, lambda descriptor, *args: stat.S_ISREG(os.fstat(descriptor).st_mode))

    with pytest.raises(ValueError) as refused:
        publish_readback(_prepared(blueprint, "Replacement.", expected))
    assert str(refused.value) == f"cannot read existing read-back: {path}: injected input/output error"
    assert path.read_bytes() == original
    assert _staged_names(path.parent) == []
    assert _unlocked(path.parent)


def test_an_identical_card_is_refiled_without_writing_even_in_a_read_only_directory(tmp_path: Path) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    same, other = _prepared(blueprint, "First."), _prepared(blueprint, "Replacement.", expected)
    _restrict(path.parent, 0o555)
    try:
        assert publish_readback(same) == path
        # A real replacement passes the compare-and-swap, then cannot write.
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

    # A write names it by that hash, like any card, and replaces it.
    with pytest.raises(ValueError, match=f"expected_card_hash={loaded.file_hash!r}"):
        publish_readback(_prepared(blueprint, "Replacement."))
    assert publish_readback(_prepared(blueprint, "Replacement.", loaded.file_hash)) == path
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

    _patch_os(monkeypatch, read=counted)

    loaded = load_readbacks(blueprint)[(_ARTICLE_ID, _DECLARATION)]
    assert f"card is over the {_CARD_LIMIT}-byte limit for a card file" in loaded.validate()
    assert loaded.text == "" and loaded.file_hash is None
    assert sum(sizes) == _CARD_LIMIT + 1
    sizes.clear()

    with pytest.raises(ValueError, match="over the .*-byte limit .*; remove it to file a new card"):
        publish_readback(_prepared(blueprint, "Replacement.", "sha256:" + "1" * 64))
    assert sum(sizes) == _CARD_LIMIT + 1
    assert path.stat().st_size == 2 * _CARD_LIMIT
    assert _staged_names(path.parent) == []


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
        _restrict(path.parent, 0o555)
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
    # The reason alone, not every field it left unread.
    assert loaded.validate() == (reason,)
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
        if Path(name) == path:
            raise OSError(error, os.strerror(error))
        return open_(name, flags, *args, **kwargs)

    _patch_os(monkeypatch, open=failing)

    loaded = load_readbacks(blueprint)
    if reported:
        assert f"card cannot be read: {os.strerror(error)}" in loaded[(_ARTICLE_ID, _DECLARATION)].validate()
    else:
        # A card removed since the listing, or a link, is not there to read.
        assert loaded == {}


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_a_fifo_or_directory_at_a_card_path_is_reported_and_refused_without_blocking(tmp_path: Path, kind: str) -> None:
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("needs FIFOs")
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, _DECLARATION)
    path.parent.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
        (path / "notes.txt").write_text("keep\n", encoding="utf-8")
    results: list[object] = []

    def attempt(call) -> None:
        try:
            results.append(call())
        except ValueError as exc:
            results.append(str(exc))

    for call in (lambda: load_readbacks(blueprint), lambda: publish_readback(_prepared(blueprint, "First."))):
        worker = threading.Thread(target=attempt, args=(call,), daemon=True)
        worker.start()
        worker.join(5)
        blocked = worker.is_alive()
        if blocked:
            # A call stuck opening the FIFO is let go, so the test fails rather than hangs.
            os.close(os.open(path, os.O_WRONLY))
            worker.join()
        assert not blocked

    # Reported as an invalid card, as the writer refuses it, rather than as a missing one; and left in place.
    loaded, refused = results
    assert "card path is not a regular file" in loaded[(_ARTICLE_ID, _DECLARATION)].validate()
    assert refused == f"read-back destination is not a regular file: {path}"
    left = stat.S_ISFIFO(os.lstat(path).st_mode) if kind == "fifo" else (path / "notes.txt").read_bytes() == b"keep\n"
    assert left and _staged_names(path.parent) == []


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

    assert readback_for(load_readbacks(blueprint), _ARTICLE_ID, declaration.name).validate() == (reason,)


# --------------------------------------------------------------------------- #
# Links and missing directories on the way to a card
# --------------------------------------------------------------------------- #


def test_a_card_that_is_a_symlink_is_neither_followed_nor_replaced(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    outside = _file_card(_blueprint(tmp_path / "elsewhere"), "Outside.")
    original = outside.read_bytes()
    path = readback_path(blueprint, _ARTICLE_ID, _DECLARATION)
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)

    assert load_readbacks(blueprint) == {}
    with monkeypatch.context() as windows:
        windows.delattr(os, "O_NOFOLLOW")
        assert load_readbacks(blueprint) == {}
    with pytest.raises(ValueError, match=f"cannot safely inspect existing read-back: {path}"):
        publish_readback(_prepared(blueprint, "Replacement.", _byte_hash(original)))
    assert path.is_symlink() and outside.read_bytes() == original
    assert _staged_names(path.parent) == []


@pytest.mark.parametrize("linked", ["readbacks", "article"])
def test_a_card_directory_reached_through_a_symlink_is_neither_loaded_nor_written(tmp_path: Path, linked: str) -> None:
    blueprint = _blueprint(tmp_path)
    outside = _file_card(_blueprint(tmp_path / "elsewhere"), "Outside.")
    original = outside.read_bytes()
    target = outside.parent if linked == "article" else outside.parent.parent
    link = readback_path(blueprint, _ARTICLE_ID, _DECLARATION).parent
    if linked == "readbacks":
        link = link.parent
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)

    assert load_readbacks(blueprint) == {}
    with pytest.raises(ValueError, match=f"refusing a symlink in the read-back path: {link}"):
        publish_readback(_prepared(blueprint, "Replacement.", _byte_hash(original)))
    assert outside.read_bytes() == original
    assert sorted(os.listdir(outside.parent)) == [outside.name]


def test_a_link_put_where_a_card_directory_is_being_made_is_refused(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    mkdir = os.mkdir

    # Just as the card directory is made, a link to another directory takes its name.
    def link_then_mkdir(path, mode: int = 0o777) -> None:
        if Path(path).name == _ARTICLE_ID:
            os.symlink(outside, path)
        mkdir(path, mode)

    _patch_os(monkeypatch, mkdir=link_then_mkdir)

    with pytest.raises(ValueError, match=f"refusing a symlink in the read-back path: .*/readbacks/{_ARTICLE_ID}"):
        _file_card(blueprint, "First.")
    assert list(outside.iterdir()) == []


def test_a_card_directory_that_cannot_be_made_or_opened_is_refused_with_the_reason(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    card = _prepared(blueprint, "First.")
    _restrict(blueprint, 0o555)
    try:
        with pytest.raises(ValueError) as refused:
            publish_readback(card)
    finally:
        blueprint.chmod(0o755)
    assert str(refused.value) == (
        f"cannot create read-back directory component: {card.blueprint / 'readbacks'}: Permission denied"
    )
    assert not (blueprint / "readbacks").exists()

    directory = card.path.parent
    directory.mkdir(parents=True)
    _restrict(directory, 0o000)
    try:
        with pytest.raises(ValueError) as refused:
            publish_readback(card)
    finally:
        directory.chmod(0o755)
    assert str(refused.value) == f"cannot open read-back directory: {directory}: Permission denied"


@pytest.mark.parametrize("missing", ["article_id", "blueprint"])
def test_a_card_whose_article_id_is_invalid_or_whose_blueprint_is_gone_is_refused(tmp_path: Path, missing: str) -> None:
    blueprint = _blueprint(tmp_path)
    if missing == "article_id":
        escape = blueprint / "readbacks" / ".." / "escape" / "Skel.sup_unique.md"
        card = PreparedReadback(blueprint, "../escape", _DECLARATION, escape, "First.\n", None)
        refusal = "invalid article_id for a read-back: '../escape'"
    else:
        card = _prepared(blueprint, "First.")
        blueprint.rename(tmp_path / "moved")
        refusal = f"cannot create read-back directory component: {card.blueprint / 'readbacks'}: No such file"

    with pytest.raises(ValueError, match=refusal):
        publish_readback(card)
    assert not (blueprint / "readbacks").exists() and not (blueprint / "escape").exists()


# --------------------------------------------------------------------------- #
# A failing call, or an interruption, at each step of a write
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("call", "when"),
    [
        ("open", lambda name, flags, *args, **kwargs: bool(flags & os.O_CREAT)),
        ("write", lambda *args: True),
        ("fchmod", lambda *args: True),
        ("fsync", lambda *args: True),
        ("replace", lambda *args, **kwargs: True),
    ],
    ids=["open", "write", "fchmod", "fsync", "replace"],
)
def test_a_call_that_fails_before_the_rename_leaves_the_card_and_no_staging_file(
    tmp_path: Path, monkeypatch, call: str, when
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    failed = _fail(monkeypatch, call, when)

    with pytest.raises(ValueError) as refused:
        _file_card(blueprint, "Replacement.", expected_card_hash=expected)
    assert str(refused.value) == f"cannot publish read-back: {path}: [Errno {errno.EIO}] injected input/output error"
    assert len(failed) == 1
    assert path.read_bytes() == original
    assert _staged_names(path.parent) == []
    assert _unlocked(path.parent)


@pytest.mark.parametrize(
    ("call", "when"),
    [
        # The card is being read for the compare.
        ("read", lambda descriptor, size: True),
        # The staging file was just created, and its descriptor not yet kept.
        ("open", lambda name, flags, *args, **kwargs: bool(flags & os.O_CREAT)),
        # The new card is about to be renamed into place.
        ("replace", lambda *args, **kwargs: True),
    ],
    ids=["read", "stage", "rename"],
)
def test_an_interruption_just_after_a_call_returns_closes_each_descriptor_once_and_leaves_no_file(
    tmp_path: Path, monkeypatch, call: str, when
) -> None:
    blueprint, path, expected = _filed(tmp_path)
    original = path.read_bytes()
    card = _prepared(blueprint, "Replacement.", expected)
    real, fired = getattr(os, call), []

    def interrupted(*args, **kwargs):
        hit = not fired and when(*args, **kwargs)
        result = real(*args, **kwargs) if call != "replace" else None
        if hit:
            fired.append(call)
            if call == "open":
                os.close(result)
            raise KeyboardInterrupt
        return result

    _patch_os(monkeypatch, **{call: interrupted})

    # A descriptor closed twice would raise, or close another file, in place of the interrupt.
    with pytest.raises(KeyboardInterrupt):
        publish_readback(card)
    assert fired == [call] and path.read_bytes() == original
    assert _staged_names(path.parent) == [] and _unlocked(path.parent)


def test_an_interruption_as_any_step_of_a_write_returns_leaves_no_lock_or_staging_file(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
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
        assert _unlocked(path.parent), step
    assert step > 10


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


def test_short_writes_still_stage_the_whole_card(tmp_path: Path, monkeypatch) -> None:
    blueprint, path, expected = _filed(tmp_path)
    write = os.write
    calls = 0

    def write_a_little(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        return write(descriptor, bytes(data[:7]))

    _patch_os(monkeypatch, write=write_a_little)

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


@pytest.mark.parametrize("missing", ["O_DIRECTORY", "O_NOFOLLOW", "fcntl"])
def test_a_platform_missing_a_call_publication_uses_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch, missing: str
) -> None:
    blueprint = _blueprint(tmp_path)
    card = _prepared(blueprint, "First.")
    if missing == "fcntl":
        monkeypatch.setattr("autoform_cli.readback.fcntl", None)
    else:
        monkeypatch.delattr(os, missing)

    with pytest.raises(ValueError, match="this platform cannot safely publish read-back cards"):
        publish_readback(card)
    assert not (blueprint / "readbacks").exists()
