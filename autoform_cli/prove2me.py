"""Client for submitting Lean proofs to Prove2Me (https://prove2.me).

Prove2Me verifies a `theorem solution` against an open theorem's exact
statement on its own Lean toolchain. This module owns the parts of that loop
that should not be re-improvised by an agent on every run: holding the API key,
resolving a mission, mirroring a target into the local workspace, the gates a
submission must pass, and waiting for a verdict.

The API key is exchanged for a short-lived access token in memory and is never
printed, written, or sent anywhere except :data:`API_BASE`. The base URL is a
constant rather than an option so that no argument can redirect credentials.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

API_BASE = "https://prove2.me/api/v1"
DEFAULT_WORKSPACE = Path("~/prove2me_workspace")
ACCEPTED = frozenset({"ACCEPTED", "SKETCH_ACCEPTED"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_PAGE = 100


class Prove2MeError(Exception):
    """A request, credential, or local gate failed; the message is safe to print."""


# --- Credentials -------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    source: str
    username: str | None = None
    expires_at: datetime.datetime | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.datetime.now(datetime.timezone.utc)


def workspace_path(explicit: str | Path | None = None) -> Path:
    # Absolute, so a path handed to another process still names the same
    # directory after it changes directory.
    raw = explicit or os.environ.get("PROVE2ME_WORKSPACE") or DEFAULT_WORKSPACE
    return Path(os.path.abspath(Path(raw).expanduser()))


def load_credentials(workspace: Path) -> Credentials:
    """Read the key from `$PROVE2ME_API_KEY`, else a credentials.json.

    The key belongs to the account, not to a checkout, so a workspace without
    its own credentials.json (a second checkout pinned to another Mathlib, a
    scratch copy) falls back to the default workspace's.
    """
    env_key = os.environ.get("PROVE2ME_API_KEY", "").strip()
    if env_key:
        return Credentials(env_key, "PROVE2ME_API_KEY", expires_at=_key_expiry(env_key))

    candidates = list(dict.fromkeys(
        directory / "credentials.json"
        for directory in (workspace, workspace_path(), DEFAULT_WORKSPACE.expanduser())
    ))
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise Prove2MeError(
            "no Prove2Me API key: run `autoform prove2me setup` in a terminal, or set PROVE2ME_API_KEY "
            "(see https://prove2.me/start.md)"
        )
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Prove2MeError(f"{path} is not valid JSON: {exc.msg}") from None
    key = str(stored.get("api_key") or "").strip() if isinstance(stored, dict) else ""
    if not key:
        raise Prove2MeError(f"{path} has no api_key")
    expires = _parse_time(stored.get("api_key_expires_at")) or _key_expiry(key)
    return Credentials(key, str(path), username=stored.get("username"), expires_at=expires)


def _parse_time(value: object) -> datetime.datetime | None:
    utc = datetime.timezone.utc
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(value, utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=utc)
    return None


def _key_expiry(key: str) -> datetime.datetime | None:
    """The `exp` claim of a `p2m_<jwt>` key, read without verifying it."""
    parts = key.removeprefix("p2m_").split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    return _parse_time(claims.get("exp")) if isinstance(claims, dict) else None


# --- HTTP client -------------------------------------------------------------


class Client:
    """Authenticated access to :data:`API_BASE`; re-exchanges the key once on a 401."""

    def __init__(self, credentials: Credentials, *, transport: httpx.BaseTransport | None = None) -> None:
        if credentials.expired:
            raise Prove2MeError(
                f"Prove2Me API key from {credentials.source} expired at {credentials.expires_at:%Y-%m-%d %H:%M} UTC; "
                "mint a new one (prove2.me account menu > API key)"
            )
        self._credentials = credentials
        # Redirects are never followed, so a response cannot move the bearer token off-host.
        self._http = httpx.Client(base_url=API_BASE, timeout=60, transport=transport, follow_redirects=False)
        self._token: str | None = None
        self.platform_version: str | None = None

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def authenticate(self) -> None:
        response = self._http.post("/agent/refresh", json={"api_key": self._credentials.api_key})
        if response.status_code == 401:
            raise Prove2MeError(
                f"Prove2Me rejected the API key from {self._credentials.source}; it has expired or been revoked"
            )
        body = _json_or_raise(response)
        self._token = body["access_token"]
        self.platform_version = body.get("version")

    def request(self, method: str, path: str, **kwargs: object) -> object:
        if not path.startswith("/") or "://" in path or path.startswith("//"):
            raise Prove2MeError(f"API path must be relative to {API_BASE}: {path!r}")
        if self._token is None:
            self.authenticate()
        response = self._send(method, path, **kwargs)
        if response.status_code == 401:
            self.authenticate()
            response = self._send(method, path, **kwargs)
        return _json_or_raise(response)

    def _send(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            return self._http.request(method, path, headers=headers, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise Prove2MeError(f"{method} {path}: {type(exc).__name__}: {exc}") from None

    def get(self, path: str, **params: object) -> object:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def paged(self, path: str, key: str, **params: object) -> Iterator[dict]:
        offset = 0
        while True:
            page = self.get(path, limit=_PAGE, offset=offset, **params)
            items = page.get(key) or []  # type: ignore[union-attr]
            yield from items
            total = page.get("total")  # type: ignore[union-attr]
            if total is None:
                total = (page.get("pagination") or {}).get("total")  # type: ignore[union-attr]
            offset += len(items)
            if not items or total is None or offset >= total:
                return


def _json_or_raise(response: httpx.Response) -> object:
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.is_success and body is not None:
        return body
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("error") or body.get("message") or body.get("detail") or "")
    method, path = response.request.method, response.request.url.path.removeprefix("/api/v1")
    hint = " (too many pending submissions; poll them before submitting more)" if response.status_code == 429 else ""
    raise Prove2MeError(f"{method} {path}: HTTP {response.status_code}{': ' + detail if detail else ''}{hint}")


# --- Missions ----------------------------------------------------------------


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def find_mission(client: Client, ref: str) -> dict:
    """Resolve a mission URL, URL slug, name, or id.

    The API has no lookup by slug, so the catalogue is paged and names are
    compared the way the website builds its URLs (spaces to underscores).
    """
    ref = ref.strip().rstrip("/")
    ref = ref.rsplit("/missions/", 1)[-1]
    wanted_id = ref.lower() if _UUID.match(ref) else None
    wanted_name = _normalize(ref)
    for mission in client.paged("/missions", "missions"):
        if wanted_id and mission.get("id", "").lower() == wanted_id:
            return mission
        if not wanted_id and _normalize(mission.get("name", "")) == wanted_name:
            return mission
    raise Prove2MeError(f"no visible Prove2Me mission matches {ref!r}")


def mission_frontier(client: Client, mission: dict) -> dict:
    main = mission.get("main_theorem") or {}
    root = main.get("theorem_id")
    milestones = list(client.paged(f"/missions/{mission['id']}/milestones", "milestones")) if mission.get("id") else []
    leaves = list(client.paged(f"/theorems/{root}/open-leaves", "open_leaves")) if root else []
    leaves.sort(key=lambda leaf: -int(leaf.get("closability") or 0))
    return {
        "mission": {"id": mission.get("id"), "name": mission.get("name")},
        "main_theorem": {k: main.get(k) for k in ("theorem_id", "theorem_name", "status")},
        "milestones": [
            {
                "id": m.get("id"),
                "title": m.get("title"),
                "completed": m.get("completed"),
                "theorem": m.get("theorem"),
            }
            for m in milestones
        ],
        "open_leaves": leaves,
    }


# --- Workspace mirroring -----------------------------------------------------


def theorem_slug(name: str) -> str:
    return name.replace(".", "_")


def workspace_mathlib_rev(workspace: Path) -> str | None:
    manifest = workspace / "lake-manifest.json"
    if manifest.is_file():
        try:
            packages = json.loads(manifest.read_text(encoding="utf-8")).get("packages", [])
        except json.JSONDecodeError:
            packages = []
        for package in packages:
            if package.get("name") == "mathlib" and package.get("rev"):
                return package["rev"]
    lakefile = workspace / "lakefile.lean"
    if lakefile.is_file():
        match = re.search(r'require\s+"?mathlib"?\s+from\s+git\s+"[^"]+"\s*@\s*"([0-9a-f]{7,40})"',
                          lakefile.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    return None


def _imports(source: str) -> list[str]:
    return re.findall(r"^\s*import\s+(\S+)", _strip_comments(source), re.MULTILINE)


@dataclass
class FetchResult:
    theorem: dict
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    workspace_rev: str | None = None

    @property
    def environment_matches(self) -> bool:
        return self.workspace_rev is not None and self.workspace_rev == self.theorem.get("mathlib_rev")


def fetch_theorem(client: Client, theorem_id: str, workspace: Path) -> FetchResult:
    """Mirror a theorem and the platform modules it imports into the workspace.

    Files land where the server resolves them (`Theorems/Thm_<slug>.lean`,
    `Definitions/Def_<name>.lean`), verbatim, so a local build sees what the
    verifier sees.
    """
    theorem = client.get(f"/theorems/{theorem_id}")
    if not isinstance(theorem, dict) or theorem.get("status") == "Definition":
        raise Prove2MeError(f"{theorem_id} is not a provable theorem")
    env = theorem.get("mathlib_rev")
    result = FetchResult(theorem=theorem, workspace_rev=workspace_mathlib_rev(workspace))

    def write(relative: str, text: str) -> None:
        path = workspace / relative
        text = text.rstrip("\n") + "\n"
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            result.unchanged.append(relative)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result.written.append(relative)

    pending = [(theorem_slug(theorem["theorem_name"]), _theorem_source(theorem), "Theorems")]
    seen: set[str] = set()
    while pending:
        name, source, kind = pending.pop()
        module = f"{kind}.{'Thm' if kind == 'Theorems' else 'Def'}_{name}"
        if module in seen:
            continue
        seen.add(module)
        write(f"{module.replace('.', '/')}.lean", source)
        for imported in _imports(source):
            if imported in seen or not imported.startswith(("Theorems.Thm_", "Definitions.Def_")):
                continue
            dependency = _lookup_module(client, imported, env)
            if dependency is None:
                result.unresolved.append(imported)
                seen.add(imported)
            else:
                pending.append(dependency)
    return result


def _theorem_source(theorem: dict) -> str:
    preamble = (theorem.get("preamble") or "").strip()
    statement = theorem.get("formal_statement") or ""
    return f"{preamble}\n\n{statement}" if preamble else statement


def _lookup_module(client: Client, module: str, env: str | None) -> tuple[str, str, str] | None:
    kind, _, leaf = module.partition(".")
    name = leaf.split("_", 1)[1]
    if kind == "Definitions":
        found = client.get("/theorems", status="Definition", theorem_name=name, env=env)
        for item in found.get("theorems", []):  # type: ignore[union-attr]
            if item.get("theorem_name") == name and item.get("definition"):
                return name, item["definition"], kind
        return None
    # A theorem module's slug replaced every '.' with '_', so try the slug as
    # an exact name, then search by its last segment and compare slugs.
    exact = client.get("/theorems", theorem_name=name, env=env)
    by_segment = client.get("/theorems", q=name.rsplit("_", 1)[-1], env=env, limit=_PAGE)
    candidates = list(exact.get("theorems", []))  # type: ignore[union-attr]
    candidates += by_segment.get("theorems", [])  # type: ignore[union-attr]
    for item in candidates:
        if theorem_slug(item.get("theorem_name", "")) == name and item.get("status") != "Definition":
            detail = client.get(f"/theorems/{item['theorem_id']}")
            return name, _theorem_source(detail), kind  # type: ignore[arg-type]
    return None


# --- Local gates -------------------------------------------------------------


def _strip_comments(source: str) -> str:
    """Remove Lean comments and string literals, keeping line structure."""
    out: list[str] = []
    i, depth, n = 0, 0, len(source)
    while i < n:
        if source.startswith("/-", i):
            depth += 1
            i += 2
        elif depth and source.startswith("-/", i):
            depth -= 1
            i += 2
        elif depth:
            out.append("\n" if source[i] == "\n" else " ")
            i += 1
        elif source.startswith("--", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
        elif source[i] == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            out.append('""')
            i = j + 1
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def _has_sorry(code: str) -> bool:
    return re.search(r"(?<![\w.'])(sorry|admit|sorryAx)(?![\w'])", code) is not None


_SOLUTION = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|nonrec)\s+)*theorem\s+solution\b")
_OPENER = re.compile(r"^\s*(?:noncomputable\s+)?(namespace|section|mutual)\b\s*(\S*)")
_END = re.compile(r"^\s*end\b")


def check_solution(source: str, target_name: str) -> list[str]:
    """Problems Prove2Me would reject before type-checking, as readable strings.

    These are the platform's gating rules: a top-level `theorem solution`, no
    `sorry` in the submitted file, and no import of the target's own module.
    """
    code = _strip_comments(source)
    problems: list[str] = []
    if _has_sorry(code):
        problems.append("contains sorry/admit; the submitted file must be sorry-free")
    own = f"Theorems.Thm_{theorem_slug(target_name)}"
    if own in _imports(source):
        problems.append(f"imports its own target {own}; the server stores it as sorry and rejects this")

    scopes: list[str] = []
    found_top_level = found_nested = False
    for line in code.splitlines():
        if _SOLUTION.match(line):
            if "namespace" in scopes:
                found_nested = True
            else:
                found_top_level = True
        opener = _OPENER.match(line)
        if opener:
            scopes.append(opener.group(1))
        elif _END.match(line) and scopes:
            scopes.pop()
    if found_nested and not found_top_level:
        problems.append("theorem solution is inside a namespace; it must be top-level")
    elif not found_top_level:
        problems.append("no top-level `theorem solution` with the target's exact type")
    return problems


def compile_locally(path: Path, workspace: Path, target_name: str, timeout: float) -> tuple[bool, str]:
    """Build the solution and check its type against the mirrored target.

    Compiling alone would accept a proof of the wrong statement, which the
    server reports as WA. So the solution and the target's module are built,
    then `example : type_of% @target := @solution` is elaborated against both,
    the same comparison the verifier makes. Returns (ok, combined output).
    """
    try:
        relative = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        relative = None
    if relative is None or relative.parts[0] != "Solutions" or relative.suffix != ".lean":
        return False, f"put the solution under {workspace / 'Solutions'} so its type can be checked against the target"
    solution_module = ".".join(relative.with_suffix("").parts)
    target_module = f"Theorems.Thm_{theorem_slug(target_name)}"
    if not (workspace / "Theorems" / f"Thm_{theorem_slug(target_name)}.lean").is_file():
        return False, f"{target_module} is not mirrored; run `autoform prove2me fetch` first"

    check = workspace / ".lake" / "autoform-prove2me" / "TypeCheck.lean"
    check.parent.mkdir(parents=True, exist_ok=True)
    check.write_text(
        f"import {solution_module}\nimport {target_module}\n\n"
        f"example : type_of% @{target_name} := @solution\n",
        encoding="utf-8",
    )
    output: list[str] = []
    for command in (["lake", "build", solution_module, target_module], ["lake", "env", "lean", str(check)]):
        try:
            run = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return False, "lake not found on PATH"
        except subprocess.TimeoutExpired:
            return False, f"{' '.join(command)} timed out after {timeout:g}s"
        output.append((run.stdout + run.stderr).strip())
        if run.returncode != 0:
            if command[1] == "env":
                output.append("the solution's type does not match the target's formal_statement")
            return False, "\n".join(filter(None, output))
    return True, "\n".join(filter(None, output))


# --- Submission --------------------------------------------------------------


def submit(
    client: Client,
    theorem_id: str,
    path: Path,
    *,
    proof_type: str = "prove",
    explanation: str | None = None,
) -> dict:
    data = {"theorem_id": theorem_id, "proof_type": proof_type}
    if explanation:
        data["explanation"] = explanation
    files = {"file": ("solution.lean", path.read_bytes(), "text/plain")}
    return client.request("POST", "/verify", data=data, files=files)  # type: ignore[return-value]


def wait_for_verdict(
    client: Client,
    submission_id: str,
    *,
    timeout: float = 1800,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict:
    return _poll(client, "/verify", {"PENDING"}, timeout, sleep, clock, submission_id=submission_id)


def _poll(
    client: Client,
    path: str,
    pending: set[str],
    timeout: float,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], float] | None,
    **params: object,
) -> dict:
    """GET `path` with a capped exponential backoff until its status leaves `pending`."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout
    delay = 5.0
    while True:
        state = client.get(path, **params)
        if state.get("status") not in pending or clock() + delay > deadline:  # type: ignore[union-attr]
            return state  # type: ignore[return-value]
        sleep(delay)
        delay = min(delay * 2, 30.0)


def set_explanation(client: Client, submission_id: str, explanation: str | None) -> dict:
    return client.request(  # type: ignore[return-value]
        "PATCH", f"/submissions/{submission_id}", json={"explanation": explanation}
    )


# --- Publishing lemmas and definitions ---------------------------------------

PUBLISHED_TERMINAL = frozenset({"PUBLISHED", "FAILED", "ERROR"})
_PROBLEM_FIELDS = ("theorem_name", "theorem_title", "formal_statement", "natural_language_statement")
_DEFINITION_FIELDS = ("definition_name", "definition_title", "definition")


def publish_items(kind: str, spec: object) -> list[dict]:
    """Normalize a problem or definition spec file into a list of request bodies."""
    if kind == "problem" and isinstance(spec, dict) and "problems" in spec:
        spec = spec["problems"]
    items = spec if isinstance(spec, list) else [spec]
    if not items or not all(isinstance(item, dict) for item in items):
        raise Prove2MeError(f"a {kind} spec must be a JSON object or a list of objects")
    if kind == "definition" and len(items) != 1:
        raise Prove2MeError("publish one definition per spec file")
    return items


def check_publish(kind: str, item: dict) -> list[str]:
    """What the publish endpoint would reject before compiling, as readable strings."""
    required = _PROBLEM_FIELDS if kind == "problem" else _DEFINITION_FIELDS
    label = item.get("theorem_name") or item.get("definition_name") or "<unnamed>"
    if "private" in item or "env" in item:
        return [f"{label}: set visibility and environment with --private and --env, not in the spec"]
    problems = [f"{label}: missing {name}" for name in required if not str(item.get(name) or "").strip()]
    if problems:
        return problems
    if kind == "problem":
        statement = _strip_comments(item["formal_statement"])
        if not re.search(r":=\s*by\s+sorry\s*(?:end\s+\S+\s*)*$", statement):
            problems.append(f"{label}: formal_statement must end with `:= by sorry`")
        short = item["theorem_name"].rsplit(".", 1)[-1]
        if not re.search(rf"\btheorem\s+(?:[\w.]*\.)?{re.escape(short)}\b", statement):
            problems.append(f"{label}: formal_statement does not declare theorem {item['theorem_name']}")
        if _has_sorry(_strip_comments(item.get("preamble") or "")):
            problems.append(f"{label}: preamble contains sorry")
    else:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", item["definition_name"]):
            problems.append(f"{label}: definition_name must match [a-zA-Z_][a-zA-Z0-9_]*")
        if _has_sorry(_strip_comments(item["definition"])):
            problems.append(f"{label}: a definition file must be sorry-free")
    for name in ("theorem_title", "definition_title"):
        if len(str(item.get(name) or "")) > 200:
            problems.append(f"{label}: {name} exceeds 200 characters")
    return problems


def publish(client: Client, kind: str, items: list[dict], env: str | None, *, private: bool = False) -> list[dict]:
    """Queue the items; returns [{name, job_id}] and raises if any was refused outright.

    `env` and `private` apply to the whole batch, so they are set here rather
    than trusted from the spec; a per-item `private` key is not honoured.
    """
    extra: dict[str, object] = {"env": env} if env else {}
    if private:
        extra["private"] = True
    if kind == "problem":
        response = client.request("POST", "/submit-problem", json={"problems": items, **extra})
        errors = response.get("errors") or []  # type: ignore[union-attr]
        jobs = response.get("jobs") or []  # type: ignore[union-attr]
    else:
        response = client.request("POST", "/submit-definition", json={**items[0], **extra})
        errors = []
        jobs = [{"job_id": response["job_id"], "name": response.get("definition_name")}]  # type: ignore[index]
    if errors:
        raise Prove2MeError(f"refused before queueing: {json.dumps(errors, ensure_ascii=False)}")
    return [{"name": job.get("name"), "job_id": job["job_id"]} for job in jobs]


def wait_for_job(
    client: Client,
    job_id: str,
    *,
    timeout: float = 900,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict:
    return _poll(client, f"/publish-jobs/{job_id}", {"PENDING", "COMPILING"}, timeout, sleep, clock)


# --- Mission proposals -------------------------------------------------------
#
# A proposal is a private draft of a new mission. Its items are stored text
# until the user confirms each one and clicks Submit Proposal on the website;
# only then are they compiled and published. That step is human-only, so
# nothing here can make a proposal public.

MISSION_TYPES = ("OpenProblem", "Textbook", "ResearchPaper")
EDITABLE_PROPOSAL = frozenset({"Draft", "Changes requested"})
_PROPOSAL_KEYS = frozenset({"name", "description", "mission_type", "field_ids", "env", "visibility"})
# Keys of a spec item that steer the upload and are never sent as item fields.
_LOCAL_ITEM_KEYS = ("kind", "goal", "milestone")
DRAFT_MARKER = "-- autoform: local draft of a Prove2Me mission proposal item; not published\n"


def item_name(item: dict) -> str:
    if item.get("kind") == "reference":
        return f"reference {item.get('theorem_id')}"
    return str(item.get("theorem_name") or item.get("definition_name") or "<unnamed>")


def _item_body(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in _LOCAL_ITEM_KEYS}


def _draft_module(item: dict) -> str:
    if item["kind"] == "definition":
        return f"Definitions.Def_{item['definition_name']}"
    return f"Theorems.Thm_{theorem_slug(item['theorem_name'])}"


def check_proposal(spec: object) -> tuple[list[str], list[str]]:
    """(problems, warnings) for a proposal spec, before anything is compiled or sent.

    The spec is {"proposal": {...metadata...}, "items": [...]}, where items are
    in dependency order and each is a `theorem` or `definition` draft (the same
    body as /submit-problem or /submit-definition) or a `reference` to a
    published theorem. One item carries `"goal": true`; theorem items may carry
    `"milestone": {"title", "description"}`.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("proposal"), dict) or not isinstance(
        spec.get("items"), list
    ):
        return ['the spec must be {"proposal": {...}, "items": [...]}'], []
    meta, items = spec["proposal"], spec["items"]
    problems: list[str] = []
    warnings: list[str] = []

    unknown = sorted(set(meta) - _PROPOSAL_KEYS)
    if unknown:
        problems.append(f"proposal: unknown field(s) {', '.join(unknown)}")
    name = str(meta.get("name") or "").strip()
    if not name:
        problems.append("proposal: missing name")
    elif len(name) > 200:
        problems.append("proposal: name exceeds 200 characters")
    if meta.get("mission_type") not in MISSION_TYPES:
        problems.append(f"proposal: mission_type must be one of {', '.join(MISSION_TYPES)}")
    if meta.get("visibility", "public") not in ("public", "private"):
        problems.append("proposal: visibility must be public or private")
    if not str(meta.get("description") or "").strip():
        problems.append("proposal: missing description (the mission introduction)")
    fields = meta.get("field_ids", [])
    if not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields):
        problems.append("proposal: field_ids must be a list of field ids")
    elif not fields:
        warnings.append("proposal: no field_ids; at least one is needed before the proposal can be submitted")

    if not items:
        problems.append("items: the proposal needs at least its goal theorem")
    goals = [item for item in items if isinstance(item, dict) and item.get("goal")]
    if len(goals) != 1:
        problems.append(f"items: exactly one item must have \"goal\": true (found {len(goals)})")

    position = {_draft_module(item): index for index, item in enumerate(items)
                if isinstance(item, dict) and item.get("kind") in ("theorem", "definition")
                and (item.get("theorem_name") or item.get("definition_name"))}
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"items[{index}]: not an object")
            continue
        kind, label = item.get("kind"), item_name(item)
        if kind not in ("theorem", "definition", "reference"):
            problems.append(f"{label}: kind must be theorem, definition, or reference")
            continue
        if label in seen:
            problems.append(f"{label}: appears twice")
        seen.add(label)
        if "env" in item or "private" in item:
            problems.append(f"{label}: set env and visibility on the proposal, not on an item")
            continue
        if kind == "definition" and item.get("goal"):
            problems.append(f"{label}: a definition cannot be the goal")
        milestone = item.get("milestone")
        if milestone is not None:
            if kind == "definition" or item.get("goal"):
                problems.append(f"{label}: only a non-goal theorem can be a milestone")
            elif not isinstance(milestone, dict) or not all(
                str(milestone.get(key) or "").strip() for key in ("title", "description")
            ):
                problems.append(f"{label}: milestone needs a title and a description")
            elif len(milestone["title"]) > 200:
                problems.append(f"{label}: milestone title exceeds 200 characters")
        if kind == "reference":
            if not str(item.get("theorem_id") or "").strip():
                problems.append(f"{label}: a reference needs theorem_id")
            extra = sorted(set(item) - {"kind", "theorem_id", "goal", "milestone"})
            if extra:
                problems.append(f"{label}: a reference carries only theorem_id (found {', '.join(extra)})")
            continue

        problems.extend(check_publish("problem" if kind == "theorem" else "definition", _item_body(item)))
        if item.get("readback") and not item.get("readback_model"):
            problems.append(f"{label}: readback_model is required with a readback")
        if not item.get("readback"):
            warnings.append(f"{label}: no read-back; the reviewer's audit page will show an empty placeholder")
        source = item.get("preamble", "") if kind == "theorem" else item.get("definition", "")
        for imported in _imports(source or ""):
            if position.get(imported, -1) > index:
                problems.append(f"{label}: imports {imported}, which is listed after it; put dependencies first")
    return problems, warnings


def compile_proposal(items: list[dict], workspace: Path, timeout: float) -> tuple[bool, str]:
    """Build every draft item in the workspace exactly where the platform would put it.

    Drafts are written to `Definitions/Def_<name>.lean` and
    `Theorems/Thm_<slug>.lean` so later items can import earlier ones, as they
    will after publishing. A file is only overwritten when it is absent or an
    earlier draft written here, so a mirrored platform module is never clobbered:
    a clash means the name is already taken in this environment.
    """
    modules = []
    for item in items:
        if item.get("kind") not in ("theorem", "definition"):
            continue
        module = _draft_module(item)
        path = workspace / f"{module.replace('.', '/')}.lean"
        if path.is_file() and not path.read_text(encoding="utf-8").startswith(DRAFT_MARKER):
            return False, f"{path} already exists and is not a local draft; {item_name(item)} may be a taken name"
        source = item["definition"] if item["kind"] == "definition" else _theorem_source(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DRAFT_MARKER + source.strip("\n") + "\n", encoding="utf-8")
        modules.append(module)
    if not modules:
        return True, "no draft items to build"
    try:
        run = subprocess.run(["lake", "build", *modules], cwd=workspace, capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError:
        return False, "lake not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"lake build timed out after {timeout:g}s"
    return run.returncode == 0, (run.stdout + run.stderr).strip()


def upload_proposal(client: Client, spec: dict, proposal_id: str | None = None) -> dict:
    """Create (or update) the draft proposal, its items, goal, order, and milestones.

    Items are matched to an existing proposal's by name (references by
    theorem_id) and patched in place, so re-running after a fix updates rather
    than duplicates. Items already in the proposal but absent from the spec are
    kept at the end of the order and reported, never deleted.
    """
    meta = spec["proposal"]
    if proposal_id:
        proposal = client.get(f"/mission-proposals/{proposal_id}")
        status, env = proposal.get("status"), proposal.get("env")  # type: ignore[union-attr]
        if status not in EDITABLE_PROPOSAL:
            raise Prove2MeError(f"proposal {proposal_id} is {status!r} and can no longer be edited")
        if meta.get("env") and not str(env or "").startswith(meta["env"]):
            raise Prove2MeError(f"proposal {proposal_id} targets env {env}, not {meta.get('env')}")
        patch = {key: meta[key] for key in ("description", "mission_type", "field_ids", "visibility") if key in meta}
        if patch:
            client.request("PATCH", f"/mission-proposals/{proposal_id}", json=patch)
    else:
        body = {key: meta[key] for key in _PROPOSAL_KEYS if meta.get(key) not in (None, "")}
        proposal = client.request("POST", "/mission-proposals", json=body)
        proposal_id = proposal["id"]  # type: ignore[index]

    try:
        return _fill_proposal(client, proposal_id, proposal, spec["items"])  # type: ignore[arg-type]
    except Prove2MeError as exc:
        raise Prove2MeError(f"{exc} (proposal {proposal_id} exists; fix the spec and re-run with "
                            f"--proposal {proposal_id})") from None


def _fill_proposal(client: Client, proposal_id: str, proposal: dict, items: list[dict]) -> dict:
    existing = {}
    for current in proposal.get("items") or []:
        key = (f"reference {current.get('theorem_id')}" if current.get("kind") == "reference"
               else str(current.get("theorem_name") or current.get("definition_name")))
        existing[key] = current["id"]

    base = f"/mission-proposals/{proposal_id}"
    order, goal, report = [], None, []
    for item in items:
        label, body = item_name(item), _item_body(item)
        if label in existing:
            item_id = existing.pop(label)
            if item["kind"] != "reference":
                client.request("PATCH", f"{base}/items/{item_id}", json=body)
            action = "updated" if item["kind"] != "reference" else "kept"
        else:
            created = client.request("POST", f"{base}/items", json={"kind": item["kind"], **body})
            item_id, action = created["id"], "added"  # type: ignore[index]
        order.append(item_id)
        if item.get("goal"):
            goal = item_id
        report.append({"name": label, "kind": item["kind"], "item_id": item_id, "action": action})

    leftover = list(existing.values())
    client.request("PATCH", base, json={"main_item_id": goal, "item_order": order + leftover})

    milestones = []
    for item, entry in zip(items, report):
        milestone = item.get("milestone")
        if milestone:
            client.request("POST", f"{base}/milestones", json={
                "item_id": entry["item_id"],
                "milestone_title": milestone["title"],
                "milestone_description": milestone["description"],
            })
            milestones.append(milestone["title"])

    final = client.get(base)
    return {
        "proposal_id": proposal_id,
        "name": final.get("name"),  # type: ignore[union-attr]
        "status": final.get("status"),  # type: ignore[union-attr]
        "visibility": final.get("visibility"),  # type: ignore[union-attr]
        "env": final.get("env"),  # type: ignore[union-attr]
        "items": report,
        "milestones": milestones,
        "not_in_spec": list(existing),
    }


# --- Workspace setup ---------------------------------------------------------

WORKSPACE_REPO = "https://github.com/prove2me/prove2me_workspace.git"
_LAKEFILE = """import Lake
open Lake DSL

package «prove2me» where
  leanOptions := #[⟨`autoImplicit, false⟩]

require mathlib from git
  "https://github.com/leanprover-community/mathlib4.git" @
  "{rev}"

lean_lib «Definitions» where
lean_lib «Theorems» where
@[default_target]
lean_lib «Solutions» where
"""


def store_api_key(workspace: Path, key: str) -> Path:
    """Save the key into the workspace's credentials.json, readable only by the user.

    Other fields (username, email) are kept. The workspace repository
    gitignores this file, so it is never committed.
    """
    path = workspace / "credentials.json"
    stored: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            stored = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            stored = {}
    stored["api_key"] = key
    expires = _key_expiry(key)
    if expires is not None:
        stored["api_key_expires_at"] = int(expires.timestamp())
    else:
        stored.pop("api_key_expires_at", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=2)
    os.chmod(path, 0o600)
    return path


def environments(client: Client) -> list[dict]:
    return list(client.get("/environments").get("environments", []))  # type: ignore[union-attr]


def resolve_environment(client: Client, rev: str | None) -> dict:
    """The platform environment for `rev` (a full or abbreviated mathlib_rev), or the default."""
    envs = environments(client)
    for env in envs:
        if rev is None and env.get("is_default"):
            return env
        if rev is not None and str(env.get("mathlib_rev", "")).startswith(rev):
            return env
    known = ", ".join(str(env.get("mathlib_rev", ""))[:7] for env in envs)
    raise Prove2MeError(f"no Prove2Me environment matches {rev or 'default'} (known: {known})")


def mission_environment(client: Client, mission_ref: str) -> str:
    mission = find_mission(client, mission_ref)
    main = (mission.get("main_theorem") or {}).get("theorem_id")
    if not main:
        raise Prove2MeError(f"mission {mission.get('name')!r} has no goal theorem")
    rev = client.get(f"/theorems/{main}").get("mathlib_rev")  # type: ignore[union-attr]
    if not rev:
        raise Prove2MeError(f"could not read the Mathlib environment of mission {mission.get('name')!r}")
    return rev


def choose_workspace(default: Path, rev: str) -> Path:
    """Where to work on theorems in environment `rev`.

    Prove2Me recommends one checkout per environment rather than re-pinning, so
    the default workspace is used when it is unpinned or already pinned to
    `rev`, and otherwise a sibling `prove2me-<rev7>` checkout.
    """
    pinned = workspace_mathlib_rev(default) if default.is_dir() else None
    if pinned is None or pinned == rev:
        return default
    return default.parent / f"prove2me-{rev[:7]}"


def clone_workspace(workspace: Path) -> None:
    if workspace.exists():
        return
    run = subprocess.run(["git", "clone", "--quiet", WORKSPACE_REPO, str(workspace)], capture_output=True, text=True)
    if run.returncode != 0:
        raise Prove2MeError(f"could not clone {WORKSPACE_REPO}: {run.stderr.strip()}")


def pin_environment(workspace: Path, env: dict) -> list[str]:
    """Write lean-toolchain and lakefile.lean for `env` where they are missing.

    The workspace repository gitignores both because they are per-environment.
    An existing pin is never overwritten: a mismatch is an error, so a checkout
    holding another environment's mirrored files is not silently re-pinned.
    """
    rev, toolchain = env["mathlib_rev"], env["toolchain"]
    pinned = workspace_mathlib_rev(workspace)
    if pinned is not None and pinned != rev:
        raise Prove2MeError(
            f"{workspace} is pinned to Mathlib {pinned[:7]}, not {rev[:7]}; use another workspace for this environment"
        )
    written = []
    toolchain_file = workspace / "lean-toolchain"
    if not toolchain_file.is_file():
        toolchain_file.write_text(toolchain + "\n", encoding="utf-8")
        written.append("lean-toolchain")
    elif toolchain_file.read_text(encoding="utf-8").strip() != toolchain:
        raise Prove2MeError(f"{toolchain_file} does not name {toolchain}; fix it or use another workspace")
    lakefile = workspace / "lakefile.lean"
    if not lakefile.is_file() and not (workspace / "lakefile.toml").is_file():
        lakefile.write_text(_LAKEFILE.format(rev=rev), encoding="utf-8")
        written.append("lakefile.lean")
    return written


def share_credentials(workspace: Path, home: Path) -> bool:
    """Link a per-environment checkout's credentials.json to the home workspace's.

    A script that points PROVE2ME_WORKSPACE at the checkout hides the home
    workspace from the key lookup. A symlink keeps one copy of the key, which
    the workspace repository gitignores like the file itself.
    """
    target, link = home / "credentials.json", workspace / "credentials.json"
    if workspace.resolve() == home.resolve() or not target.is_file() or link.exists() or link.is_symlink():
        return False
    link.symlink_to(target.resolve())
    return True


def mathlib_ready(workspace: Path) -> bool:
    return (workspace / ".lake" / "packages" / "mathlib" / ".lake" / "build").is_dir()
