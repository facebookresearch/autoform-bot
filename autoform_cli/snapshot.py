"""The bytes a blueprint was read from.

The graph keeps the bytes of every article it parsed in a ``BlueprintSnapshot``,
so published pages and audits read the text the graph was built from, never a
later state of the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BlueprintSnapshot:
    """Captured file bytes, keyed by canonical absolute path."""

    files: dict[Path, bytes] = field(default_factory=dict)

    def text(self, path: Path) -> str:
        """Return a captured file as UTF-8 text."""

        return self.files[path].decode("utf-8")


__all__ = ["BlueprintSnapshot"]
