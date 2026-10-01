"""Optional post-verdict feedback form ("debrief") for prover runs.

After the driver has fixed a run's verdict, it can resume the backend's session
once, read-only, and ask the prover what infrastructure would have made the
proof cheaper. The answer never affects the verdict. Off by default.

Configuration (environment):

``AUTOFORM_DEBRIEF``
    ``1`` / ``true`` / ``yes`` enables the debrief. Anything else disables it.
``AUTOFORM_DEBRIEF_DIR``
    Where records and reports are written. Defaults to
    ``<project>/.autoform/debriefs``; a relative path is resolved against the
    Lean project directory.
``AUTOFORM_DEBRIEF_QUESTION_FILE``
    A file whose text replaces :data:`DEFAULT_QUESTION`. ``{node_id}`` and
    ``{outcome}`` are substituted literally; nothing else is interpreted.
``AUTOFORM_DEBRIEF_BUDGET``
    Wall-clock seconds for the debrief turn (default 180).

Output, under the debrief directory:

``debriefs.jsonl``
    One append-only record per debriefed run.
``reports/<node>--<timestamp>--<run id>.md``
    The same record rendered as Markdown.

``python -m servers.prover.debrief <dir>`` re-renders every report and writes a
``README.md`` index.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

DEBRIEF_ENV = "AUTOFORM_DEBRIEF"
DEBRIEF_DIR_ENV = "AUTOFORM_DEBRIEF_DIR"
DEBRIEF_QUESTION_FILE_ENV = "AUTOFORM_DEBRIEF_QUESTION_FILE"
DEBRIEF_BUDGET_ENV = "AUTOFORM_DEBRIEF_BUDGET"
DEFAULT_BUDGET_SECONDS = 180.0
RECORDS_FILENAME = "debriefs.jsonl"
REPORTS_DIRNAME = "reports"

DEFAULT_QUESTION = """This round is finished and its verdict is already recorded — **nothing you say
now changes it**, and there is nothing left to fix. This is a separate
question about tooling, not about the proof.

You have just spent this session proving `{node_id}`. Outcome: `{outcome}`.

We maintain project proof infrastructure — helper lemmas, simp and Aesop rules,
wrapper tactics, imports, and search/tooling support — and want to know what to
add or improve. You are the only witness to what this proof actually cost: the
finished file records what you wrote, not what you searched for and could not
find, nor what you tried and abandoned.

Answer with a single JSON object and no other text. Do not wrap it in a Markdown code fence.

{
  "friction": [
    {
      "file": "path/to/File.lean",
      "line": 111,
      "lines": 7,
      "what": "one sentence: what this block establishes",
      "why_hard": "lemma-search | lemma-name-guessing | repeated-boilerplate | side-goal-plumbing | rewrite-plumbing | other",
      "goal": "example ... := by sorry   (best effort, need not compile)"
    }
  ],
  "searched_for": [
    {"wanted": "what missing fact or infrastructure you needed",
     "searched": "the grep/rg terms or Mathlib names you tried",
     "why_hard": "missing-infrastructure | repeated-failed-search | misleading-name | inaccessible-import | complex-workaround | other",
     "found": false, "resolution": "what you eventually found or had to do instead"}
  ],
  "infrastructure_proposals": [
    {
      "kind": "tactic-idea | helper-lemma | simp-rule | aesop-rule | wrapper-tactic | import-export | search-tooling | other",
      "name": "snake_case_slug or null",
      "what": "one sentence: the reusable infrastructure or rough automation idea that might help",
      "evidence": "friction 1 | searched_for 2 | other"
    }
  ],
  "nothing_to_report": false
}

Rules for `friction`:
- Only blocks of **5 or more lines** where you knew what to do and it still
  took that long. Skip short steps where you named the right lemma first time.
- Skip blocks that were hard *mathematically*. Infrastructure can remove routine
  proof work; it cannot replace the argument.
- Friction is worth recording even when you do not know the right improvement.

Rules for `infrastructure_proposals`:
- A proposal may be any reusable infrastructure improvement. It need not
  correspond to a `friction` block. `evidence` is provenance, not a claim that
  the proposal is known to work; list every relevant entry rather than
  duplicating a proposal.
- Speculative tactic ideas are welcome: a suggestive name plus the proof pattern
  it might automate is enough.
- A `helper-lemma` should describe its approximate statement and where it would
  be reused. An `import-export` proposal should name the missing module.
- An empty `infrastructure_proposals` array is perfectly valid.

Rules for `searched_for`:
- This is a friction report, **not a history of searches**. Include an entry
  only when needed infrastructure was absent or inaccessible, several searches
  or name guesses failed, or the search ended in a materially more complex
  workaround. Omit lookups that succeeded on the first attempt.

If you have no useful notes, that is a complete and valid answer: return
`"nothing_to_report": true` with every array empty. Do not invent entries to
fill the schema."""

_JSON_FENCE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\s*\Z", re.IGNORECASE | re.DOTALL)
_KNOWN_KEYS = frozenset(
    {"friction", "searched_for", "infrastructure_proposals", "nothing_to_report",
     "usage", "error", "raw", "parse_error"}
)


def enabled() -> bool:
    return (os.environ.get(DEBRIEF_ENV) or "").strip().lower() in {"1", "true", "yes"}


def budget_seconds() -> float:
    raw = (os.environ.get(DEBRIEF_BUDGET_ENV) or "").strip()
    return float(raw) if raw else DEFAULT_BUDGET_SECONDS


def debrief_dir(project_dir: str | Path) -> Path:
    configured = (os.environ.get(DEBRIEF_DIR_ENV) or "").strip()
    if not configured:
        return Path(project_dir) / ".autoform" / "debriefs"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else Path(project_dir) / path


def build_question(node_id: str, outcome: str) -> str:
    path = (os.environ.get(DEBRIEF_QUESTION_FILE_ENV) or "").strip()
    template = Path(path).expanduser().read_text(encoding="utf-8") if path else DEFAULT_QUESTION
    return template.replace("{node_id}", node_id).replace("{outcome}", outcome)


def parse_debrief_text(text: str) -> dict[str, Any]:
    """Parse a JSON-object answer, accepting a single optional Markdown fence."""
    raw = text.strip()
    fenced = _JSON_FENCE.fullmatch(raw)
    candidate = fenced.group(1).strip() if fenced is not None else raw
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as error:
        return {"raw": text, "parse_error": str(error)}
    if not isinstance(parsed, dict):
        return {"raw": text, "parse_error": "debrief response was not a JSON object"}
    return parsed


def _string(value: Any, default: str = "Not reported") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    return text or default


def _list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _heading_text(value: Any, fallback: str) -> str:
    text = " ".join(_string(value, fallback).replace("`", "").split())
    if len(text) <= 100:
        return text
    return text[:97].rsplit(" ", 1)[0] + "..."


def _code_block(text: str, language: str = "") -> list[str]:
    runs = [len(run) for run in re.findall(r"`+", text)]
    fence = "`" * max(3, (max(runs) + 1) if runs else 3)
    return [f"{fence}{language}", text, fence]


def _proposal_lines(proposal: dict[str, Any]) -> list[str]:
    name = _string(proposal.get("name"), "")
    lines = [
        f"**Infrastructure kind:** {_string(proposal.get('kind'))}",
        f"**Suggested name:** `{name}`" if name else "**Suggested name:** Unnamed",
        f"**Improvement:** {_string(proposal.get('what'))}",
    ]
    if proposal.get("evidence"):
        lines.append(f"**Evidence:** {_string(proposal['evidence'])}")
    return lines


def _linked_proposals(proposals: list[dict[str, Any]], friction_index: int) -> list[str]:
    reference = re.compile(rf"\bfriction\s+{friction_index}\b", re.IGNORECASE)
    return [
        f"- `{_string(p.get('name'), 'unnamed')}` ({_string(p.get('kind'), 'other')}): "
        f"{_string(p.get('what'))}"
        for p in proposals
        if reference.search(_string(p.get("evidence"), ""))
    ]


def render_debrief_markdown(record: dict[str, Any]) -> str:
    """Render one debrief record as a human-readable Markdown report."""
    debrief = record.get("debrief")
    if not isinstance(debrief, dict):
        debrief = {"raw": debrief, "parse_error": "debrief record was not a JSON object"}
    friction = _list(debrief.get("friction"))
    searched = _list(debrief.get("searched_for"))
    proposals = _list(debrief.get("infrastructure_proposals"))

    lines = [
        f"# Proof debrief: {_string(record.get('node_id'), 'unknown node')}",
        "",
        f"- **Outcome:** {_string(record.get('outcome'))}",
        f"- **Recorded at:** {_string(record.get('recorded_at'))}",
        f"- **Run ID:** `{_string(record.get('run_id'))}`",
        f"- **Backend:** {_string(record.get('backend'))}",
        "",
        "## At a glance",
        "",
        f"- Friction blocks: {len(friction)}",
        f"- Search reports: {len(searched)}",
        f"- Infrastructure proposals: {len(proposals)}",
        f"- Nothing to report: {_string(debrief.get('nothing_to_report'), 'no')}",
        "",
    ]

    if debrief.get("error"):
        lines.extend(["## Debrief error", "", _string(debrief["error"]), ""])
    if debrief.get("parse_error"):
        lines.extend(
            [
                "## Unparsed response",
                "",
                f"**Parse error:** {_string(debrief['parse_error'])}",
                "",
                *_code_block(_string(debrief.get("raw")), "text"),
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(["## Infrastructure proposals", ""])
    if not proposals:
        lines.extend(["No infrastructure proposals were reported.", ""])
    for index, proposal in enumerate(proposals, 1):
        title = _heading_text(proposal.get("name") or proposal.get("kind"), "Infrastructure improvement")
        lines.extend([f"### {index}. {title}", "", *_proposal_lines(proposal), ""])

    lines.extend(["## Mechanical friction", ""])
    if not friction:
        lines.extend(["No qualifying mechanical blocks were reported.", ""])
    for index, item in enumerate(friction, 1):
        lines.extend(
            [
                f"### {index}. {_heading_text(item.get('what'), 'Reported block')}",
                "",
                f"- **Location:** `{_string(item.get('file'))}:{_string(item.get('line'), '?')}`",
                f"- **Block size:** {_string(item.get('lines'), '?')} lines",
                f"- **Difficulty:** {_string(item.get('why_hard'))}",
                "",
                _string(item.get("what")),
                "",
            ]
        )
        linked = _linked_proposals(proposals, index)
        if linked:
            lines.extend(["**Related infrastructure:**", "", *linked, ""])
        if item.get("goal"):
            lines.extend(["**Goal sketch:**", "", *_code_block(_string(item["goal"]), "lean"), ""])

    lines.extend(["## Searches and missing infrastructure", ""])
    if not searched:
        lines.extend(["No searches were reported.", ""])
    for index, item in enumerate(searched, 1):
        lines.extend(
            [
                f"### {index}. {_heading_text(item.get('wanted'), 'Search')}",
                "",
                f"**Found:** {_string(item.get('found'), 'unknown')}",
                *([f"**Difficulty:** {_string(item['why_hard'])}"] if item.get("why_hard") else []),
                "",
                f"**Wanted:** {_string(item.get('wanted'))}",
                "",
                f"**Searched:** {_string(item.get('searched'))}",
                "",
                f"**Resolution:** {_string(item.get('resolution'))}",
                "",
            ]
        )

    # A custom question may ask for fields this renderer does not know about.
    extra = {key: value for key, value in debrief.items() if key not in _KNOWN_KEYS}
    if extra:
        lines.extend(["## Other fields", ""])
        for key, value in extra.items():
            lines.extend([f"### `{key}`", "", *_code_block(json.dumps(value, indent=2, ensure_ascii=False), "json"), ""])

    usage = debrief.get("usage")
    if isinstance(usage, dict):
        lines.extend(["## Debrief usage", ""])
        lines.extend(f"- `{key}`: {_string(value)}" for key, value in usage.items())
        lines.append("")

    return "\n".join(lines)


def _slug(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", _string(value, "unknown")).strip("-") or "unknown"


def _report_name(record: dict[str, Any]) -> str:
    node = _string(record.get("node_id"), "unknown").rsplit("/", 1)[-1]
    return f"{_slug(node)}--{_slug(record.get('recorded_at'))}--{_slug(record.get('run_id'))}.md"


def write_record(directory: Path, record: dict[str, Any]) -> Path:
    """Append ``record`` to the ledger and write its Markdown report."""
    reports = directory / REPORTS_DIRNAME
    reports.mkdir(parents=True, exist_ok=True)
    with (directory / RECORDS_FILENAME).open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(record, ensure_ascii=False) + "\n")
    path = reports / _report_name(record)
    path.write_text(render_debrief_markdown(record), encoding="utf-8")
    return path


def render_reports(directory: Path) -> list[Path]:
    """Re-render every record in ``directory`` and write a linked ``README.md`` index."""
    records_path = directory / RECORDS_FILENAME
    reports_dir = directory / REPORTS_DIRNAME
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[Path, dict[str, Any]]] = []
    for line_number, line in enumerate(records_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at {records_path}:{line_number}: {error}") from error
        if not isinstance(record, dict):
            continue
        path = reports_dir / _report_name(record)
        path.write_text(render_debrief_markdown(record), encoding="utf-8")
        written.append((path, record))

    index = ["# Proof debriefs", "", f"Generated from `{RECORDS_FILENAME}`. Newest last.", ""]
    if not written:
        index.append("No debrief records found.")
    for path, record in written:
        node = _string(record.get("node_id"), "unknown node")
        index.append(f"- [{node} — {_string(record.get('outcome'))}]({REPORTS_DIRNAME}/{path.name})")
    (directory / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return [path for path, _ in written]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-render Autoform proof debriefs as Markdown")
    parser.add_argument("directory", type=Path, help=f"directory containing {RECORDS_FILENAME}")
    args = parser.parse_args(argv)
    reports = render_reports(args.directory)
    print(f"Rendered {len(reports)} debrief report(s) in {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
