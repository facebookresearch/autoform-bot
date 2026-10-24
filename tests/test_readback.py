from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.readback import (
    READBACK_HEADING,
    SKELETON_HEADING,
    formula_problems,
    load_readbacks,
    prepare_readback,
    publish_readback,
    readback_for,
    readback_keys,
    readback_path,
)
from autoform_cli.skeleton import DeclarationSkeleton, TrustedDeclaration, evidence_hash_of
from tests.skeleton_fixtures import SEMANTIC as _PROP
from tests.skeleton_fixtures import declaration as fixture_declaration

_BODY = '{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}},"value":{"bvar":0}}}'
_ARTICLE_ID = "af_0123456789abcdef01234567"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _trusted(source: str, semantic: str = _BODY) -> TrustedDeclaration:
    comments: list[tuple[int, int]] = []
    if source.startswith("/--"):
        end = source.index("-/") + 2
        comments.append((0, len(source[:end].encode("utf-8"))))
    line_comment = source.find("--", 3)
    if line_comment >= 0:
        comments.append((len(source[:line_comment].encode("utf-8")), len(source.encode("utf-8"))))
    return TrustedDeclaration(
        name="Skel.IsSup",
        kind="def",
        module="Skel.Defs",
        path="Skel/Defs.lean",
        start_line=3,
        end_line=4,
        signature="Skel.IsSup {S : Type} [LinearOrder S] (E : Set S) (b : S) : Prop",
        raw_signature="Skel.IsSup {S : Type} [LinearOrder S] (E : Set S) (b : S) : Prop",
        semantic=semantic,
        depends=(),
        source=source,
        source_comments=tuple(comments),
    )


def _declaration(
    source: str = "/-- doc -/\ndef IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x ≤ b",
    semantic: str = _BODY,
) -> DeclarationSkeleton:
    """A theorem resting on one definition; ``semantic`` is the definition's elaborated body."""

    return fixture_declaration(
        "Skel.sup_unique",
        module="Skel.Main",
        path="Skel/Main.lean",
        start_line=10,
        end_line=14,
        signature="Skel.sup_unique {E : Set ℝ} {a b : ℝ} (ha : Skel.IsSup E a) (hb : Skel.IsSup E b) : a = b",
        raw_signature="Skel.sup_unique {E : Set ℝ} {a b : ℝ} (ha : Skel.IsSup E a) (hb : Skel.IsSup E b) : a = b",
        depends=("Skel.IsSup",),
        trusted=(_trusted(source, semantic),),
        axioms=("propext",),
        axiom_semantics=(("propext", _PROP),),
    )


def write_readback(blueprint: str | Path, **fields) -> Path:
    """File a card as ``record`` does: run every check, then publish it."""

    return publish_readback(prepare_readback(blueprint, **fields))


def _blueprint(root: Path) -> Path:
    blueprint = root / "blueprint"
    chapter = blueprint / "roadmap" / "basics"
    chapter.mkdir(parents=True)
    (blueprint / "roadmap" / "README.md").write_text("# Roadmap\n\n- [Basics](basics/README.md)\n", encoding="utf-8")
    (chapter / "README.md").write_text("# Basics\n\nOne result.\n\n- [Sup unique](sup-unique.md)\n", encoding="utf-8")
    (chapter / "sup-unique.md").write_text(
        f"---\narticle_id: {_ARTICLE_ID}\ndeclaration: theorem\nlean: Skel.sup_unique\nstatement: formalized\n---\n\n"
        "# Sup unique\n\nA supremum is unique.\n\n## Depends on\n\nNone.\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage"
    coverage.mkdir()
    (coverage / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| All | OUT | scratch |\n",
        encoding="utf-8",
    )
    return blueprint


def _file_card(blueprint: Path, text: str, *, write=write_readback, **options):
    """File a card, or with ``write=prepare_readback`` only prepare it, defaulting the fields left out.

    ``declaration`` may be a skeleton, whose name and hashes the card records.
    """

    declaration = options.pop("declaration", None) or _declaration()
    if isinstance(declaration, DeclarationSkeleton):
        options.setdefault("statement_hash", declaration.statement_hash)
        options.setdefault("packet_hash", declaration.evidence_hash)
        options.setdefault("packet_text", declaration.blind_text())
        declaration = declaration.name
    options.setdefault("article_id", _ARTICLE_ID)
    options.setdefault("model", "m")
    return write(blueprint, text=text, declaration=declaration, **options)


def _prepared(blueprint: Path, text: str, expected: str | None = None):
    return _file_card(blueprint, text, write=prepare_readback, expected_card_hash=expected)


def _staged_names(directory: Path) -> list[str]:
    return [name for name in os.listdir(directory) if name.startswith(".autoform-readback-")]


# --------------------------------------------------------------------------- #
# Read-back files
# --------------------------------------------------------------------------- #


def test_readbacks_are_filed_per_declaration_and_validate(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()

    path = _file_card(blueprint, "Let $E$ …\r\n\n```\n", model="test-model")

    assert path == readback_path(blueprint, _ARTICLE_ID, "Skel.sup_unique")
    # The file is a self-contained review card: the skeleton shown to the
    # auditor, then the testimony as typed, with line ends as "\n", in a text
    # block no line of it can close.
    card = path.read_text(encoding="utf-8")
    assert card.startswith(f'---\nschema: autoform-readback/v1\narticle_id: "{_ARTICLE_ID}"\n')
    assert f"\nstatement: {declaration.statement_hash}\npacket: {declaration.evidence_hash}\n" in card
    assert f"\n{SKELETON_HEADING}\n\n```lean\n" in card
    assert card.endswith(f"\n{READBACK_HEADING}\n\n````text\nLet $E$ …\n\n```\n````\n")
    assert "def IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x ≤ b" in card and "/-- doc -/" not in card
    readback = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert readback.article_id == _ARTICLE_ID and readback.valid
    assert (readback.model, readback.text) == ("test-model", "Let $E$ …\n\n```")
    assert (readback.statement_hash, readback.packet_hash) == (declaration.statement_hash, declaration.evidence_hash)
    # A proof edit, here one that stops using `propext`, leaves both hashes the card records.
    edited = replace(declaration, axioms=(), axiom_semantics=())
    assert (edited.statement_hash, edited.evidence_hash) == (readback.statement_hash, readback.packet_hash)
    other = _declaration(source="def IsSup (E : Set S) (b : S) : Prop := ∀ x ∈ E, x < b")
    assert other.evidence_hash != readback.packet_hash
    # A card changed after it was read, or built by hand, is not the card its hash names.
    assert replace(readback, text="Other.").validate() == ("whole-file hash does not match the canonical card bytes",)
    assert replace(readback, model="\ud800").validate() == ("whole-file hash does not match the canonical card bytes",)


def test_card_paths_stay_inside_the_readbacks_tree(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    for article_id, declaration in (
        ("../escape", "Skel.sup_unique"), (_ARTICLE_ID, ""), ("basics//sup-unique", "Skel.sup_unique")
    ):
        with pytest.raises(ValueError):
            readback_path(blueprint, article_id, declaration)
    encoded = readback_path(blueprint, _ARTICLE_ID, "../Skel.sup_unique")
    assert encoded.parent == blueprint / "readbacks" / _ARTICLE_ID
    assert "/" not in encoded.name and encoded.suffix == ".md"


@pytest.mark.parametrize(
    ("name", "spelled"),
    [("Aux.lemma", "%41ux.lemma"), ("con.x", "%63on.x"), ("COM1.y", "%43OM1.y"), ("LPT0.z.w", "%4CPT0.z.w")],
)
def test_no_card_filename_is_a_windows_device_name_before_its_first_dot(
    tmp_path: Path, name: str, spelled: str
) -> None:
    """Windows takes such a file for the device, so Git for Windows could not check the card out."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.", declaration=replace(_declaration(), name=name))

    assert path.name.split("--")[0] == spelled
    assert load_readbacks(blueprint)[(_ARTICLE_ID, name)].valid
    # Without a dot the digest follows the name, and the name is left as it is.
    assert readback_path(blueprint, _ARTICLE_ID, "Aux").name.startswith("Aux--")


def _testimony_block(card: str, testimony: str) -> str:
    return re.sub(r"(?s)```text\n.*\n```\n\Z", f"```text\n{testimony}\n```\n", card)


#: A hand edit of a filed card, as (text, replacement) or a function of the card, and the reason it is invalid.
_DAMAGES = {
    "missing-schema": (("schema: autoform-readback/v1\n", ""), "missing frontmatter field 'schema'"),
    "wrong-schema": (("schema: autoform-readback/v1", "schema: future"), "unsupported read-back schema"),
    "unknown-field": (("---\n", "---\nsurprise: value\n"), "unknown frontmatter field 'surprise'"),
    "duplicate-field": (('model: "m"\n', 'model: "m"\nmodel: "x"\n'), "field 'model' appears more than once"),
    "missing-packet": ((f"packet: {_declaration().evidence_hash}\n", ""), "no packet hash"),
    "malformed-packet": ((_declaration().evidence_hash, "not-a-hash"), "malformed packet hash"),
    "malformed-statement": ((_declaration().statement_hash, "approved"), "malformed statement hash"),
    # The card is read in the vault, so the Lean it shows is the evidence.
    "altered-packet": (("x ≤ b", "x < b"), "the displayed skeleton does not match the recorded packet hash"),
    "interstitial": (
        (f"\n{READBACK_HEADING}\n", f"\n## Unverified claim\n\nInjected.\n\n{READBACK_HEADING}\n"),
        "card body must be one skeleton block followed by one read-back block",
    ),
    "testimony-outside-its-block": (("```text\nFine.\n```", "Fine."), "card body must be one skeleton block"),
    "empty-model": (('model: "m"\n', "model:\n"), "nonempty model label"),
    "stacked-model": (('model: "m"', 'model: "m\u0301\u0301\u0301"'), "frontmatter field 'model' must be printable"),
    "long-frontmatter": (("---\n", "---\n" + "\n" * 40), "no closing frontmatter delimiter in its first 32 lines"),
    "reordering-model": (('model: "m"', 'model: "m\u05d0"'), "frontmatter field 'model' must be printable"),
    "wrong-article": ((_ARTICLE_ID, "af_" + "a" * 24), "does not match the card path article_id"),
    "wrong-declaration": (('"Skel.sup_unique"', '"Skel.other"'), "does not match the card path"),
    # A hundred thousand nested arrays are deeper than the JSON decoder goes on Python 3.10 to 3.13,
    # and on 3.14, which goes as deep as the stack allows, in the 8 MiB stacks CI runs with.
    **{
        f"nested-{field}": (
            (f"{field}: {json.dumps(value)}\n", f"{field}: {'[' * 100_000}\n"),
            f"frontmatter field '{field}' must be a JSON double-quoted string",
        )
        for field, value in (("article_id", _ARTICLE_ID), ("declaration", "Skel.sup_unique"), ("model", "m"))
    },
    "missing-skeleton-block": (
        lambda card: re.sub(f"{SKELETON_HEADING}.*?(?={READBACK_HEADING})", "", card, flags=re.S),
        "card body must be one skeleton block",
    ),
    "empty-testimony": (lambda card: _testimony_block(card, " "), "card contains no nonempty read-back testimony"),
    "unsafe-testimony": (lambda card: _testimony_block(card, "Fi\u200bne."), "invisible or reordering characters"),
    "longer-fence": (("```text\nFine.\n```", "````text\nFine.\n````"), "not in canonical autoform-readback/v1 form"),
    "crlf": (lambda card: card.replace("\n", "\r\n"), "card body must be one skeleton block"),
}


@pytest.mark.parametrize("damage", _DAMAGES)
def test_a_card_edited_by_hand_is_invalid_and_named_by_the_hash_of_its_bytes(tmp_path: Path, damage: str) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    edit, reason = _DAMAGES[damage]
    card = path.read_text(encoding="utf-8")
    path.write_bytes((edit(card) if callable(edit) else card.replace(*edit, 1)).encode("utf-8"))

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert readback.file_hash == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert reason in "; ".join(readback.validate())


def test_a_card_body_that_repeats_its_closing_lines_loads_quickly(tmp_path: Path) -> None:
    """A body that repeats the fence and heading that end the packet would make
    a backtracking parse quadratic in its length; at the card size limit the
    card still loads at once, as an invalid card."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    card = path.read_text(encoding="utf-8")
    head = card[: card.index(f"\n{SKELETON_HEADING}\n")]
    repeat = f"```\n\n{READBACK_HEADING}\n\n```text\nx\n"
    body = f"\n{SKELETON_HEADING}\n\n```lean\n" + repeat * ((4 * 1024 * 1024 - len(head)) // len(repeat) - 1)
    path.write_text(head + body, encoding="utf-8")

    started = time.process_time()
    readback = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert time.process_time() - started < 2
    assert not readback.valid


def test_a_card_of_junk_frontmatter_lines_loads_quickly(tmp_path: Path) -> None:
    """Only the first lines can hold the frontmatter, so a card of millions of
    lines before its closing delimiter costs no more to load than a card."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    card = path.read_text(encoding="utf-8")
    junk = "k: v\n" * ((4 * 1024 * 1024 - len(card.encode("utf-8"))) // 5)
    path.write_text(card.replace("---\n", "---\n" + junk, 1), encoding="utf-8")

    started = time.process_time()
    readback = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert time.process_time() - started < 1
    assert "card has no closing frontmatter delimiter in its first 32 lines" in readback.validate()


def test_a_card_one_folder_deeper_with_a_surrogate_model_is_invalid(tmp_path: Path) -> None:
    """A refused model label is not kept, and a card in a folder below its
    article's takes no article_id from its own frontmatter, so judging the
    card raises nothing."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    deeper = path.parent / "nested" / path.name
    deeper.parent.mkdir()
    path.rename(deeper)
    deeper.write_text(deeper.read_text(encoding="utf-8").replace('model: "m"', 'model: "\\ud800"'), encoding="utf-8")

    (card,) = load_readbacks(blueprint).values()
    assert not card.valid
    assert card.model is None and card.article_id == f"{_ARTICLE_ID}/nested"
    assert any(error.startswith("frontmatter field 'model' must be printable") for error in card.validate())


def test_a_card_holding_a_code_point_this_python_leaves_unassigned_loads_as_valid(tmp_path: Path) -> None:
    """A later Python may assign it, so only a write refuses it, and every
    Python that loads the card judges it the same."""

    blueprint = _blueprint(tmp_path)
    unassigned = "\u0378"
    with pytest.raises(ValueError, match="unassigned in this Python's Unicode data"):
        _file_card(blueprint, f"Fine {unassigned}.")
    with pytest.raises(ValueError, match="model label must be printable"):
        _file_card(blueprint, "Fine.", model=f"m{unassigned}")
    path = _file_card(blueprint, "Fine.")
    card = path.read_text(encoding="utf-8")
    path.write_text(
        card.replace('model: "m"', f'model: "m{unassigned}"').replace("Fine.", f"Fine {unassigned}."),
        encoding="utf-8",
    )

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")]
    assert readback.validate() == ()
    assert (readback.model, readback.text) == (f"m{unassigned}", f"Fine {unassigned}.")


def test_messages_about_a_card_name_a_few_problems_each_escaped_and_cut(tmp_path: Path) -> None:
    """A card can hold any number of problems, and characters a reader would
    not see; a message names the first few, with those characters escaped and
    each name cut short."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    keys = ["k\u202e" + "x" * 200, *(f"k{index}" for index in range(1, 20))]
    card = path.read_text(encoding="utf-8")
    path.write_text(card.replace("---\n", "---\n" + "".join(f"{key}: v\n" for key in keys), 1), encoding="utf-8")

    errors = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].validate()
    assert errors[:9] == (
        "unknown frontmatter field 'k\\u202e" + "x" * 54 + "...'",
        *(f"unknown frontmatter field 'k{index}'" for index in range(1, 8)),
        "and 12 more frontmatter errors",
    )
    name = "a\n" + "b" * 62
    assert formula_problems(f"\\begin{{{name}}}x\\end{{{name}}}") == (
        "TeX outside the read-back allowlist: \\begin{a\\u000a" + "b" * 47 + "...",
    )


def test_a_second_file_keyed_like_a_card_makes_that_card_invalid(tmp_path: Path) -> None:
    """A file beside a card whose name spells the same declaration claims the
    same identity, so neither is current: the card is reported invalid,
    naming both files."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    stray = path.with_name("Skel.sup_unique.md")
    stray.write_bytes(path.read_bytes())

    cards = load_readbacks(blueprint)
    assert list(cards) == [(_ARTICLE_ID, "Skel.sup_unique")]
    card = cards[(_ARTICLE_ID, "Skel.sup_unique")]
    assert card.path == path
    assert card.validate() == (
        f"multiple cards claim the same declaration identity: 'readbacks/{_ARTICLE_ID}/{path.name}' and "
        f"'readbacks/{_ARTICLE_ID}/{stray.name}'",
    )


def test_a_declaration_with_a_card_under_each_of_its_keys_has_one_invalid_card(tmp_path: Path) -> None:
    """A card is keyed by its name, or by its filename's stem when that does
    not spell the name, as for a ``.MD`` file; a card under each key is a
    duplicate, as two under one are."""

    blueprint = _blueprint(tmp_path)
    _file_card(blueprint, "Fine.")
    by_name, by_filename = readback_keys(_ARTICLE_ID, "Skel.sup_unique")
    card = load_readbacks(blueprint)[by_name]
    other = replace(card, declaration=by_filename[1], path=card.path.with_suffix(".MD"))

    assert readback_for({by_filename: other}, *by_name) == replace(other, declaration="Skel.sup_unique")
    found = readback_for({by_name: card, by_filename: other}, *by_name)
    assert found is not None and found.path == card.path
    assert found.validate() == (
        f"multiple cards claim the same declaration identity: 'readbacks/{_ARTICLE_ID}/{card.path.name}' and "
        f"'readbacks/{_ARTICLE_ID}/{other.path.name}'",
    )


def test_a_stray_file_named_like_an_empty_declaration_loads_as_an_invalid_card(tmp_path: Path) -> None:
    """``--`` and a digest decodes to an empty name, which no card has; it
    does not stop the other cards from loading."""

    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    (path.parent / ("--" + "0" * 64 + ".md")).write_text("stray\n", encoding="utf-8")

    cards = load_readbacks(blueprint)
    assert cards[(_ARTICLE_ID, "Skel.sup_unique")].valid
    assert [card.valid for key, card in cards.items() if key != (_ARTICLE_ID, "Skel.sup_unique")] == [False]


@pytest.mark.skipif(sys.platform == "darwin", reason="APFS refuses a file name that is not UTF-8")
def test_a_card_whose_path_is_not_utf8_is_unreadable(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    os.rename(path, os.fsencode(path.parent) + b"/\xff.md")
    # Bytes that are not UTF-8 either do not hide the reason the path gives.
    with open(os.fsencode(path.parent) + b"/\xff.md", "wb") as stream:
        stream.write(b"\xff\n")

    (card,) = load_readbacks(blueprint).values()
    assert card.unreadable and card.validate() == ("card path is not UTF-8",)
    card.declaration.encode("utf-8")


@pytest.mark.parametrize("recorded", ["Skel.other", "Skel." + "β" * 40])
def test_a_long_named_card_recording_another_declaration_is_invalid_for_the_one_its_path_names(
    tmp_path: Path, recorded: str
) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = replace(_declaration(), name="Skel." + "α" * 40)
    path = _file_card(blueprint, "Fine.", declaration=declaration)
    # Too long to spell in a filename, so the path names it only by a digest.
    assert path.name.startswith("declaration--")
    card = path.read_text(encoding="utf-8")
    path.write_text(card.replace(f'declaration: "{declaration.name}"', f'declaration: "{recorded}"'), encoding="utf-8")

    # As for a short name: invalid for the declaration the writer would find it under, not missing.
    found = readback_for(load_readbacks(blueprint), _ARTICLE_ID, declaration.name)
    assert found is not None and not found.valid
    assert f"frontmatter declaration {recorded!r} does not match the card path" in "; ".join(found.validate())


def test_a_card_whose_suffix_differs_only_in_case_is_reported_for_the_declaration_its_path_names(
    tmp_path: Path,
) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "Fine.")
    variant = path.rename(path.with_suffix(".MD"))

    # On a volume that ignores case the writer reaches this file, so it is not missing.
    found = readback_for(load_readbacks(blueprint), _ARTICLE_ID, "Skel.sup_unique")
    assert found is not None
    shown = variant.relative_to(path.parent.parent).as_posix()
    assert f"does not match the card path {shown!r}" in "; ".join(found.validate())


def test_card_parser_ignores_headings_and_fences_inside_the_packet_and_testimony(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = replace(_declaration(), signature=f'Skel.sup_unique : String := "\n{READBACK_HEADING}\n```text\n"')
    testimony = f"It returns\n{READBACK_HEADING}\n\n```text\nas a string; see ````."
    _file_card(blueprint, testimony, declaration=declaration)

    readback = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]
    assert readback.valid
    assert (readback.shown_text, readback.text) == (declaration.blind_text(), testimony)


def test_writer_requires_the_packet_its_hash_names_and_nonempty_fields(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()

    with pytest.raises(ValueError, match="packet does not match its packet hash"):
        _file_card(blueprint, "Fine.", packet_text=declaration.blind_text().replace("≤", "<"))
    with pytest.raises(ValueError, match="invalid statement hash"):
        _file_card(blueprint, "Fine.", statement_hash="approved")
    with pytest.raises(ValueError, match="nonempty testimony"):
        _file_card(blueprint, " \n")
    with pytest.raises(ValueError, match="nonempty model"):
        _file_card(blueprint, "Fine.", model=" ")
    for model in (
        "line\nbreak", "tab\tlabel", "control\x00label", "right\u05d0to-left", "blank\u2800label", "m\u0301\u0301\u0301",
        "\u0301m",
    ):
        with pytest.raises(ValueError, match="printable, single-line"):
            _file_card(blueprint, "Fine.", model=model)
    # Its closing fence would share the packet's last line, and Markdown would read the Lean block on.
    unterminated = declaration.blind_text().rstrip("\n")
    with pytest.raises(ValueError, match="packet does not end with a line break"):
        _file_card(blueprint, "Fine.", packet_text=unterminated, packet_hash=evidence_hash_of(unterminated))
    with pytest.raises(ValueError, match="invalid expected card hash"):
        _file_card(blueprint, "Fine.", expected_card_hash="sha256:short")
    assert not readback_path(blueprint, _ARTICLE_ID, declaration.name).exists()


def test_card_frontmatter_round_trips_quoted_names_and_models(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = replace(_declaration(), name="Review.«name: quoted»")
    model = 'reviewer: "strict" # literal'

    source = _file_card(blueprint, "Fine.", declaration=declaration, model=model).read_text("utf-8")
    card = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]

    assert f"declaration: {json.dumps(declaration.name, ensure_ascii=False)}" in source
    assert f"model: {json.dumps(model, ensure_ascii=False)}" in source
    assert (card.declaration, card.model, card.valid) == (declaration.name, model, True)
    Path(card.path).write_text(source.replace(json.dumps(model, ensure_ascii=False), "reviewer"), encoding="utf-8")
    reloaded = load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)]
    assert "frontmatter field 'model' must be a JSON double-quoted string" in reloaded.validation_errors


def test_writer_ignores_a_predictable_stage_link(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    path = readback_path(blueprint, _ARTICLE_ID, _declaration().name)
    path.parent.mkdir(parents=True)
    valuable = tmp_path / "valuable.md"
    valuable.write_text("keep\n", encoding="utf-8")
    predictable = path.with_name(path.name + ".tmp")
    predictable.symlink_to(valuable)

    _file_card(blueprint, "Fine.")
    assert valuable.read_text(encoding="utf-8") == "keep\n"
    assert predictable.is_symlink()


def test_the_card_path_never_goes_empty_while_a_card_is_replaced(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    expected = load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].file_hash
    create_only = _prepared(blueprint, "Create-only.")
    seen: dict[str, list] = {"loaded": [], "create-only": []}
    rename_file = os.rename

    # Just before and just after the new card is renamed over the old, a
    # reader looks, and a create-only writer tries to publish.
    def look() -> None:
        loaded = load_readbacks(blueprint).get((_ARTICLE_ID, "Skel.sup_unique"))
        seen["loaded"].append(None if loaded is None or not loaded.valid else loaded.text)
        try:
            publish_readback(create_only)
        except ValueError as exc:
            seen["create-only"].append(str(exc))
        else:
            seen["create-only"].append("published")

    def look_around_the_rename(*args, **kwargs) -> None:
        look()
        rename_file(*args, **kwargs)
        look()

    monkeypatch.setattr(os, "rename", look_around_the_rename)
    monkeypatch.setattr("autoform_cli.readback._LOCK_TIMEOUT", 0.05)

    assert _file_card(blueprint, "Replacement.", expected_card_hash=expected) == path
    assert seen["loaded"] == ["First.", "Replacement."]
    assert len(seen["create-only"]) == 2 and all("did not finish" in message for message in seen["create-only"])
    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "Replacement."
    assert _staged_names(path.parent) == []


def _crash_publication(tmp_path: Path, repo_root: Path, card: object, *, rename: str) -> None:
    """Publish ``card`` in a child process that dies just ``rename`` (before or after) it renames the card into place."""

    prepared = tmp_path / "prepared.pickle"
    prepared.write_bytes(pickle.dumps(card))
    script = (
        "import os, pickle, sys\n"
        "from autoform_cli import readback\n"
        "rename_file = os.rename\n"
        "def die(*args, **kwargs):\n"
        "    if sys.argv[2] == 'after':\n"
        "        rename_file(*args, **kwargs)\n"
        "    os._exit(17)\n"
        "os.rename = die\n"
        "with open(sys.argv[1], 'rb') as stream:\n"
        "    readback.publish_readback(pickle.load(stream))\n"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(prepared), rename],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(repo_root), os.environ.get("PYTHONPATH"))))},
        check=False,
    )
    assert crashed.returncode == 17


@pytest.mark.parametrize("rename", ["before", "after"])
def test_a_crash_mid_publication_leaves_nothing_that_blocks_a_retry(
    tmp_path: Path, repo_root: Path, rename: str
) -> None:
    blueprint = _blueprint(tmp_path)
    path = _file_card(blueprint, "First.")
    original = path.read_bytes()
    card = _prepared(blueprint, "Replacement.", load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].file_hash)

    _crash_publication(tmp_path, repo_root, card, rename=rename)

    if rename == "before":
        # The old card stands; the staging file is never read as a card, and the lock died with the writer.
        assert path.read_bytes() == original
        (leftover,) = _staged_names(path.parent)
        assert not leftover.endswith(".md") and path.with_name(leftover).read_bytes() == card.content.encode("utf-8")
        assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "First."
        assert publish_readback(card) == path
    else:
        assert _staged_names(path.parent) == []
    assert load_readbacks(blueprint)[(_ARTICLE_ID, "Skel.sup_unique")].text == "Replacement."


def test_case_distinct_declarations_have_distinct_card_paths(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    base = _declaration()
    upper = replace(base, name="Foo.x")
    lower = replace(base, name="foo.x")

    paths = [
        _file_card(blueprint, f"Testimony for {declaration.name}.", declaration=declaration)
        for declaration in (upper, lower)
    ]

    assert paths[0] != paths[1]
    assert paths[0].name.casefold() != paths[1].name.casefold()
    assert set(load_readbacks(blueprint)) == {(_ARTICLE_ID, "Foo.x"), (_ARTICLE_ID, "foo.x")}


def test_card_identity_survives_a_roadmap_move(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    declaration = _declaration()
    card = _file_card(blueprint, "Stable testimony.")
    article = blueprint / "roadmap" / "basics" / "sup-unique.md"
    moved = blueprint / "roadmap" / "renamed" / "result.md"
    moved.parent.mkdir()
    article.rename(moved)

    assert card == readback_path(blueprint, _ARTICLE_ID, declaration.name)
    assert load_readbacks(blueprint)[(_ARTICLE_ID, declaration.name)].text == "Stable testimony."
