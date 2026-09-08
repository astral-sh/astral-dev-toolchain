"""Small, bounded GitHub REST client used by the updater."""

import json
import os
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, StrictStr, StringConstraints

type Commit = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).netloc != "api.github.com" or not newurl.startswith(
            "https://"
        ):
            raise GitHubError("refusing GitHub API redirect to another origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Object(BaseModel):
    sha: Commit
    type: Literal["commit", "tag", "tree", "blob"]


class _TagObject(BaseModel):
    object: _Object


class _Reference(_TagObject):
    ref: StrictStr


class _Commit(BaseModel):
    sha: Commit


class _Tag(BaseModel):
    name: StrictStr
    commit: _Commit


class GitHub:
    def __init__(self, token: str | None = None):
        self.token = (
            token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        )
        self.opener = build_opener(_Redirects())

    def request(self, method: str, endpoint: str, *, data: object = None) -> Any:
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError("expected a GitHub API path")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "astral-dev-toolchain",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(data).encode()
        request = Request(
            f"https://api.github.com{endpoint}",
            headers=headers,
            method=method,
            data=payload,
        )
        # Writes are not retried: an uncertain response is recovered by the next
        # run's branch/PR reconciliation instead of repeating a creation request.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with self.opener.open(request, timeout=30) as response:
                    body = response.read()
                return json.loads(body) if body else None
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After", "")
                error.close()
                retryable = status in {429, 500, 502, 503, 504}
                delay = 2**attempt
                if retry_after:
                    try:
                        delay = max(delay, int(retry_after))
                    except ValueError:
                        retryable = False
                if retryable and attempt + 1 < attempts and delay <= 30:
                    time.sleep(delay)
                    continue
                raise GitHubError(
                    f"GitHub {method} {endpoint}: HTTP {status}", status
                ) from None
            except (URLError, OSError) as error:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise GitHubError(f"GitHub {method} {endpoint}: {error}") from error
        raise AssertionError("unreachable")

    def paginate(self, endpoint: str) -> Iterator[dict[str, Any]]:
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, 101):
            entries = self.request(
                "GET", f"{endpoint}{separator}per_page=100&page={page}"
            )
            if not isinstance(entries, list) or not all(
                isinstance(entry, dict) for entry in entries
            ):
                raise GitHubError(f"expected a list from {endpoint}")
            yield from entries
            if len(entries) < 100:
                return
        raise GitHubError(f"pagination limit exceeded for {endpoint}; scan incomplete")

    def tags(self, repo: str) -> dict[str, str]:
        tags: dict[str, str] = {}
        for raw in self.paginate(f"/repos/{repo}/tags"):
            tag = _Tag.model_validate(raw)
            if tag.name in tags and tags[tag.name] != tag.commit.sha:
                raise GitHubError(f"tag changed during pagination: {repo}/{tag.name}")
            tags[tag.name] = tag.commit.sha
        return tags

    def resolve_tag(self, repo: str, tag: str) -> str:
        ref = _Reference.model_validate(
            self.request("GET", f"/repos/{repo}/git/ref/tags/{quote(tag, safe='')}")
        )
        if ref.ref != f"refs/tags/{tag}":
            raise GitHubError(f"unexpected reference resolving {repo}/{tag}")
        target = ref.object
        seen = set()
        for _ in range(10):
            if target.type == "commit":
                return target.sha
            if target.type != "tag" or target.sha in seen:
                break
            seen.add(target.sha)
            raw = self.request("GET", f"/repos/{repo}/git/tags/{target.sha}")
            target = _TagObject.model_validate(raw).object
        raise GitHubError(f"tag does not resolve to a commit: {repo}/{tag}")
