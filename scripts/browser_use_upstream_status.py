#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

SEMVER_TAG_MATCH = "[0-9]*"


class RefStatus(BaseModel):
    ref: str
    sha: str
    ahead: int = Field(ge=0)
    behind: int = Field(ge=0)
    latest_tag: str | None = None


class UpstreamStatus(BaseModel):
    repo_root: str
    current_sha: str
    current_tag: str | None = None
    latest_release_tag: str | None = None
    on_latest_release: bool
    upstream_stable: RefStatus
    upstream_main: RefStatus


def run_git(repo_root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def try_git(repo_root: Path, *args: str) -> str | None:
    try:
        value = run_git(repo_root, *args)
    except subprocess.CalledProcessError:
        return None
    return value or None


def build_ref_status(repo_root: Path, ref: str) -> RefStatus:
    sha = run_git(repo_root, "rev-parse", ref)
    ahead_str, behind_str = run_git(
        repo_root,
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...{ref}",
    ).split()
    latest_tag = try_git(repo_root, "describe", "--tags", "--abbrev=0", "--match", SEMVER_TAG_MATCH, ref)
    return RefStatus(
        ref=ref,
        sha=sha,
        ahead=int(ahead_str),
        behind=int(behind_str),
        latest_tag=latest_tag,
    )


def build_status(repo_root: Path) -> UpstreamStatus:
    current_sha = run_git(repo_root, "rev-parse", "HEAD")
    current_tag = try_git(repo_root, "describe", "--tags", "--abbrev=0", "--match", SEMVER_TAG_MATCH, "HEAD")
    upstream_stable = build_ref_status(repo_root, "upstream/stable")
    upstream_main = build_ref_status(repo_root, "upstream/main")
    latest_release_tag = upstream_stable.latest_tag

    return UpstreamStatus(
        repo_root=str(repo_root),
        current_sha=current_sha,
        current_tag=current_tag,
        latest_release_tag=latest_release_tag,
        on_latest_release=current_tag is not None and current_tag == latest_release_tag and upstream_stable.behind == 0,
        upstream_stable=upstream_stable,
        upstream_main=upstream_main,
    )


def format_summary(status: UpstreamStatus) -> str:
    lines = [
        f"Repository: {status.repo_root}",
        f"Current HEAD: {status.current_sha}",
        f"Current Browser Use release tag: {status.current_tag or 'unknown'}",
        f"Latest upstream release tag: {status.latest_release_tag or 'unknown'}",
        f"On latest upstream release: {'yes' if status.on_latest_release else 'no'}",
        "",
        "Upstream stable:",
        f"  ref: {status.upstream_stable.ref}",
        f"  sha: {status.upstream_stable.sha}",
        f"  ahead: {status.upstream_stable.ahead}",
        f"  behind: {status.upstream_stable.behind}",
        f"  tag: {status.upstream_stable.latest_tag or 'unknown'}",
        "",
        "Upstream main:",
        f"  ref: {status.upstream_main.ref}",
        f"  sha: {status.upstream_main.sha}",
        f"  ahead: {status.upstream_main.ahead}",
        f"  behind: {status.upstream_main.behind}",
        f"  tag: {status.upstream_main.latest_tag or 'unknown'}",
    ]
    return "\n".join(lines)


def write_github_outputs(output_path: Path, status: UpstreamStatus) -> None:
    outputs = {
        "current_sha": status.current_sha,
        "current_tag": status.current_tag or "",
        "latest_release_tag": status.latest_release_tag or "",
        "on_latest_release": str(status.on_latest_release).lower(),
        "ahead_upstream_stable": str(status.upstream_stable.ahead),
        "behind_upstream_stable": str(status.upstream_stable.behind),
        "ahead_upstream_main": str(status.upstream_main.ahead),
        "behind_upstream_main": str(status.upstream_main.behind),
        "upstream_stable_sha": status.upstream_stable.sha,
        "upstream_main_sha": status.upstream_main.sha,
    }
    with output_path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report how this fork tracks upstream browser-use.")
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="Path to the repository root.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch upstream main, stable, and tags before computing status.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a text summary.",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Append computed values to the GitHub Actions output file.",
    )
    parser.add_argument(
        "--fail-if-behind",
        choices=["stable", "main"],
        help="Exit non-zero when HEAD is behind the selected upstream ref.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()

    if args.fetch:
        run_git(repo_root, "fetch", "upstream", "main", "stable", "--tags", "--prune")

    status = build_status(repo_root)

    if args.github_output is not None:
        write_github_outputs(args.github_output, status)

    if args.json:
        print(json.dumps(status.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        print(format_summary(status))

    if args.fail_if_behind == "stable" and status.upstream_stable.behind > 0:
        return 1
    if args.fail_if_behind == "main" and status.upstream_main.behind > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
