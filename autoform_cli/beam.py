"""Verify an installed Lean Beam runtime against Autoform's preview pin."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


BEAM_INSPECTION_SCHEMA = "autoform-lean-beam-inspection/v1"
_LOCK_NAME = "lean-beam.lock.json"
_COMMIT = re.compile(r"[0-9a-f]{40}")
_HOST_CONTROLS = (
    "deny lean_save and lean_close_save",
    "set a finite tool deadline",
    "confine the Beam owner and Lean children at the OS or container boundary",
    "do not overlap Beam calls with external builds",
)


@dataclass(frozen=True, slots=True)
class BeamIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BeamInspection:
    command: str | None
    expected: dict[str, object]
    observed: dict[str, object] | None
    required_host_controls: tuple[str, ...]
    issues: tuple[BeamIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "expected": self.expected,
            "issues": [asdict(issue) for issue in self.issues],
            "observed": self.observed,
            "ok": self.ok,
            "required_host_controls": list(self.required_host_controls),
            "schema": BEAM_INSPECTION_SCHEMA,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


class BeamProbeError(RuntimeError):
    pass


class BeamInstallError(RuntimeError):
    pass


def load_beam_lock() -> dict[str, object]:
    """Load the one preview lock from a wheel or source checkout."""

    resource = files("autoform_cli").joinpath(_LOCK_NAME)
    try:
        text = resource.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = (Path(__file__).resolve().parent.parent / _LOCK_NAME).read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Lean Beam lock is not an object")
    return payload


def inspect_beam_runtime(
    command: str | None = None,
    *,
    timeout: float = 15.0,
) -> BeamInspection:
    """Probe Beam's public identity tool and compare it with the preview lock."""

    expected = load_beam_lock()
    try:
        executable = ensure_managed_beam(timeout=max(timeout, 900.0)) if command is None else _resolve_executable(command)
    except BeamInstallError as error:
        return BeamInspection(
            None,
            expected,
            None,
            _HOST_CONTROLS,
            (BeamIssue("beam-install-failed", str(error)),),
        )
    if executable is None:
        return BeamInspection(
            None,
            expected,
            None,
            _HOST_CONTROLS,
            (BeamIssue("beam-not-found", f"Lean Beam executable was not found: {command}"),),
        )

    try:
        observed = asyncio.run(asyncio.wait_for(_probe_identity(executable, timeout), timeout=timeout))
    except TimeoutError:
        issues = (BeamIssue("beam-timeout", f"Lean Beam identity probe exceeded {timeout:g} seconds"),)
        return BeamInspection(executable, expected, None, _HOST_CONTROLS, issues)
    except (BeamProbeError, OSError, RuntimeError, ValueError) as error:
        issues = (BeamIssue("beam-probe-failed", str(error)),)
        return BeamInspection(executable, expected, None, _HOST_CONTROLS, issues)

    issues = _identity_issues(expected, observed)
    return BeamInspection(executable, expected, observed, _HOST_CONTROLS, issues)


def ensure_managed_beam(*, timeout: float = 900.0) -> str:
    """Install the exact Beam pin into Autoform-owned state and return its MCP command."""

    lock = load_beam_lock()
    commit = lock.get("commit")
    repository = lock.get("repository")
    toolchains = lock.get("tested_toolchains")
    if (
        not isinstance(commit, str)
        or _COMMIT.fullmatch(commit) is None
        or not isinstance(repository, str)
        or not repository.startswith("https://github.com/")
        or not isinstance(toolchains, list)
        or not toolchains
        or not all(isinstance(item, str) and item for item in toolchains)
    ):
        raise BeamInstallError("Autoform's Lean Beam lock is malformed")

    root = _managed_root() / commit
    executable = root / "bin" / "lean-beam-mcp"
    if executable.is_file() and os.access(executable, os.X_OK):
        return str(executable.resolve())

    root.parent.mkdir(parents=True, exist_ok=True)
    install_lock = root.parent / f".{commit}.installing"
    try:
        install_lock.mkdir()
    except FileExistsError:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if executable.is_file() and os.access(executable, os.X_OK):
                return str(executable.resolve())
            time.sleep(0.25)
        raise BeamInstallError(f"timed out waiting for another Lean Beam install: {install_lock}") from None

    try:
        if executable.is_file() and os.access(executable, os.X_OK):
            return str(executable.resolve())
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        log = root / "install.log"
        with TemporaryDirectory(prefix=f"autoform-beam-{commit[:12]}-", dir=root.parent) as temporary:
            source = Path(temporary) / "source"
            _run_install_step(
                [
                    "git",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "clone",
                    "--no-checkout",
                    "--filter=blob:none",
                    repository,
                    str(source),
                ],
                cwd=root.parent,
                log=log,
                timeout=min(timeout, 300.0),
            )
            _run_install_step(
                [
                    "git",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "fetch",
                    "--depth=1",
                    "origin",
                    commit,
                ],
                cwd=source,
                log=log,
                timeout=min(timeout, 300.0),
            )
            _run_install_step(
                ["git", "-c", "core.hooksPath=/dev/null", "checkout", "--detach", "FETCH_HEAD"],
                cwd=source,
                log=log,
                timeout=min(timeout, 120.0),
            )
            head = _run_install_step(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                log=log,
                timeout=30,
            ).stdout.strip()
            if head != commit:
                raise BeamInstallError(f"Lean Beam checkout resolved to {head}, expected {commit}")
            environment = dict(os.environ)
            environment.update(
                {
                    "BEAM_BIN_HOME": str(root / "bin"),
                    "BEAM_INSTALL_ROOT": str(root / "runtime"),
                }
            )
            command = [str(source / "scripts/install-beam.sh"), "--dont-ask"]
            for toolchain in toolchains:
                command.extend(("--toolchain", toolchain))
            _run_install_step(command, cwd=source, log=log, timeout=timeout, env=environment)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise BeamInstallError(f"Lean Beam installer did not create {executable}")
        return str(executable.resolve())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise BeamInstallError(f"Lean Beam bootstrap failed; inspect {root / 'install.log'}") from error
    finally:
        install_lock.rmdir()


async def _probe_identity(executable: str, timeout: float) -> dict[str, object]:
    parameters = StdioServerParameters(command=executable)
    with TemporaryFile(mode="w+t", encoding="utf-8") as errors:
        try:
            async with stdio_client(parameters, errlog=errors) as (reader, writer):
                async with ClientSession(
                    reader,
                    writer,
                    read_timeout_seconds=timedelta(seconds=timeout),
                ) as session:
                    await session.initialize()
                    result = await session.call_tool("beam_version")
        except Exception as error:
            errors.flush()
            errors.seek(0)
            detail = " ".join(errors.read(4096).split())
            suffix = f": {detail}" if detail else ""
            raise BeamProbeError(f"Lean Beam MCP probe failed{suffix}") from error
    if result.isError or not isinstance(result.structuredContent, dict):
        raise BeamProbeError("beam_version did not return structured runtime identity")
    return result.structuredContent


def _identity_issues(
    expected: dict[str, object], observed: dict[str, object]
) -> tuple[BeamIssue, ...]:
    requirements = {
        "name": "lean-beam-mcp",
        "version": expected.get("version"),
        "mcp_protocol": expected.get("mcp_protocol"),
        "source_commit": expected.get("commit"),
        "runtime_current": True,
        "runtime_active": False,
    }
    issues = [
        BeamIssue(
            "beam-identity-mismatch",
            f"beam_version {field} is {observed.get(field)!r}; expected {value!r}",
        )
        for field, value in requirements.items()
        if observed.get(field) != value
    ]
    if observed.get("source_dirty") is True:
        issues.append(BeamIssue("beam-source-dirty", "Lean Beam reports a dirty source checkout"))
    if runtime_error := observed.get("runtime_error"):
        issues.append(BeamIssue("beam-runtime-error", f"Lean Beam reports: {runtime_error}"))
    return tuple(issues)


def _resolve_executable(command: str) -> str | None:
    expanded = os.path.expanduser(command)
    if os.path.dirname(expanded):
        path = Path(expanded).resolve()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    resolved = shutil.which(expanded)
    return str(Path(resolved).resolve()) if resolved is not None else None


def _managed_root() -> Path:
    configured = os.environ.get("AUTOFORM_BEAM_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return (data / "autoform/lean-beam").resolve()


def _run_install_step(
    command: list[str],
    *,
    cwd: Path,
    log: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    from .skeleton import SkeletonError, _run_bounded_command

    try:
        result = _run_bounded_command(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            context="Lean Beam install",
            output_limit=8 * 1024 * 1024,
        )
    except SkeletonError as error:
        with log.open("a", encoding="utf-8") as output:
            output.write(f"$ {' '.join(command)}\n{error}\n")
        raise BeamInstallError(str(error)) from error
    with log.open("a", encoding="utf-8") as output:
        output.write(f"$ {' '.join(command)}\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        raise BeamInstallError(f"Lean Beam install step failed ({result.returncode}): {command[0]}")
    return result


__all__ = [
    "BEAM_INSPECTION_SCHEMA",
    "BeamInspection",
    "BeamIssue",
    "ensure_managed_beam",
    "inspect_beam_runtime",
    "load_beam_lock",
]
