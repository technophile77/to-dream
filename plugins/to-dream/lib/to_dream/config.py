"""User configuration (``$TO_DREAM_HOME/config.json``): every key optional, validated on write.

``DEFAULTS`` lists the known keys. :func:`load` merges the file over the defaults,
:func:`set_value` coerces and validates before an atomic write, so a hand-typed
``to-dream config set default_days 3`` stores the integer ``3``.

Example::

    >>> DEFAULTS["model"]
    'opus'
    >>> coerce("default_days", "3")
    3
"""

import json
import re
from typing import Any, Callable

from to_dream import state

DEFAULTS: dict[str, Any] = {
    "model": "opus",
    "analyst_model": "sonnet",
    "default_days": 7,
    "timeout_min": 60,
    "branch_template": "dream/{run_id}",
    "worktree_dir": "",  # "" = $TO_DREAM_HOME/worktrees
    "worktree_template": "{run_id}",
    "steering_path": "",  # "" = $TO_DREAM_HOME/steering.md if present, else the bundled steering
    "schedule_time": "03:30",
    "update_check": True,
    "rollup": True,  # fold an unreviewed run's window into the next dream and supersede it
    "permissions_review": False,  # opt in: suggest allow/ask permission rules from permission friction
}

TEMPLATE_FIELDS = ("run_id", "project", "yy", "mm", "dd", "hhmm")
_FIELD = re.compile(r"\{(\w+)\}")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_BAD_REF = re.compile(r"[\s~^:?*\[\\]|\.\.|@\{|//|\.lock(?:/|$)|^[/.-]|[/.]$")
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})
_SAMPLE_FIELDS = {"run_id": "proj-261102-1830", "project": "proj", "yy": "26", "mm": "11", "dd": "02", "hhmm": "1830"}


class ConfigError(RuntimeError):
    """Raised for an unknown key, an invalid value, or a ``config.json`` that cannot be parsed."""


def _string(key: str, value: Any, allow_empty: bool) -> str:
    """Return ``value`` as a stripped string; reject non-strings (and empty ones unless allowed)."""
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be text, got {value!r}")
    text = value.strip()
    if not text and not allow_empty:
        raise ConfigError(f"{key} must not be empty")
    return text


def _integer(key: str, value: Any) -> int:
    """Return ``value`` as a positive integer (accepts digit strings and integral floats)."""
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a whole number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a whole number, got {value!r}") from None
    if number != int(number) or number < 1:
        raise ConfigError(f"{key} must be a whole number of at least 1, got {value!r}")
    return int(number)


def _positive_number(key: str, value: Any) -> int | float:
    """Return ``value`` as a positive number, as an int when it is integral (``60.0`` -> ``60``)."""
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a positive number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a positive number, got {value!r}") from None
    if not number > 0 or number == float("inf"):
        raise ConfigError(f"{key} must be a positive number, got {value!r}")
    return int(number) if number == int(number) else number


def _boolean(key: str, value: Any) -> bool:
    """Return ``value`` as a bool (accepts true/false/1/0/yes/no/on/off in any case)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{key} must be true or false, got {value!r}")


def render_name(template: str, values: dict[str, str], key: str = "template") -> str:
    """Fill ``{field}`` placeholders (see ``TEMPLATE_FIELDS``); other braces are kept literally.

    >>> render_name("dream/{run_id}", {"run_id": "p-261102-1830"})
    'dream/p-261102-1830'

    Raises:
        ConfigError: the template names a field that is not in ``TEMPLATE_FIELDS``.
    """
    unknown = sorted(set(_FIELD.findall(template)) - set(TEMPLATE_FIELDS))
    if unknown:
        raise ConfigError(
            f"{key} uses unknown placeholder(s) {', '.join('{' + n + '}' for n in unknown)}; "
            f"allowed: {', '.join('{' + n + '}' for n in TEMPLATE_FIELDS)}"
        )
    return _FIELD.sub(lambda m: values[m.group(1)], template)


def _branch_template(key: str, value: Any) -> str:
    """Validate a branch template: known placeholders and a plausible git ref name once rendered."""
    template = _string(key, value, allow_empty=False)
    sample = render_name(template, _SAMPLE_FIELDS, key)
    if _BAD_REF.search(sample):
        raise ConfigError(f"{key} {template!r} does not render to a valid git branch name (e.g. {sample!r})")
    return template


def _worktree_template(key: str, value: Any) -> str:
    """Validate a worktree directory-name template (known placeholders only)."""
    template = _string(key, value, allow_empty=False)
    render_name(template, _SAMPLE_FIELDS, key)
    return template


def _schedule_time(key: str, value: Any) -> str:
    """Validate a 24-hour ``HH:MM`` time."""
    text = _string(key, value, allow_empty=False)
    if not _TIME.match(text):
        raise ConfigError(f"{key} must be HH:MM (24-hour), got {value!r}")
    return text


_COERCERS: dict[str, Callable[[str, Any], Any]] = {
    "model": lambda k, v: _string(k, v, False),
    "analyst_model": lambda k, v: _string(k, v, False),
    "default_days": _integer,
    "timeout_min": _positive_number,
    "branch_template": _branch_template,
    "worktree_dir": lambda k, v: _string(k, v, True),
    "worktree_template": _worktree_template,
    "steering_path": lambda k, v: _string(k, v, True),
    "schedule_time": _schedule_time,
    "update_check": _boolean,
    "rollup": _boolean,
    "permissions_review": _boolean,
}


def coerce(key: str, value: Any) -> Any:
    """Validate ``value`` for ``key`` and return it in its stored type.

    Strings from the command line are converted (``"3"`` -> ``3``, ``"off"`` -> ``False``).

    >>> coerce("update_check", "off"), coerce("timeout_min", "45.0"), coerce("schedule_time", "04:15")
    (False, 45, '04:15')

    Raises:
        ConfigError: unknown key, or a value that does not fit the key.
    """
    if key not in _COERCERS:
        raise ConfigError(f"unknown config key {key!r}; known keys: {', '.join(sorted(DEFAULTS))}")
    return _COERCERS[key](key, value)


def _read_file() -> dict[str, Any]:
    """Return the raw ``config.json`` object ({} when absent).

    Raises:
        ConfigError: the file is not a JSON object.
    """
    path = state.config_path()
    try:
        data = state.read_json_file(path)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}); fix or delete it") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object; fix or delete it")
    return data


def load() -> dict[str, Any]:
    """Return the effective configuration: ``DEFAULTS`` overlaid with the valid keys of ``config.json``.

    Unknown keys in the file are ignored (forward compatibility).

    Example::

        load()["default_days"]  # 7 unless config.json says otherwise

    Raises:
        ConfigError: the file is unreadable JSON or a known key holds an invalid value.
    """
    stored = _read_file()
    merged = dict(DEFAULTS)
    for key, value in stored.items():
        if key in _COERCERS:
            merged[key] = coerce(key, value)
    return merged


def get(key: str) -> Any:
    """Return the effective value of one key.

    Raises:
        ConfigError: unknown key (or any error :func:`load` raises).
    """
    if key not in DEFAULTS:
        raise ConfigError(f"unknown config key {key!r}; known keys: {', '.join(sorted(DEFAULTS))}")
    return load()[key]


def set_value(key: str, value: Any) -> Any:
    """Validate, coerce and persist one key (atomic write); return the stored value.

    Example::

        set_value("default_days", "3")  # -> 3
        set_value("worktree_dir", "")   # back to the default location

    Raises:
        ConfigError: unknown key or invalid value (nothing is written).
    """
    coerced = coerce(key, value)
    stored = _read_file()
    stored[key] = coerced
    state.write_json_atomic(stored, state.config_path(), prefix=".config-")
    return coerced


def format_value(value: Any) -> str:
    """Render a value for the command line: text as is, everything else as JSON (``true``, ``7``)."""
    return value if isinstance(value, str) else json.dumps(value)
