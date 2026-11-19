#!/usr/bin/env python3
"""Compare the links the site reads, `site_links`, with today's reader, `markdown_links`, over a vault.

    python scripts/reader_diff.py ROOT [--files LIST] [--sample N] [--seed S] [--show K]

ROOT is a repository or its blueprint folder; every Markdown page under ROOT/blueprint (or ROOT) is read, or the
pages LIST names, one path relative to ROOT per line, and with --sample, N of them shuffled by --seed. Each link
one reader reads and the other does not, or reads on another line or in another section, is sorted into a named
class and printed by page, line and section, K per class. It also counts links the marks did not place, addresses
not verbatim in the source (no span), pages whose marks changed what the site links, and each reader's pages/s.
Run it from a checkout with its autoform_cli importable (PYTHONPATH=<checkout>).
"""

from __future__ import annotations

import argparse
import html
import random
import re
import sys
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from markdown.util import AMP_SUBSTITUTE

from autoform_cli.markdown import (
    HEADING,
    PAGE_HEADER,
    PAGE_SUFFIXES,
    SiteLink,
    SiteReadingError,
    first_unclosed_fence,
    markdown_links,
    mask_code_and_comments,
    published_page,
    rendered_visible_text,
    _site_tree,
    site_links,
)

#: A line in a quote or a list item, after its indent.
_CONTAINER = re.compile(r"^[ \t]*(?:>|(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$))")
#: A setext heading's underline.
_SETEXT = re.compile(r"^ {0,3}(?:-+|=+)[ \t]*$")
#: A fence opened in a quote or a list item.
_CONTAINER_FENCE = re.compile(r"^[ \t]*(?:(?:>[ \t]?|(?:[-*+]|\d{1,9}[.)])[ \t]+)[ \t]*)+(`{3,}|~{3,})")


def _pages(root: Path, listing: Path | None, sample: int | None, seed: int) -> list[str]:
    if listing is not None:
        names = listing.read_text(encoding="utf-8").split()
    else:
        base = root / "blueprint" if (root / "blueprint").is_dir() else root
        names = sorted(
            path.relative_to(root).as_posix()
            for path in base.rglob("*")
            if path.suffix in PAGE_SUFFIXES and path.is_file()
        )
    if sample is not None:
        random.Random(seed).shuffle(names)
        names = names[:sample]
    return names


def _today_section(masked: list[str], line: int) -> str | None:
    """The H2 section the graph loader puts *line* in: the last ``## `` heading at or above it, outside code."""

    for text in reversed(masked[:line]):
        if (heading := HEADING.match(text)) and len(heading.group(1)) == 2:
            return heading.group(2).strip()
    return None


@lru_cache(maxsize=None)
def _visible(heading: str | None) -> str:
    """A heading's text as a reader sees it, to compare sections by."""

    return " ".join(rendered_visible_text(heading or "").split()).casefold()


def _in_container_fence(lines: list[str], line: int) -> bool:
    """Whether a fence opened in a quote or list item above *line*, in its block, is still open there."""

    open_fence = False
    for text in lines[: line - 1]:
        if not text.strip():
            open_fence = False
        elif _CONTAINER_FENCE.match(text):
            open_fence = not open_fence
    return open_fence


def _site_class(link: SiteLink, lines: list[str], fence: int | None) -> str:
    if link.kind == "autolink":
        return "autolink (D1): the site links <https://...>, today's reader does not"
    if link.kind is None or link.line is None:
        return "unplaced: no mark in the address"
    if fence is not None and link.line > fence + 1:
        return "after an unclosed fence: the site reads it as text"
    if link.kind == "reference" and any(_CONTAINER.match(lines[n]) for n in (link.line - 1, link.line - 2) if n >= 0):
        return "definition in a quote or list item: the site reads it"
    return "other (site only)"


def _today_class(line: int, lines: list[str]) -> str:
    if _in_container_fence(lines, line):
        return "fence opened in a quote or list item: the site shows code"
    return "other (today only)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--files", type=Path)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=10)
    arguments = parser.parse_args()

    names = _pages(arguments.root, arguments.files, arguments.sample, arguments.seed)
    classes: dict[str, list[str]] = defaultdict(list)
    counts = Counter()
    took = Counter()
    # The first call of each reader imports and builds what it needs: keep that out of the timing.
    warm = "# T\n\n[a](b.md)\n\n```python\nx = 1\n```\n"
    site_links(warm), markdown_links(warm)
    for name in names:
        text = (arguments.root / name).read_text(encoding="utf-8")
        start = time.perf_counter()
        try:
            site = site_links(text)
        except SiteReadingError as error:
            classes["unreadable: the converter fails"].append(f"{name}: {error}")
            continue
        took["site_links"] += time.perf_counter() - start
        start = time.perf_counter()
        today = markdown_links(text)
        took["markdown_links"] += time.perf_counter() - start

        counts["pages"] += 1
        counts["site links"] += len(site)
        counts["today links"] += len(today)
        counts["unplaced"] += sum(link.kind is None for link in site)
        counts["no span"] += sum(link.kind is not None and link.span is None for link in site)
        tree = _site_tree(published_page(text).removeprefix(PAGE_HEADER))
        plain = Counter(
            html.unescape(href.replace(AMP_SUBSTITUTE, "&"))
            for anchor in tree.iter("a")
            if (href := anchor.get("href")) is not None
        )
        counts["pages whose marks change a link"] += plain != Counter(link.href for link in site)

        lines = text.splitlines()
        masked = mask_code_and_comments(lines)
        fence = first_unclosed_fence(lines)
        spare_site = list(site)
        spare_today = [(line, target) for line, target, _ in today]
        # Pair the same address on the same line, then on another line.
        pairs: list[tuple[SiteLink, int]] = []
        for same_line in (True, False):
            for link in list(spare_site):
                match = next(
                    (item for item in spare_today if item[1] == link.href and (not same_line or item[0] == link.line)),
                    None,
                )
                if match is None:
                    continue
                spare_site.remove(link)
                spare_today.remove(match)
                pairs.append((link, match[0]))
                if not same_line:
                    if link.kind == "reference":
                        where = "a reference link placed at its definition"
                    elif link.line is not None and link.line > match[0]:
                        where = "a link whose text wraps, placed at its destination"
                    else:
                        where = "other"
                    classes[f"line: {where}"].append(f"{name}:{match[0]}->{link.line} {link.href}")
        for link, line in pairs:
            ours = _today_section(masked, line)
            if _visible(ours) != " ".join((link.section or "").split()).casefold():
                setext = line < len(lines) and _SETEXT.match(lines[line])
                why = "a line of - or = under text makes it an H2" if setext else "other"
                classes[f"section (D3): the site's headings, not the loader's: {why}"].append(
                    f"{name}:{line} {link.href} site={link.section!r} today={ours!r}"
                )

        # A quote in the destination: the site links the address up to the quote, today's reader past it (#828).
        def quoted(longer: str, address: str) -> bool:
            return longer != address and longer.startswith(address) and longer[len(address)] in "'\""

        quote = "quote in destination (#828)"
        for line, target in list(spare_today):
            # Or a later link the site reads in such a link's title.
            if any(quoted(target, link.href) or target.rstrip("'\"") in (link.title or "") for link in site):
                spare_today.remove((line, target))
                classes[quote].append(f"{name}:{line} today={target}")
        for link in list(spare_site):
            if any(quoted(target, link.href) for _, target, _ in today):
                spare_site.remove(link)
                classes[quote].append(f"{name}:{link.line} site={link.href}")
        for link in spare_site:
            classes[_site_class(link, lines, fence)].append(f"{name}:{link.line} [{link.section}] {link.href}")
        for line, target in spare_today:
            classes[_today_class(line, lines)].append(f"{name}:{line} [{_today_section(masked, line)}] {target}")

    print(
        f"pages={counts['pages']} site_links={counts['site links']} markdown_links={counts['today links']} "
        f"unplaced={counts['unplaced']} no span={counts['no span']} pages whose marks change a link={counts['pages whose marks change a link']}"
    )
    for reader in ("site_links", "markdown_links"):
        if took[reader]:
            print(
                f"{reader}: {counts['pages'] / took[reader]:.0f} pages/s ({1000 * took[reader] / counts['pages']:.2f} ms/page)"
            )
    for name, found in sorted(classes.items(), key=lambda item: -len(item[1])):
        pages = len({entry.split(":")[0] for entry in found})
        print(f"{len(found):6d} links on {pages} pages  {name}")
        for entry in found[: arguments.show]:
            print(f"         {entry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
