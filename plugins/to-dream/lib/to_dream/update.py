"""``to-dream check-update``: tell the user when a newer release is published.

Reads plugin.json from the repository's default branch over HTTPS (5 s timeout), caches the
answer for 24 h in ``$TO_DREAM_HOME/update-check.json`` and never fails a command: any
problem is reported as ``unknown``.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, state, version

TIMEOUT_SECONDS = 5
CACHE_TTL = timedelta(hours=24)
FAILURE_TTL = timedelta(hours=1)  # a failed lookup is retried sooner, but not on every command
CACHE_FILENAME = "update-check.json"
OPT_OUT_ENV = "TO_DREAM_NO_UPDATE_CHECK"
URL_OVERRIDE_ENV = "TO_DREAM_UPDATE_URL"  # lets tests point at a local server
MANIFEST_PATH = "plugins/to-dream/.claude-plugin/plugin.json"
MAX_BYTES = 256 * 1024
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")

Fetcher = Callable[[str, float], str]


def parse_semver(text: str) -> Optional[tuple]:
    """Parse ``MAJOR.MINOR.PATCH[-pre][+build]`` (optional leading ``v``); None if invalid."""
    match = _SEMVER.match(text.strip()) if isinstance(text, str) else None
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), pre.split(".") if pre else []


def _compare_identifiers(a: list[str], b: list[str]) -> int:
    """Compare pre-release identifier lists per semver 2.0 section 11."""
    if not a and not b:
        return 0
    if not a:
        return 1  # a release outranks any of its pre-releases
    if not b:
        return -1
    for left, right in zip(a, b):
        if left == right:
            continue
        left_num, right_num = left.isdigit(), right.isdigit()
        if left_num and right_num:
            return -1 if int(left) < int(right) else 1
        if left_num != right_num:
            return -1 if left_num else 1  # numeric identifiers sort before alphanumeric ones
        return -1 if left < right else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 as ``a`` is older than, equal to, or newer than ``b`` (semver).

    Raises:
        ValueError: if either string is not a valid semantic version.

    Example:
        >>> compare_versions("0.2.0", "0.10.0")
        -1
        >>> compare_versions("1.0.0-rc.1", "1.0.0")
        -1
    """
    left, right = parse_semver(a), parse_semver(b)
    if left is None or right is None:
        raise ValueError("not a semantic version: %r" % (a if left is None else b))
    if left[:3] != right[:3]:
        return -1 if left[:3] < right[:3] else 1
    return _compare_identifiers(left[3], right[3])


def manifest_url(repository: str) -> str:
    """Derive the raw plugin.json URL on the default branch from a GitHub repository URL.

    Example:
        >>> manifest_url("https://github.com/technophile77/to-dream")
        'https://raw.githubusercontent.com/technophile77/to-dream/HEAD/plugins/to-dream/.claude-plugin/plugin.json'
    """
    override = os.environ.get(URL_OVERRIDE_ENV)
    if override:
        return override
    match = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repository.strip())
    if not match:
        raise ValueError("unsupported repository URL: %r" % repository)
    return "https://raw.githubusercontent.com/%s/%s/HEAD/%s" % (match.group(1), match.group(2), MANIFEST_PATH)


def changelog_url(repository: str) -> str:
    """Return the browsable CHANGELOG.md URL for ``repository``."""
    return repository.rstrip("/") + "/blob/HEAD/CHANGELOG.md"


def http_fetch(url: str, timeout: float) -> str:
    """GET ``url`` and return the body text (at most 256 KiB). Raises on any network error."""
    request = urllib.request.Request(url, headers={"User-Agent": "to-dream-update-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        return response.read(MAX_BYTES).decode("utf-8")


def fetch_latest_version(url: str, fetch: Fetcher = http_fetch) -> str:
    """Download the manifest at ``url`` and return its validated ``version``.

    Raises:
        Exception: on network failure, bad JSON, or a missing/invalid version string.
    """
    data = json.loads(fetch(url, TIMEOUT_SECONDS))
    latest = data["version"]
    if parse_semver(latest) is None:
        raise ValueError("invalid version in manifest")
    return latest


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(text: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _means_off(value: Any) -> bool:
    """True when a config value means 'off': JSON false, 0, or the strings false/0/no/off."""
    if isinstance(value, str):
        return value.strip().lower() in ("false", "0", "no", "off")
    return value is False or (isinstance(value, int) and value == 0)


def check_disabled() -> bool:
    """True when the user opted out via ``TO_DREAM_NO_UPDATE_CHECK=1`` or ``update_check: false``."""
    if os.environ.get(OPT_OUT_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        value = config.get("update_check")
    except Exception:
        return False
    return value is not None and _means_off(value)


def _cache_path() -> Path:
    return Path(state.home()) / CACHE_FILENAME


def _read_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(entry: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _record_notice(info: dict) -> None:
    """Mirror the result into state.json's ``update_notice`` (set when newer, cleared when not)."""
    def mutate(current: dict) -> dict:
        if info["update_available"]:
            current["update_notice"] = {"current": info["current"], "latest": info["latest"],
                                        "checked_at": info["checked_at"]}
        else:
            current.pop("update_notice", None)
        return current
    try:
        state.update_state(mutate)
    except Exception:
        pass


def check_update(force: bool = False, fetch: Fetcher = http_fetch,
                 now: Optional[Callable[[], datetime]] = None) -> dict:
    """Compare the running version with the latest published one; never raises.

    Returns ``{current, latest, update_available, checked_at, changelog_url, status}`` where
    ``status`` is ``"ok"``, ``"unknown"`` (offline or any error; ``latest`` is None) or
    ``"disabled"`` (opted out; no network access is made). The result is cached for 24 h
    (1 h for failures) unless ``force`` is true. A definite answer also updates the
    ``update_notice`` key in state.json.

    Example:
        >>> info = check_update(force=True)
        >>> info["update_available"], info["status"]
        (False, 'ok')
    """
    clock = now or _now
    current = version.current_version()
    repository = version.repository_url()
    info = {"current": current, "latest": None, "update_available": False,
            "checked_at": None, "changelog_url": changelog_url(repository), "status": "unknown"}
    try:
        if check_disabled():
            info["status"] = "disabled"
            return info
        cached = _read_cache()
        cached_at = _parse_time(cached.get("checked_at"))
        fresh_for = CACHE_TTL if cached.get("latest") else FAILURE_TTL
        if (not force and cached_at and cached.get("current") == current
                and clock() - cached_at < fresh_for):
            latest, checked_at = cached.get("latest"), cached["checked_at"]
        else:
            checked_at = clock().strftime(TIME_FORMAT)
            try:
                latest = fetch_latest_version(manifest_url(repository), fetch)
            except Exception:
                latest = None
            _write_cache({"current": current, "latest": latest, "checked_at": checked_at})
        info["checked_at"] = checked_at
        if latest:
            info["latest"] = latest
            info["status"] = "ok"
            info["update_available"] = compare_versions(latest, current) > 0
            _record_notice(info)
    except Exception:
        info.update(latest=None, update_available=False, status="unknown")
    return info


def reminder_line(info: dict) -> str:
    """One-line update reminder for ``info`` from :func:`check_update` ('' when up to date)."""
    if not info.get("update_available"):
        return ""
    return ("to-dream %s is out (you have %s). Update: `/plugin marketplace update to-dream`, then "
            "`/reload-plugins`. To stop needing this, turn on auto-update under `/plugin` -> "
            "Marketplaces -> to-dream." % (info["latest"], info["current"]))


def _handle(args) -> int:
    info = check_update(force=args.force)
    if args.json:
        print(json.dumps(info, indent=2))
    elif info["status"] == "disabled":
        print("to-dream %s (update check disabled)" % info["current"])
    elif info["status"] == "unknown":
        print("to-dream %s (latest version unknown: offline or lookup failed)" % info["current"])
    elif info["update_available"]:
        print(reminder_line(info))
        print("Changes: %s" % info["changelog_url"])
    else:
        print("to-dream %s is up to date" % info["current"])
    return 0


def add_parser(subparsers) -> None:
    """Register the ``check-update`` subcommand."""
    parser = subparsers.add_parser("check-update", help="check whether a newer to-dream release is out")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--force", action="store_true", help="ignore the 24 h cache")
    parser.set_defaults(func=_handle)
