#!/usr/bin/env python3
"""Check the changelog fragments, or move them into CHANGELOG.md at a release.

Each change worth knowing about adds its own `changelog.d/SECTION-SLUG.md`, so
pull requests never edit CHANGELOG.md and never conflict there. SECTION is one
of `SECTIONS`; SLUG is lowercase words joined by `-`. The file holds one or more
entries, each `- **Title.** What changed (#PR).`, continued on lines indented by
two spaces. Links resolve from the repository root, as in CHANGELOG.md.

    python3 scripts/changelog.py check    # tests/test_changelog.py runs it in CI
    python3 scripts/changelog.py release  # after the version bump

`release` puts the fragments under a heading naming pyproject.toml's version, in
section order and file-name order within a section, and deletes them; review
the result and write its Highlights before committing.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = {
    "upgrading": "Upgrading from {previous}",
    "added": "Added",
    "changed": "Changed",
    "deprecated": "Deprecated",
    "removed": "Removed",
    "fixed": "Fixed",
}
_NAME = re.compile(r"([a-z]+)-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_CITATION = re.compile(r"(?<![\w/])#\d+\b")


def fragments(root: Path) -> list[Path]:
    return sorted((root / "changelog.d").glob("*.md"))


def problems(root: Path) -> list[str]:
    """One line per fragment that breaks the format, and for an Unreleased section in CHANGELOG.md."""
    found = []
    if re.search(r"^## Unreleased\b", (root / "CHANGELOG.md").read_text(encoding="utf-8"), re.MULTILINE):
        found.append("CHANGELOG.md: has an Unreleased section; put each entry in its own changelog.d/ file")
    for path in fragments(root):
        where = f"changelog.d/{path.name}"
        name = _NAME.fullmatch(path.name)
        if not name or name[1] not in SECTIONS:
            found.append(f"{where}: name it SECTION-SLUG.md, with SECTION one of {', '.join(SECTIONS)}")
        text = path.read_text(encoding="utf-8").strip("\n")
        if not text.strip():
            found.append(f"{where}: empty")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip() and not line.startswith(("- **", "  ")):
                found.append(
                    f"{where}:{number}: start each entry with `- **Title.**` and indent the lines that continue "
                    "it by two spaces; no headings, the file name gives the section"
                )
        for entry in re.split(r"^(?=- )", text, flags=re.MULTILINE):
            if entry.startswith("- ") and not _CITATION.search(entry):
                found.append(f"{where}: cite the pull request or issue in each entry, as (#123): {entry[:50]!r}")
    return found


def release(root: Path, date: str) -> int:
    """Move every fragment under a new heading for pyproject.toml's version, and return how many moved."""
    if found := problems(root):
        raise SystemExit("\n".join(found))
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    changelog = root / "CHANGELOG.md"
    preamble, _, rest = changelog.read_text(encoding="utf-8").partition("\n## ")
    previous = rest.split(maxsplit=1)[0] if rest else ""
    if previous == version:
        raise SystemExit(f"CHANGELOG.md already has {version}: bump the version in pyproject.toml first")
    paths = fragments(root)
    if not paths:
        raise SystemExit("changelog.d/ has no fragments to release")
    entries: dict[str, list[str]] = {section: [] for section in SECTIONS}
    for path in paths:
        entries[_NAME.fullmatch(path.name)[1]].append(path.read_text(encoding="utf-8").strip("\n"))
    parts = [f"## {version} ({date})"]
    for section, title in SECTIONS.items():
        if entries[section]:
            parts += [f"### {title.format(previous=previous)}", "\n".join(entries[section])]
    released = preamble.rstrip("\n") + "\n\n" + "\n\n".join(parts) + "\n"
    changelog.write_text(released + ("\n## " + rest if rest else ""), encoding="utf-8")
    for path in paths:
        path.unlink()
    return len(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "release"))
    parser.add_argument("--date", default=datetime.date.today().isoformat(), help="the release date (default today)")
    arguments = parser.parse_args(argv)
    if arguments.command == "release":
        print(f"CHANGELOG.md: moved {release(ROOT, arguments.date)} fragments from changelog.d/")
        return 0
    found = problems(ROOT)
    print("\n".join(found), file=sys.stderr, end="\n" if found else "")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
