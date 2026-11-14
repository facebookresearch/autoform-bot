"""Write fixture articles as a migrated project keeps them, with their facts in ``blueprint/links/``.

A fixture spells an article as frontmatter once held it, which reads best. :func:`write_annotated` writes it the way
``autoform migrate links blueprint --write`` leaves one: the page loses its facts, and they go to its annotation,
encoded by Autoform's own writer. Unlike migrate, it proposes no ``article_id`` for a page without one, so a test
reads back the facts it wrote and no others. :func:`annotated_text` and :func:`edit_annotated` read and edit such a
page as if its facts were still in its frontmatter. Tests about reading frontmatter write their pages directly, or
through :func:`write_frontmatter`.
"""

from __future__ import annotations

from pathlib import Path

from autoform_cli import annotation_writer as writer

#: Each page :func:`write_annotated` annotated, by resolved path: the fact lines it moved out of the page, and the
#: bytes it left in the page's two annotation files, None for no file.
_WRITTEN: dict[Path, tuple[list[str], dict[Path, bytes | None]]] = {}


def _blueprint(page: Path) -> Path | None:
    """The blueprint *page* is an article of: the folder holding the ``roadmap`` folder it is below, if any."""
    return next((parent.parent for parent in page.parents if parent.name == "roadmap"), None)


def _annotations(paths: tuple[Path, ...]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _fact_lines(text: str) -> list[str]:
    """The lines of *text*'s frontmatter that :func:`autoform_cli.annotation_writer.strip_facts` removes."""
    lines = text.splitlines(keepends=True)
    end = writer._block_end(lines) or 0
    return [line for line in lines[1:end] if writer._is_fact(line)]


def write_annotated(page: Path, text: str) -> None:
    """Write *text*, a page with any facts in its frontmatter, as a migrated project keeps it. A page with no facts, or
    one below no roadmap folder, is written as it stands, and an article loses any annotation an earlier write gave
    it."""
    blueprint = _blueprint(page)
    facts = writer.frontmatter_facts(page.stem, text) if blueprint is not None else {}
    # The loader keeps no fact from a line it rejects, which stripping would remove with the rest.
    rejected = sorted(set(writer.fact_keys(text.encode("utf-8"))) - facts.keys()) if facts else []
    assert not rejected, f"{page}: the loader rejects its {', '.join(rejected)} line; write it with write_frontmatter"
    page.parent.mkdir(parents=True, exist_ok=True)
    if not facts and page.resolve() not in _WRITTEN:
        page.write_text(text, encoding="utf-8")
        return
    assert blueprint is not None
    paths = writer.annotation_paths(blueprint, page)
    changes = writer.encode(blueprint, page, facts) if facts else {paths.own: None, paths.mathlib: None}
    page.write_text(writer.strip_facts(text.encode("utf-8")).decode("utf-8") if facts else text, encoding="utf-8")
    for path, data in changes.items():
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    _WRITTEN[page.resolve()] = (_fact_lines(text) if facts else [], _annotations((paths.own, paths.mathlib)))


def annotated_text(page: Path) -> str:
    """*page* with the facts :func:`write_annotated` moved out of it back in its frontmatter, while its annotations
    still hold just those facts; a page it never annotated, as it stands."""
    current = page.read_text(encoding="utf-8")
    if page.resolve() not in _WRITTEN:
        return current
    facts, annotations = _WRITTEN[page.resolve()]
    assert _annotations(tuple(annotations)) == annotations, (
        f"{page}'s annotations changed since write_annotated wrote them"
    )
    if not facts:
        return current
    lines = current.splitlines(keepends=True)
    if writer._block_end(lines) is not None:  # the page kept its frontmatter for its settings
        return lines[0] + "".join(facts) + "".join(lines[1:])
    return "---\n" + "".join(facts) + "---\n\n" + current


def edit_annotated(page: Path, old: str, new: str) -> None:
    """Replace *old* with *new* in *page* read by :func:`annotated_text`, and write it through
    :func:`write_annotated`."""
    text = annotated_text(page)
    assert old in text
    write_annotated(page, text.replace(old, new))


def _forget(page: Path) -> None:
    """Remove the annotation :func:`write_annotated` gave *page*, if any."""
    if page.resolve() in _WRITTEN:
        blueprint = _blueprint(page)
        assert blueprint is not None
        paths = writer.annotation_paths(blueprint, page)
        for path in (paths.own, paths.mathlib):
            path.unlink(missing_ok=True)
        del _WRITTEN[page.resolve()]


def move_annotated(page: Path, target: Path) -> None:
    """Rename *page* to *target* as ``autoform move`` would, its annotation following it."""
    text = annotated_text(page)
    _forget(page)
    page.unlink()
    write_annotated(target, text)


def write_frontmatter(page: Path, text: str) -> None:
    """Write *text* as it stands, facts in frontmatter, and drop any annotation :func:`write_annotated` gave *page*:
    for a test about reading frontmatter in a project that otherwise keeps its facts in annotations."""
    _forget(page)
    page.write_text(text, encoding="utf-8")
