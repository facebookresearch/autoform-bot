"""GitHub access for the work loop, through the ``gh`` CLI the operator is logged in with.

Every call goes through :meth:`GitHub.call`, which retries transient failures
and raises :class:`GitHubError` otherwise, so a round that cannot read GitHub
stops instead of mistaking an outage for "nothing to do".
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence

from .. import lean_probe
from .gitops import GitError, network_env, run_command

# Logins whose comments are never a person's request.
BOT_LOGINS = frozenset(
    {
        "github-actions", "facebook-github-bot", "meta-cla", "dependabot", "copilot-pull-request-reviewer",
        "chatgpt-codex-connector", "github-advanced-security", "codecov", "netlify", "vercel",
    }
)
_TRANSIENT = re.compile(
    r"\b50[234]\b|timeout|timed out|EOF|connection reset|was submitted too quickly|secondary rate|try again", re.I
)

PR_FIELDS = """
id number title url state isDraft headRefName headRefOid baseRefName mergeable mergeStateStatus isInMergeQueue
headRepository { nameWithOwner } headRepositoryOwner { login } isCrossRepository
createdAt updatedAt mergedAt closedAt body author { login }
commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) { pageInfo { hasNextPage } nodes {
  __typename
  ... on CheckRun { databaseId name status conclusion startedAt completedAt detailsUrl
                    checkSuite { workflowRun { workflow { name } } } }
  ... on StatusContext { context state createdAt targetUrl } } } } } } }
reviews(last: 50) { nodes { databaseId author { login __typename } state submittedAt body commit { oid } } }
comments(last: 100) { nodes { databaseId author { login __typename } createdAt body url } }
reviewThreads(last: 50) { nodes { isResolved comments(first: 20) { nodes {
  databaseId author { login __typename } createdAt body path line url pullRequestReview { databaseId } } } } }
"""


class GitHubError(RuntimeError):
    pass


def is_bot(author: Mapping[str, object] | None, extra: frozenset[str] = frozenset()) -> bool:
    if not author:
        return True
    login = str(author.get("login") or "")
    return author.get("__typename") == "Bot" or login.endswith("[bot]") or login in BOT_LOGINS or login in extra


def normalize_pr(node: Mapping[str, object]) -> dict[str, object]:
    """A pull request from :data:`PR_FIELDS` as plain data the decision functions read."""

    commits = ((node.get("commits") or {}).get("nodes") or [{}])  # type: ignore[union-attr]
    commit = (commits[-1] or {}).get("commit") or {}
    checks: list[dict[str, object]] = []
    rollup = (commit.get("statusCheckRollup") or {}).get("contexts") or {}
    contexts = rollup.get("nodes") or []
    for item in contexts:
        if not item:
            continue
        if item.get("__typename") == "CheckRun":
            workflow = (((item.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}).get("name")
            checks.append(
                {
                    "name": item.get("name") or "",
                    "workflow": workflow or "",
                    "status": item.get("status") or "",
                    "conclusion": item.get("conclusion") or "",
                    "started_at": item.get("startedAt") or "",
                    "url": item.get("detailsUrl") or "",
                    "id": item.get("databaseId"),
                }
            )
        else:
            state = item.get("state") or ""
            checks.append(
                {
                    "name": item.get("context") or "",
                    "workflow": "",
                    "status": "IN_PROGRESS" if state in ("PENDING", "EXPECTED") else "COMPLETED",
                    "conclusion": {"SUCCESS": "SUCCESS", "FAILURE": "FAILURE", "ERROR": "FAILURE"}.get(state, ""),
                    "started_at": item.get("createdAt") or "",
                    "url": item.get("targetUrl") or "",
                    "id": None,
                }
            )
    reviews = [
        {
            "id": f"review:{review.get('databaseId')}",
            "author": (review.get("author") or {}).get("login") or "",
            "author_type": (review.get("author") or {}).get("__typename") or "",
            "state": review.get("state") or "",
            "submitted_at": review.get("submittedAt") or "",
            "body": review.get("body") or "",
            "commit": (review.get("commit") or {}).get("oid"),
        }
        for review in ((node.get("reviews") or {}).get("nodes") or [])  # type: ignore[union-attr]
        if review
    ]
    comments: list[dict[str, object]] = []
    for comment in (node.get("comments") or {}).get("nodes") or []:  # type: ignore[union-attr]
        if comment:
            comments.append(
                {
                    "id": f"issue:{comment.get('databaseId')}",
                    "kind": "comment",
                    "author": (comment.get("author") or {}).get("login") or "",
                    "bot": is_bot(comment.get("author")),
                    "created_at": comment.get("createdAt") or "",
                    "body": comment.get("body") or "",
                    "url": comment.get("url") or "",
                }
            )
    for thread in (node.get("reviewThreads") or {}).get("nodes") or []:  # type: ignore[union-attr]
        for comment in ((thread or {}).get("comments") or {}).get("nodes") or []:
            if comment:
                comments.append(
                    {
                        "id": f"line:{comment.get('databaseId')}",
                        "kind": "line",
                        "author": (comment.get("author") or {}).get("login") or "",
                        "bot": is_bot(comment.get("author")),
                        "created_at": comment.get("createdAt") or "",
                        "body": comment.get("body") or "",
                        "url": comment.get("url") or "",
                        "path": comment.get("path"),
                        "line": comment.get("line"),
                        "resolved": bool((thread or {}).get("isResolved")),
                        "review": f"review:{(comment.get('pullRequestReview') or {}).get('databaseId')}",
                    }
                )
    for review in reviews:
        author = {"login": review["author"], "__typename": review["author_type"]}
        if review["body"].strip() and not is_bot(author):  # type: ignore[union-attr]
            comments.append(
                {
                    "id": review["id"],
                    "kind": "review",
                    "author": review["author"],
                    "bot": False,
                    "created_at": review["submitted_at"],
                    "body": review["body"],
                    "url": "",
                    "state": review["state"],
                }
            )
    return {
        "id": node.get("id"),
        "number": node.get("number"),
        "title": node.get("title") or "",
        "url": node.get("url") or "",
        "state": node.get("state") or "",
        "draft": bool(node.get("isDraft")),
        "branch": node.get("headRefName") or "",
        "head": node.get("headRefOid") or "",
        "head_repository": (node.get("headRepository") or {}).get("nameWithOwner") or "",  # type: ignore[union-attr]
        "base": node.get("baseRefName") or "",
        "mergeable": node.get("mergeable") or "",
        "merge_state": node.get("mergeStateStatus") or "",
        "in_merge_queue": bool(node.get("isInMergeQueue")),
        "created_at": node.get("createdAt") or "",
        "merged_at": node.get("mergedAt"),
        "closed_at": node.get("closedAt"),
        "body": node.get("body") or "",
        "author": (node.get("author") or {}).get("login") or "",  # type: ignore[union-attr]
        "checks": checks,
        # False when GitHub has more checks than one read returns: merge-readiness then cannot be decided.
        "checks_complete": not (rollup.get("pageInfo") or {}).get("hasNextPage"),
        "reviews": reviews,
        "comments": comments,
    }


class GitHub:
    def __init__(self, gh: str | None = None, *, env: Mapping[str, str] | None = None, retries: int = 3) -> None:
        found = gh or lean_probe.find_program("gh")
        if not found:
            raise GitHubError("the work loop needs the GitHub CLI `gh`, logged in as the account that opens PRs")
        self.gh = found
        self.env = network_env(env)
        self.retries = retries
        self._viewer: str | None = None

    def call(self, *args: str, input_text: str | None = None, timeout: float = 300, retry: bool = True) -> str:
        """Run ``gh`` with ``args``; ``input_text`` reaches it as the file an ``--input -`` argument names.

        A read is retried on a transient failure; a write (``retry=False``) is not, since a lost response
        does not say whether it happened.
        """

        last = ""
        with tempfile.TemporaryDirectory(prefix="autoform-work-gh-") as scratch:
            if input_text is not None:
                path = os.path.join(scratch, "input.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(input_text)
                args = tuple(path if arg == "-" and previous == "--input" else arg
                             for previous, arg in zip(("",) + args, args))
            for attempt in range(self.retries + 1 if retry else 1):
                try:
                    result = run_command([self.gh, *args], env=self.env, timeout=timeout)
                except GitError as error:
                    last = str(error)
                else:
                    if result.returncode == 0:
                        return result.stdout
                    last = (result.stderr or result.stdout or "").strip()
                    if not _TRANSIENT.search(last):
                        break
                if attempt < self.retries:
                    time.sleep(5 * (attempt + 1))
        raise GitHubError(f"gh {' '.join(args[:3])} failed: {last[-2000:]}")

    def api(self, path: str, *, method: str = "GET", body: object = None, accept: str | None = None) -> object:
        args = ["api", path, "--method", method]
        if accept:
            args += ["--header", f"Accept: {accept}"]
        if body is not None:
            args += ["--input", "-"]
        out = self.call(*args, input_text=None if body is None else json.dumps(body), retry=method == "GET")
        return json.loads(out) if out.strip() else None

    def api_pages(self, path: str) -> list[object]:
        out = self.call("api", path, "--paginate", "--slurp")
        pages = json.loads(out) if out.strip() else []
        return [item for page in pages for item in (page if isinstance(page, list) else [page])]

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        data = json.loads(self.call(*args))
        if data.get("errors"):
            raise GitHubError(f"GraphQL: {data['errors']}")
        return data["data"]

    def viewer(self) -> str:
        if self._viewer is None:
            data = self.api("user")
            if not isinstance(data, dict) or not data.get("login"):
                raise GitHubError("cannot read the logged-in GitHub account; run `gh auth login`")
            self._viewer = str(data["login"])
        return self._viewer

    def token(self) -> str:
        return self.call("auth", "token").strip()

    def repository(self, repository: str) -> dict[str, object]:
        data = self.api(f"repos/{repository}")
        if not isinstance(data, dict):
            raise GitHubError(f"cannot read {repository}")
        return data

    def fork_of(self, repository: str) -> str | None:
        """The viewer's fork of ``repository``, if any."""

        name = repository.split("/", 1)[1]
        try:
            data = self.repository(f"{self.viewer()}/{name}")
        except GitHubError:
            data = {}
        parent = ((data.get("parent") or {}).get("full_name") or "").lower()  # type: ignore[union-attr]
        source = ((data.get("source") or {}).get("full_name") or "").lower()  # type: ignore[union-attr]
        if data.get("fork") and repository.lower() in (parent, source):
            return str(data["full_name"])
        for fork in self.api_pages(f"repos/{repository}/forks?per_page=100"):
            if isinstance(fork, dict) and (fork.get("owner") or {}).get("login") == self.viewer():
                return str(fork["full_name"])
        return None

    def create_fork(self, repository: str) -> str:
        data = self.api(f"repos/{repository}/forks", method="POST", body={"default_branch_only": True})
        if not isinstance(data, dict) or not data.get("full_name"):
            raise GitHubError(f"could not fork {repository}")
        return str(data["full_name"])

    def required_checks(self, repository: str, branch: str) -> frozenset[str]:
        """Check names the branch's rulesets and classic protection require (empty when none do).

        A failed read raises :class:`GitHubError`: requirements that cannot be read are not "none".
        """

        rules = self.api(f"repos/{repository}/rules/branches/{branch}")
        names = {
            str(check.get("context"))
            for rule in (rules if isinstance(rules, list) else []) if isinstance(rule, dict)
            and rule.get("type") == "required_status_checks"
            for check in ((rule.get("parameters") or {}).get("required_status_checks") or [])
            if isinstance(check, dict) and check.get("context")
        }
        details = self.api(f"repos/{repository}/branches/{branch}")
        protection = (details.get("protection") or {}) if isinstance(details, dict) else {}
        names.update(str(name) for name in (protection.get("required_status_checks") or {}).get("contexts") or [])
        return frozenset(names)

    def failed_checks_on(self, repository: str, commit: str) -> frozenset[str]:
        """Names of the checks that completed with a failure on ``commit``, such as the base branch's tip."""

        try:
            data = self.api(f"repos/{repository}/commits/{commit}/check-runs?per_page=100")
        except GitHubError:
            return frozenset()
        runs = data.get("check_runs", []) if isinstance(data, dict) else []
        return frozenset(
            str(item.get("name"))
            for item in runs
            if isinstance(item, dict) and item.get("status") == "completed"
            and item.get("conclusion") in ("failure", "timed_out", "startup_failure")
        )

    def pull_request(self, repository: str, number: int) -> dict[str, object]:
        owner, name = repository.split("/", 1)
        query = (
            "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){"
            + PR_FIELDS + "}}}"
        )
        data = self.graphql(query, o=owner, r=name, n=number)
        node = (data.get("repository") or {}).get("pullRequest")  # type: ignore[union-attr]
        if not node:
            raise GitHubError(f"{repository}#{number} not found")
        return normalize_pr(node)

    def open_pull_requests_by(self, repository: str, author: str) -> list[dict[str, object]]:
        """The open pull requests ``author`` opened on ``repository``, with their checks, reviews and comments."""

        query = "query($q:String!){search(query:$q,type:ISSUE,first:100){nodes{... on PullRequest{" + PR_FIELDS + "}}}}"
        # Every loop pull request carries the "Autoform project:" line, so the search need not read all of them.
        data = self.graphql(query, q=f'repo:{repository} is:pr is:open author:{author} "Autoform project" in:body')
        nodes = (data.get("search") or {}).get("nodes") or []  # type: ignore[union-attr]
        return [normalize_pr(node) for node in nodes if node]

    def open_pull_request_texts(self, repository: str) -> list[dict[str, object]]:
        """Number, title, body and branch of every open pull request (the generic taken check reads them)."""

        rows = self.api_pages(f"repos/{repository}/pulls?state=open&per_page=100")
        return [
            {
                "number": row.get("number"),
                "title": row.get("title") or "",
                "body": row.get("body") or "",
                "branch": (row.get("head") or {}).get("ref") or "",
                "author": (row.get("user") or {}).get("login") or "",
                "created_at": row.get("created_at") or "",
            }
            for row in rows
            if isinstance(row, dict)
        ]

    def create_pull_request(
        self, repository: str, *, base: str, head: str, title: str, body: str, draft: bool = False
    ) -> tuple[int, str]:
        payload: dict[str, object] = {"base": base, "head": head, "title": title, "body": body, "draft": draft}
        data = self.api(f"repos/{repository}/pulls", method="POST", body=payload)
        if not isinstance(data, dict) or not isinstance(data.get("number"), int):
            raise GitHubError("GitHub did not return the new pull request")
        return int(data["number"]), str(data.get("html_url") or "")

    def find_pull_request(self, repository: str, head: str) -> dict[str, object] | None:
        owner = head.split(":", 1)[0]
        rows = self.api(f"repos/{repository}/pulls?state=all&head={head}&per_page=5")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and ((row.get("head") or {}).get("user") or {}).get("login") == owner:
                return row
        return None

    def comment(self, repository: str, number: int, body: str) -> str:
        data = self.api(f"repos/{repository}/issues/{number}/comments", method="POST", body={"body": body})
        return str((data or {}).get("html_url") or "") if isinstance(data, dict) else ""

    def add_labels(self, repository: str, number: int, labels: Sequence[str]) -> bool:
        """Add existing labels; ``False`` when the account may not label or a label does not exist."""

        try:
            self.api(f"repos/{repository}/issues/{number}/labels", method="POST", body={"labels": list(labels)})
        except GitHubError:
            return False
        return True

    def job_log(self, repository: str, job_id: str) -> str:
        try:
            return self.call("api", f"repos/{repository}/actions/jobs/{job_id}/logs", timeout=600)
        except GitHubError as error:
            return f"(log unavailable: {error})"

    def render_markdown(self, repository: str, text: str) -> str:
        # Rendering writes nothing, so it may be retried.
        return self.call(
            "api", "markdown", "--method", "POST", "--input", "-",
            input_text=json.dumps({"text": text, "mode": "gfm", "context": repository}),
        )

