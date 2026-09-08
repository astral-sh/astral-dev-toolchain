"""Discover upstream updates and keep their observations in build.toml."""

import logging
import os
import re
import tempfile
import tomllib
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit
from astral_dev_toolchain_core import Tool, ToolObservation
from packaging.version import Version
from tomlkit.items import InlineTable, Table

from ._github import GitHub, GitHubError

COOLDOWN = timedelta(days=7)
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")

logger = logging.getLogger(__name__)


def atomic_write(path: Path, content: str) -> None:
    """Replace a whole definition only after its new contents are written."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(path.stat().st_mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def repository(url: str) -> str:
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or not all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", p) for p in parts)
    ):
        raise ValueError(f"updating requires an HTTPS GitHub repository: {url}")
    return "/".join(parts).lower()


def tag_version(tag: str, prefix: str = "") -> Version | None:
    if not tag.startswith(prefix):
        return None
    value = tag[len(prefix) :].removeprefix("v")
    return Version(value) if _VERSION.fullmatch(value) else None


@dataclass(frozen=True)
class Definition:
    path: Path
    text: str
    tool: Tool

    @classmethod
    def parse(cls, path: Path, text: str) -> Definition:
        return cls(path, text, Tool.model_validate(tomllib.loads(text)))


def definitions(directory: Path, selected: Collection[str] = ()) -> list[Definition]:
    found: dict[str, Definition] = {}
    for path in sorted(directory.glob("*/build.toml")):
        definition = Definition.parse(path, path.read_text(encoding="utf-8"))
        name = definition.tool.tool.name
        if name in found:
            raise ValueError(f"duplicate tool name: {name}")
        found[name] = definition
    if unknown := set(selected) - found.keys():
        raise ValueError(f"unknown tools: {', '.join(sorted(unknown))}")
    return [found[name] for name in sorted(set(selected) or found)]


@dataclass(frozen=True)
class Proposal:
    old_version: Version
    version: Version
    tag: str
    commit: str
    first_seen: datetime

    @property
    def eligible_at(self) -> datetime:
        return self.first_seen + COOLDOWN


@dataclass
class Discovery:
    definition: Definition
    repo: str
    observed: dict[str, ToolObservation]
    proposal: Proposal | None

    @property
    def name(self) -> str:
        return self.definition.tool.tool.name

    def render(self, *, keep: Definition | None = None) -> str:
        """Apply a proposal, or keep an existing draft's pin while observing tags."""
        base = keep or self.definition
        document = tomlkit.parse(base.text)
        update = document["update"]
        assert isinstance(update, (Table, InlineTable))
        assert base.tool.update is not None
        floor = (
            self.proposal.version
            if self.proposal is not None and keep is None
            else tag_version(base.tool.source.tag, base.tool.update.tag_prefix)
        )
        observed = {
            tag: observation
            for tag, observation in self.observed.items()
            if floor is None
            or (version := tag_version(tag, base.tool.update.tag_prefix)) is not None
            and version >= floor
        }
        if base.tool.update.observed != observed:
            if observed:
                update["observed"] = {
                    tag: observation.model_dump(by_alias=True)
                    for tag, observation in sorted(observed.items())
                }
            else:
                update.pop("observed", None)
        if self.proposal is not None and keep is None:
            proposal = self.proposal
            explicit = base.tool.tool.version
            if explicit is not None and Version(explicit) != proposal.old_version:
                raise ValueError(
                    f"{self.name}: explicit tool.version differs from the pinned tag; update manually"
                )
            source, spec = document["source"], document["tool"]
            assert isinstance(source, (Table, InlineTable))
            assert isinstance(spec, (Table, InlineTable))
            source["tag"] = proposal.tag
            source["commit"] = proposal.commit
            if explicit is not None:
                spec["version"] = str(proposal.version)
            spec.pop("rebuild", None)
        rendered = tomlkit.dumps(document)
        Tool.model_validate(tomllib.loads(rendered))
        return rendered


@dataclass
class Scan:
    discoveries: list[Discovery] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _check_pin(github: GitHub, repo: str, tool: Tool) -> None:
    try:
        commit = github.resolve_tag(repo, tool.source.tag)
    except GitHubError as error:
        if error.status == 404:
            raise ValueError(
                f"{tool.tool.name}: pinned tag {tool.source.tag} is missing; review manually"
            ) from error
        raise
    if commit != tool.source.commit:
        raise ValueError(
            f"{tool.tool.name}: pinned tag {tool.source.tag} moved from "
            f"{tool.source.commit} to {commit}; review manually"
        )


def _version_floor(tool: Tool) -> tuple[str, Version]:
    assert tool.update is not None
    prefix = tool.update.tag_prefix
    current = tag_version(tool.source.tag, prefix)
    if current is None:
        raise ValueError(f"{tool.tool.name}: pinned tag is not a stable version")
    return prefix, current


def _observe_tags(
    tool: Tool,
    repo: str,
    tags: Mapping[str, str],
    prior: Tool,
    observed_at: datetime,
) -> dict[str, ToolObservation]:
    prefix, current = _version_floor(tool)
    old = (
        prior.update.observed
        if prior.update is not None
        and prior.update.tag_prefix == prefix
        and repository(prior.source.repo) == repo
        else {}
    )
    observed = {}
    for tag, commit in tags.items():
        version = tag_version(tag, prefix)
        if version is None or version < current:
            continue
        observation = old.get(tag)
        if version == current and observation is None:
            continue
        if observation is not None and observation.first_seen > observed_at:
            raise ValueError(f"{repo}/{tag}: first-seen timestamp is in the future")
        if observation is None or observation.commit != commit:
            observation = ToolObservation(commit=commit, first_seen=observed_at)
        observed[tag] = observation
    return observed


def _select_proposal(
    tool: Tool, observed: Mapping[str, ToolObservation], observed_at: datetime
) -> tuple[Proposal | None, list[str]]:
    prefix, current = _version_floor(tool)
    candidates, messages = [], []
    for tag, observation in observed.items():
        version = tag_version(tag, prefix)
        assert version is not None
        if version == current:
            continue
        eligible_at = observation.first_seen + COOLDOWN
        if eligible_at > observed_at:
            messages.append(
                f"{tool.tool.name}: {tag} cooling down until {eligible_at.isoformat()}"
            )
        else:
            candidates.append((version, tag, observation))
    if not candidates:
        messages.append(f"{tool.tool.name}: no eligible update")
        return None, messages
    version, tag, observation = max(candidates, key=lambda c: (c[0], c[1]))
    if len({o.commit for v, _, o in candidates if v == version}) != 1:
        raise ValueError(f"{tool.tool.name}: ambiguous tags for {version}")
    return Proposal(
        current, version, tag, observation.commit, observation.first_seen
    ), messages


def _scan_tool(
    definition: Definition,
    repo: str,
    tags: Mapping[str, str],
    github: GitHub,
    prior: Tool,
    observed_at: datetime,
) -> tuple[Discovery, list[str]]:
    tool = definition.tool
    _check_pin(github, repo, tool)
    if tags.get(tool.source.tag) != tool.source.commit:
        raise ValueError(f"{repo}: pinned tag changed during discovery")
    observed = _observe_tags(tool, repo, tags, prior, observed_at)
    proposal, messages = _select_proposal(tool, observed, observed_at)
    discovery = Discovery(definition, repo, observed, proposal)
    discovery.render()
    return discovery, messages


def _scan_repository(
    repo: str,
    tools: list[Definition],
    github: GitHub,
    now: datetime | None,
    previous: Mapping[str, Tool],
) -> Scan:
    minimums = [_version_floor(definition.tool) for definition in tools]

    def relevant(tag: str) -> bool:
        return any(
            (version := tag_version(tag, prefix)) is not None and version >= current
            for prefix, current in minimums
        )

    prefix = os.path.commonprefix([prefix for prefix, _ in minimums])
    tags = github.tags(repo, prefix, include=relevant)
    logger.debug("%s: fetched %d relevant tags", repo, len(tags))
    observed_at = now or datetime.now(UTC)
    # Keep results local until every tool succeeds: incomplete scans must not
    # discard observations of missing tags or update only part of a repository.
    result = Scan()
    for definition in tools:
        prior = previous.get(definition.tool.tool.name, definition.tool)
        discovery, messages = _scan_tool(
            definition, repo, tags, github, prior, observed_at
        )
        result.discoveries.append(discovery)
        result.messages.extend(messages)
    return result


def scan(
    selected: list[Definition],
    github: GitHub,
    now: datetime | None = None,
    *,
    previous: Mapping[str, Tool] | None = None,
) -> Scan:
    result = Scan()
    groups: dict[str, list[Definition]] = defaultdict(list)
    for definition in selected:
        tool = definition.tool
        if tool.update is None or not tool.update.github:
            result.messages.append(f"{tool.tool.name}: automatic updates disabled")
            continue
        try:
            groups[repository(tool.source.repo)].append(definition)
        except ValueError as error:
            result.errors.append(str(error))

    for index, (repo, tools) in enumerate(groups.items(), 1):
        logger.info("[%d/%d] checking upstream tags for %s", index, len(groups), repo)
        try:
            scanned = _scan_repository(repo, tools, github, now, previous or {})
            result.discoveries.extend(scanned.discoveries)
            result.messages.extend(scanned.messages)
        except (GitHubError, ValueError) as error:
            logger.warning("%s: discovery failed: %s", repo, error)
            result.errors.append(f"{repo}: {error}")
    return result


def recheck(discovery: Discovery, github: GitHub, now: datetime | None = None) -> None:
    """Recheck a proposal, recording moved or deleted candidates in its manifest."""
    proposal = discovery.proposal
    if proposal is None:
        return
    _check_pin(github, discovery.repo, discovery.definition.tool)
    try:
        commit = github.resolve_tag(discovery.repo, proposal.tag)
    except GitHubError as error:
        if error.status == 404:
            discovery.observed.pop(proposal.tag, None)
            discovery.proposal = None
        raise
    if commit != proposal.commit:
        discovery.observed[proposal.tag] = ToolObservation(
            commit=commit, first_seen=now or datetime.now(UTC)
        )
        discovery.proposal = None
        raise ValueError(f"{discovery.name}: candidate tag moved; cooldown restarted")


def update_local(
    directory: Path,
    selected: Collection[str] = (),
    *,
    dry_run: bool = False,
    github: GitHub | None = None,
    now: datetime | None = None,
) -> Scan:
    github = github or GitHub()
    result = scan(definitions(directory, selected), github, now)
    for discovery in result.discoveries:
        try:
            try:
                recheck(discovery, github, now)
            except (GitHubError, ValueError) as error:
                # A changed/deleted candidate still needs its observation saved;
                # other failures must not apply the unverified proposal.
                discovery.proposal = None
                result.errors.append(str(error))
            content = discovery.render()
            if content != discovery.definition.text:
                if not dry_run:
                    if (
                        discovery.definition.path.read_text(encoding="utf-8")
                        != discovery.definition.text
                    ):
                        raise ValueError(
                            f"{discovery.name}: definition changed during update"
                        )
                    atomic_write(discovery.definition.path, content)
                action = "would update" if dry_run else "updated"
                proposal = discovery.proposal
                detail = (
                    f"{proposal.old_version} -> {proposal.version} ({proposal.commit})"
                    if proposal
                    else "observations in build.toml"
                )
                result.messages.append(f"{discovery.name}: {action} {detail}")
        except (ValueError, OSError) as error:
            result.errors.append(str(error))
    return result
