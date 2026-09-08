"""Read upstream tags through Git without a clone or a hosting-service API."""

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Collection

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


class Git:
    def tags(
        self,
        repo: str,
        prefix: str = "",
        *,
        include: Callable[[str], bool] | None = None,
    ) -> dict[str, str]:
        patterns = (f"refs/tags/{prefix}*",) if prefix else ()
        return {
            tag: commit
            for tag, commit in self._read_refs(repo, patterns).items()
            if tag.startswith(prefix) and (include is None or include(tag))
        }

    def resolve_tags(self, repo: str, tags: Collection[str]) -> dict[str, str]:
        """Read fresh tag IDs together, omitting tags that no longer exist."""
        if not tags:
            return {}
        patterns = [
            ref for tag in tags for ref in (f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
        ]
        refs = self._read_refs(repo, patterns)
        return {tag: refs[tag] for tag in tags if tag in refs}

    def _read_refs(self, repo: str, patterns: Collection[str]) -> dict[str, str]:
        refs: dict[str, str] = {}
        for line in self._ls_remote(repo, patterns).splitlines():
            match = re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64})\trefs/tags/(\S+)", line)
            if match is None:
                raise GitError(f"invalid git ls-remote response for {repo}")
            commit, tag = match.groups()
            if tag in refs and refs[tag] != commit:
                raise GitError(f"tag changed during discovery: {repo}/{tag}")
            refs[tag] = commit
        for tag in refs:
            if tag.endswith("^{}") and tag[:-3] not in refs:
                raise GitError(f"peeled tag without a reference: {repo}/{tag}")
        return {
            tag: refs.get(f"{tag}^{{}}", commit)
            for tag, commit in refs.items()
            if not tag.endswith("^{}")
        }

    def _ls_remote(self, repo: str, patterns: Collection[str]) -> str:
        # Honor Git's configured transports and credentials, without prompting.
        env = os.environ | {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
        }
        command = [
            "git",
            "ls-remote",
            "--tags",
            "--sort=v:refname",
            "--",
            repo,
            *patterns,
        ]
        logger.debug("git ls-remote: %s (refs %s)", repo, patterns or "all tags")
        started = time.monotonic()
        try:
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, check=True, timeout=30
            )
        except subprocess.TimeoutExpired as error:
            raise GitError(
                f"git ls-remote timed out after 30 seconds: {repo}"
            ) from error
        except subprocess.CalledProcessError as error:
            raise GitError(
                f"git ls-remote failed for {repo}: {error.stderr.strip()}"
            ) from error
        except OSError as error:
            raise GitError(f"cannot run git ls-remote for {repo}: {error}") from error
        logger.debug(
            "git ls-remote: %s completed in %.1fs", repo, time.monotonic() - started
        )
        return result.stdout
