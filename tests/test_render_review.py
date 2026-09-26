from dataclasses import replace
import json
from pathlib import Path

import markdown as markdown_renderer
import pytest

from autoform_cli.readback import (
    TESTIMONY_MAX_BRACKETS,
    Readback,
    _testimony_errors,
    load_readbacks,
    write_readback,
)
from autoform_cli.markdown import SITE_EXTENSION_CONFIGS, SITE_EXTENSIONS
from autoform_cli.render import _mermaid_script, _readback_block, _skeleton_block
from autoform_cli.skeleton import DeclarationSkeleton


def _declaration() -> DeclarationSkeleton:
    return DeclarationSkeleton(
        name="Review.result",
        kind="theorem",
        module="Review",
        path="Review.lean",
        start_line=1,
        end_line=2,
        signature="Review.result : True",
        semantic='{"const":"True"}',
        lean_version="4.32.2",
        depends=(),
        trusted=(),
        assumed=(),
        assumed_semantics=(),
        boundary_modules=(),
        axioms=(),
        axiom_semantics=(),
        statement="/-- reviewer hint that is absent from the packet -/\ntheorem result : True",
    )


def test_review_displays_the_exact_hashed_blind_packet() -> None:
    declaration = _declaration()

    rendered = "\n".join(_skeleton_block(declaration))

    assert "reviewer hint" not in rendered
    assert "Review.result : True" in rendered
    assert declaration.evidence_hash in rendered


def test_readback_markdown_cannot_inject_raw_html() -> None:
    declaration = _declaration()
    readback = Readback(
        article_id="af_0123456789abcdef01234567",
        declaration=declaration.name,
        skeleton_hash=declaration.hash,
        packet_hash=declaration.evidence_hash,
        model="reviewer",
        text="**Result:** $P$. <script>alert(1)</script>",
        path=Path("card.md"),
        shown_hash=declaration.evidence_hash,
    )

    rendered = "\n".join(_readback_block(declaration, readback))

    assert "**Result:** $P$." in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered

    invalid = "\n".join(
        _readback_block(declaration, replace(readback, validation_errors=("raw HTML is not allowed",)))
    )
    assert "bp-readback-invalid" in invalid
    assert "invalid · raw HTML is not allowed" in invalid


@pytest.mark.parametrize(
    "testimony",
    [
        "<script>alert(1)</script>",
        "[click](javascript:alert(1))",
        "![remote](https://example.test/pixel.png)",
        "<https://example.test/track>",
        "*claim*{onclick=alert(1)}",
        "[remote]: https://example.test/track",
        "FALSE\n{: hidden=true }",
        "`FALSE`{aria-hidden=true}",
        "FALSE\n{: .bp-visually-hidden}",
        "FALSE\n{: dir=rtl}",
        "FALSE\n{: contenteditable=true}",
        "graph TD\nA-->B\nclick A javascript:alert(1)\n{: .mermaid}",
        '```mermaid\ngraph TD\nclick A "javascript:alert(1)"\n```',
        "``` {.mermaid}\ngraph TD\nA-->B\n```",
        r"$\require{html}\href{javascript:alert(1)}{x}$",
        r"$\style{visibility:hidden}{FALSE}$",
        r"$\class{mermaid}{graph TD}$",
        r"$\cssId{hidden}{FALSE}$",
        "$\\re% hidden continuation\nquire{html}$",
        r"$\csname href\endcsname{javascript:alert(1)}{x}$",
    ],
)
def test_writer_rejects_active_testimony(testimony: str, tmp_path: Path) -> None:
    declaration = _declaration()

    with pytest.raises(ValueError, match="unsafe read-back testimony"):
        write_readback(
            tmp_path,
            article_id="af_0123456789abcdef01234567",
            declaration=declaration,
            model="reviewer",
            text=testimony,
            packet_text=declaration.blind_text(),
        )


def test_parser_marks_hand_authored_active_testimony_invalid(tmp_path: Path) -> None:
    declaration = _declaration()
    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text="A plain mathematical statement.",
        packet_text=declaration.blind_text(),
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "A plain mathematical statement.",
            "[run](javascript:alert(1)) ![remote](https://example.test/pixel.png) "
            "<https://example.test/track>",
        ),
        encoding="utf-8",
    )

    card = load_readbacks(tmp_path)[("af_0123456789abcdef01234567", declaration.name)]

    assert not card.valid
    assert "Markdown links, images, and autolinks are not allowed" in card.validation_errors
    rendered = "\n".join(_readback_block(declaration, card))
    published = markdown_renderer.markdown(
        rendered,
        extensions=["attr_list", "md_in_html", "pymdownx.arithmatex", "pymdownx.superfences"],
    )
    assert "bp-readback-invalid" in published
    assert "href=" not in published
    assert "<img" not in published
    assert "<pre><code>" in published


def test_mermaid_attribute_form_matches_the_published_runtime(tmp_path: Path) -> None:
    testimony = "graph TD\nA-->B\nclick A javascript:alert(1)\n{: .mermaid}"
    published = markdown_renderer.markdown(
        testimony,
        extensions=list(SITE_EXTENSIONS),
        extension_configs=SITE_EXTENSION_CONFIGS,
    )
    assert 'class="mermaid"' in published
    assert 'querySelectorAll(".mermaid")' in _mermaid_script()

    declaration = _declaration()
    with pytest.raises(ValueError, match="active Mermaid"):
        write_readback(
            tmp_path,
            article_id="af_0123456789abcdef01234567",
            declaration=declaration,
            model="reviewer",
            text=testimony,
            packet_text=declaration.blind_text(),
        )


def test_shipped_mathjax_configuration_filters_active_math(tmp_path: Path) -> None:
    configuration = (
        Path(__file__).parents[1]
        / "autoform_cli"
        / "templates"
        / "blueprint"
        / "javascripts"
        / "mathjax.js"
    ).read_text(encoding="utf-8")

    assert 'load: ["ui/safe"]' in configuration
    assert 'URLs: "none"' in configuration
    assert 'classes: "none"' in configuration
    assert 'cssIDs: "none"' in configuration
    assert 'styles: "none"' in configuration
    assert 'packages: {"[-]": ["require"]}' in configuration


def test_writer_keeps_inert_markdown_and_mathematics(tmp_path: Path) -> None:
    declaration = _declaration()

    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text="**Precisely:** for $x < y$, the claim holds.\n\n- No extra hypothesis.",
        packet_text=declaration.blind_text(),
    )

    assert load_readbacks(tmp_path)[
        ("af_0123456789abcdef01234567", declaration.name)
    ].valid
    assert path.is_file()


def _file(tmp_path: Path, testimony: str) -> Path:
    declaration = _declaration()
    return write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text=testimony,
        packet_text=declaration.blind_text(),
    )


@pytest.mark.parametrize(
    ("testimony", "reason"),
    [
        ("​", "U+200B ZERO WIDTH SPACE"),
        ("The claim holds⁠ for all x.", "U+2060 WORD JOINER"),
        ("The bound is ‮1 > x‬ for every x.", "U+202E RIGHT-TO-LEFT OVERRIDE"),
        ("The claim holds&#8203; for all x.", "U+200B ZERO WIDTH SPACE"),
        (r"For all $x$, $P(x) \phantom{\land Q(x)}$ holds.", r"\phantom"),
        (r"$P \rlap{\,\land Q}$", r"\rlap"),
        (r"$P \kern-2em \land Q$", r"\kern"),
        (r"$\toggle{P}{P \land Q}\endtoggle$", r"\toggle"),
        (r"$\bbox[black]{P}$", r"\bbox"),
        (r"$\unicode{x200B}$", r"\unicode"),
        (r"$\newcommand{\h}[1]{} P \h{\land Q}$", r"\newcommand"),
        (r"$\def\h#1{} P \h{\land Q}$", r"\def"),
        ("$P(x) % \\land Q(x)\n$", "TeX comments are not allowed"),
        (r"$P\!\!\!\!\!\!Q$", "repeated negative TeX spacing"),
        ("$ $", "renders no visible text"),
    ],
)
def test_writer_rejects_testimony_that_hides_what_it_says(testimony: str, reason: str, tmp_path: Path) -> None:
    """A reader must be shown everything the card says, and only that."""

    with pytest.raises(ValueError, match="unsafe read-back testimony") as refused:
        _file(tmp_path, testimony)

    assert reason in str(refused.value)


@pytest.mark.parametrize(
    "testimony",
    [
        r"For every $x \in [0, 1]$ and the set $\{x\}$, the claim holds.",
        r"Integrate with a thin negative space, $\int\! f$, once.",
        r"At least $50\%$ of cases, or 50\% in prose.",
        "Code such as `a % b` is shown as written.",
        r"Height is kept with $\mathstrut x$ and a smash-free formula.",
    ],
)
def test_writer_accepts_ordinary_mathematical_testimony(testimony: str, tmp_path: Path) -> None:
    _file(tmp_path, testimony)

    assert all(card.valid for card in load_readbacks(tmp_path).values())


@pytest.mark.parametrize(
    ("testimony", "limit"),
    [
        ("[" * 8000, "opening brackets"),
        ("`" * 4000, "run of 4000 backticks"),
        ("a `b` " * 600, "backticks"),
        ("a $b$ " * 2000, "math delimiters"),
        ("".join("  " * depth + "- a\n" for depth in range(512)), "columns deep"),
        ("word " * 7000, "-byte limit"),
        ("x\n" * 600, "-line limit"),
    ],
)
def test_testimony_over_a_limit_is_refused_before_it_is_parsed(
    testimony: str, limit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parsing is superlinear in spans and openers, cubic in a backtick run,
    and recursive in nesting: 8,000 brackets took ten seconds, 4,000 backticks
    over two minutes, and a list 512 levels deep overflowed the stack."""

    def unbounded(*args: object, **kwargs: object) -> None:
        raise AssertionError("the Markdown renderer ran on testimony over a limit")

    monkeypatch.setattr("autoform_cli.readback.markdown_renderer.Markdown", unbounded)

    assert any(limit in error for error in _testimony_errors(testimony))


def test_a_card_over_a_limit_is_invalid_without_being_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every card in a pull request is read before its validity is known."""

    path = _file(tmp_path, "A plain mathematical statement.")
    path.write_text(
        path.read_text(encoding="utf-8").replace("A plain mathematical statement.", "[" * 8000),
        encoding="utf-8",
    )

    def unbounded(*args: object, **kwargs: object) -> None:
        raise AssertionError("the Markdown renderer ran on a card over a limit")

    monkeypatch.setattr("autoform_cli.readback.markdown_renderer.Markdown", unbounded)
    card = load_readbacks(tmp_path)[("af_0123456789abcdef01234567", _declaration().name)]

    assert not card.valid
    assert f"testimony has 8000 opening brackets, over the limit of {TESTIMONY_MAX_BRACKETS}" in card.validation_errors


def test_card_frontmatter_round_trips_quoted_names_and_models(tmp_path: Path) -> None:
    declaration = replace(_declaration(), name="Review.«name: quoted»")
    model = 'reviewer: "strict" # literal'

    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model=model,
        text="A plain mathematical statement.",
        packet_text=declaration.blind_text(),
    )
    source = path.read_text(encoding="utf-8")
    card = load_readbacks(tmp_path)[
        ("af_0123456789abcdef01234567", declaration.name)
    ]

    assert f"declaration: {json.dumps(declaration.name, ensure_ascii=False)}" in source
    assert f"model: {json.dumps(model, ensure_ascii=False)}" in source
    assert card.declaration == declaration.name
    assert card.model == model
    assert card.valid


def test_parser_rejects_bare_identity_frontmatter(tmp_path: Path) -> None:
    declaration = _declaration()
    path = write_readback(
        tmp_path,
        article_id="af_0123456789abcdef01234567",
        declaration=declaration,
        model="reviewer",
        text="A plain mathematical statement.",
        packet_text=declaration.blind_text(),
    )
    source = path.read_text(encoding="utf-8")
    path.write_text(source.replace('model: "reviewer"', "model: reviewer"), encoding="utf-8")

    card = load_readbacks(tmp_path)[
        ("af_0123456789abcdef01234567", declaration.name)
    ]

    assert not card.valid
    assert "frontmatter field 'model' must be a JSON double-quoted string" in card.validation_errors


@pytest.mark.parametrize("model", ["line\nbreak", "tab\tlabel", "control\x00label"])
def test_writer_rejects_control_characters_in_model_labels(model: str, tmp_path: Path) -> None:
    declaration = _declaration()

    with pytest.raises(ValueError, match="printable, single-line"):
        write_readback(
            tmp_path,
            article_id="af_0123456789abcdef01234567",
            declaration=declaration,
            model=model,
            text="A plain mathematical statement.",
            packet_text=declaration.blind_text(),
        )
