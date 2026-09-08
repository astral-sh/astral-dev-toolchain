"""Small, bounded GitHub REST client used by the updater."""

import base64
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from http.client import IncompleteRead
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, StrictStr, StringConstraints

type Commit = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{40}$")]

logger = logging.getLogger(__name__)


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
            started = time.monotonic()
            logger.debug(
                "GitHub %s %s (attempt %d/%d)", method, endpoint, attempt + 1, attempts
            )
            try:
                # This bounds individual socket operations, not the entire scan.
                with self.opener.open(request, timeout=30) as response:
                    body = response.read()
                logger.debug(
                    "GitHub %s %s: received %d bytes in %.1fs",
                    method,
                    endpoint,
                    len(body),
                    time.monotonic() - started,
                )
                return json.loads(body) if body else None
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After", "")
                exhausted = error.headers.get("X-RateLimit-Remaining") == "0"
                error.close()
                retryable = status in {429, 500, 502, 503, 504}
                delay = 2**attempt
                if retry_after:
                    try:
                        delay = max(delay, int(retry_after))
                    except ValueError:
                        retryable = False
                if retryable and attempt + 1 < attempts and delay <= 30:
                    logger.warning(
                        "GitHub %s %s: HTTP %d; retrying in %ds (attempt %d/%d)",
                        method,
                        endpoint,
                        status,
                        delay,
                        attempt + 2,
                        attempts,
                    )
                    time.sleep(delay)
                    continue
                detail = ""
                if status in {403, 429} and exhausted:
                    detail = "; API rate limit exhausted"
                    if not self.token:
                        detail += "; set GH_TOKEN or GITHUB_TOKEN to authenticate"
                raise GitHubError(
                    f"GitHub {method} {endpoint}: HTTP {status}{detail}", status
                ) from None
            except (URLError, OSError, IncompleteRead) as error:
                if attempt + 1 < attempts:
                    delay = 2**attempt
                    logger.warning(
                        "GitHub %s %s: %s; retrying in %ds (attempt %d/%d)",
                        method,
                        endpoint,
                        str(error) or type(error).__name__,
                        delay,
                        attempt + 2,
                        attempts,
                    )
                    time.sleep(delay)
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

    def tags(
        self,
        repo: str,
        prefix: str = "",
        *,
        include: Callable[[str], bool] | None = None,
    ) -> dict[str, str]:
        # Git advertises peeled IDs alongside annotated tags, so discovery needs
        # no separate requests for tag objects. Rechecks still verify commit types.
        refs: dict[str, str] = {}
        for line in self._ls_remote(repo, prefix).splitlines():
            match = re.fullmatch(r"([0-9a-f]{40})\trefs/tags/(\S+)", line)
            if match is None:
                raise GitHubError(f"invalid git ls-remote response for {repo}")
            commit, tag = match.groups()
            if tag in refs and refs[tag] != commit:
                raise GitHubError(f"tag changed during discovery: {repo}/{tag}")
            refs[tag] = commit
        for tag in refs:
            if tag.endswith("^{}") and tag[:-3] not in refs:
                raise GitHubError(f"peeled tag without a reference: {repo}/{tag}")
        return {
            tag: refs.get(f"{tag}^{{}}", commit)
            for tag, commit in refs.items()
            if not tag.endswith("^{}")
            and tag.startswith(prefix)
            and (include is None or include(tag))
        }

    def _ls_remote(self, repo: str, prefix: str) -> str:
        command = ["git", "-c", "credential.helper=", "-c", "core.askPass="]
        env = os.environ | {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
        }
        if self.token:
            # Keep credentials out of argv, URLs, and progress messages.
            authorization = base64.b64encode(f"x-access-token:{self.token}".encode())
            env["ASTRAL_DEV_TOOLCHAIN_GIT_AUTHORIZATION"] = (
                f"Authorization: Basic {authorization.decode()}"
            )
            command.append(
                "--config-env=http.https://github.com/.extraheader=ASTRAL_DEV_TOOLCHAIN_GIT_AUTHORIZATION"
            )
        command.extend(["ls-remote", "--tags", f"https://github.com/{repo}.git"])
        if prefix:
            command.append(f"refs/tags/{prefix}*")
        logger.debug("git ls-remote: %s (prefix %r)", repo, prefix)
        started = time.monotonic()
        try:
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, check=True, timeout=30
            )
        except subprocess.TimeoutExpired as error:
            raise GitHubError(
                f"git ls-remote timed out after 30 seconds: {repo}"
            ) from error
        except subprocess.CalledProcessError as error:
            raise GitHubError(
                f"git ls-remote failed for {repo}: {error.stderr.strip()}"
            ) from error
        except OSError as error:
            raise GitHubError(
                f"cannot run git ls-remote for {repo}: {error}"
            ) from error
        logger.debug(
            "git ls-remote: %s completed in %.1fs", repo, time.monotonic() - started
        )
        return result.stdout

    def resolve_tag(self, repo: str, tag: str) -> str:
        ref = _Reference.model_validate(
            self.request("GET", f"/repos/{repo}/git/ref/tags/{quote(tag, safe='')}")
        )
        if ref.ref != f"refs/tags/{tag}":
            raise GitHubError(f"unexpected reference resolving {repo}/{tag}")
        return self._peel_tag(repo, tag, ref.object)

    def _peel_tag(self, repo: str, tag: str, target: _Object) -> str:
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
