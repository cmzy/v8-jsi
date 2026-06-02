"""Bump ``config.json`` when V8 ships a new build for the channel we track.

Mirrors ``scripts/update_version.ps1``. The script is intended to be driven
on a cron-like schedule by CI; it exits non-zero when a new major / minor
release appears (because that requires manual intervention to refresh the
in-tree patches), and silently exits 0 when nothing needs doing.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.request
from pathlib import Path

from . import env


CHROMIUM_DASH = (
    "https://chromiumdash.appspot.com/fetch_releases"
    "?channel={channel}&platform=Windows&num=1"
)


def _http_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def _fetch_v8_version_header(branch: str) -> str:
    url = (
        f"https://chromium.googlesource.com/v8/v8.git/+/"
        f"refs/heads/{branch}-lkgr/include/v8-version.h?format=text"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        encoded = resp.read()
    return base64.b64decode(encoded).decode("utf-8")


def _parse_macro(header_text: str, macro: str) -> str:
    m = re.search(rf"{macro}\s+(\d+)", header_text)
    if not m:
        raise SystemExit(f"could not find {macro} in v8-version.h")
    return m.group(1).strip()


def _bump_semver(version: str) -> str:
    parts = version.split(".")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def main(sources_path: Path, *, beta: bool = False, git_push: bool = False) -> int:
    channel = "Beta" if beta else "Stable"
    releases = _http_json(CHROMIUM_DASH.format(channel=channel))
    if not isinstance(releases, list) or not releases:
        raise SystemExit("chromiumdash returned no releases")
    milestone = releases[0]["milestone"]
    # chromiumdash exposes Chrome milestone; V8 major = milestone / 10 (rounded).
    latest_branch = f"{milestone // 10}.{milestone % 10}"
    print(f"Latest {channel} version is {latest_branch}", flush=True)

    config_path = sources_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    m = re.search(r"refs/branch-heads/(\d+\.\d+)", config["v8ref"])
    if not m:
        raise SystemExit(f"unexpected v8ref in config.json: {config['v8ref']!r}")
    built_branch = m.group(1).strip()
    print(f"Version currently being built is {built_branch}", flush=True)

    if built_branch != latest_branch:
        print(
            f"New {channel} version released, manual intervention required to "
            "bump the version (refresh patches and re-validate the build).",
            flush=True,
        )
        return 1

    header = _fetch_v8_version_header(latest_branch)
    build_number = _parse_macro(header, "V8_BUILD_NUMBER")
    print(f"Latest build number upstream is {build_number}", flush=True)
    print(f"Build number currently being built is {config['buildNumber']}", flush=True)

    if build_number == str(config["buildNumber"]):
        print("Latest build number is already being built. All good!", flush=True)
        return 0

    print(f"New {channel} build number released, attempting to bump it", flush=True)
    config["buildNumber"] = build_number
    config["version"] = _bump_semver(config["version"])
    config_path.write_text(
        json.dumps(config, indent=4) + "\n", encoding="utf-8"
    )

    if not git_push:
        print(
            "Git push not requested, we would be updating the version to "
            f"{config['version']} (new upstream build number {build_number})",
            flush=True,
        )
        return 0

    subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions@github.com"], check=True
    )
    subprocess.run(["git", "add", "config.json"], check=True, cwd=str(sources_path))
    subprocess.run(
        [
            "git", "commit", "-m",
            (
                f"Updating version to {config['version']} "
                f"(new upstream build number {build_number})"
            ),
        ],
        check=True,
        cwd=str(sources_path),
    )
    subprocess.run(["git", "push"], check=True, cwd=str(sources_path))
    return 0
