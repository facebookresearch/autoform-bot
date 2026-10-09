"""Which of a project's locked packages publish an index that search can use."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .index import INDEX_FILE
from .locate import LibraryError, checkout_paths, read_workspace
from .verify import load_library

LIBRARY_LIST_SCHEMA = "autoform-library-list/v1"


@dataclass(frozen=True, slots=True)
class ListedPackage:
    name: str
    type: str
    revision: str | None
    #: Whether the checkout holds an index file at all.
    index: bool
    #: Whether ``autoform search --library`` would accept it. The same checks decide both.
    usable: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class LibraryListing:
    packages: tuple[ListedPackage, ...]

    def as_dict(self) -> dict[str, object]:
        return {"schema": LIBRARY_LIST_SCHEMA, "packages": [asdict(package) for package in self.packages]}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def list_libraries(root: str | Path) -> LibraryListing:
    """Report every package ``root`` locks, running for each the checks search runs."""

    workspace = read_workspace(root)
    listed: list[ListedPackage] = []
    for package in workspace.packages:
        reason = None
        try:
            load_library(workspace, package.name)
        except LibraryError as error:
            reason = error.reason
        listed.append(
            ListedPackage(
                name=package.name,
                type=package.type,
                revision=package.rev,
                index=_has_index(workspace, package),
                usable=reason is None,
                reason=reason,
            )
        )
    return LibraryListing(tuple(listed))


def _has_index(workspace, package) -> bool:
    if package.type != "git":
        return False
    try:
        _checkout, root = checkout_paths(workspace, package)
        return stat.S_ISREG(os.lstat(root / INDEX_FILE).st_mode)
    except (LibraryError, OSError):
        return False


__all__ = ["LIBRARY_LIST_SCHEMA", "LibraryListing", "ListedPackage", "list_libraries"]
