# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""The config file: which tree, which build dirs, when, and the disk floor.

It lives at `$XDG_CONFIG_HOME/fx-updater/config.toml` (falling back to
`~/.config/fx-updater/config.toml`). `install` writes it; `run` reads it.
A flag given on the command line always wins over the file.

    fuchsia_dir = "/path/to/fuchsia"
    build_dirs = ["core.x64"]       # optional; default: the tree's .fx-build-dir
    schedule = "*-*-* 05:30:00"     # systemd OnCalendar syntax
    min_free_gb = 100.0
    prom_dir = ""                   # optional; "" writes no .prom file
    post_build_hook = ""            # optional; a command, split like a shell would

`install` regenerates the file from its values, so comments added by hand
do not survive the next install.

Unknown keys are an error rather than ignored: a misspelled `min_free_gb`
silently falling back to the default is exactly the kind of mistake nobody
notices on an unattended host.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shlex
import tempfile
import tomllib

DEFAULT_SCHEDULE = "*-*-* 05:30:00"
DEFAULT_MIN_FREE_GB = 100.0


class ConfigError(Exception):
    """The config file is missing, unparseable, or has an invalid value."""


@dataclasses.dataclass
class Config:
    """The settings `run` and `install` share."""

    fuchsia_dir: pathlib.Path
    build_dirs: list[str] = dataclasses.field(default_factory=list)
    schedule: str = DEFAULT_SCHEDULE
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    prom_dir: pathlib.Path | None = None
    # Run after every outcome, under the lock; see docs/contract.md.
    post_build_hook: str = ""


def default_path() -> pathlib.Path:
    """`$XDG_CONFIG_HOME/fx-updater/config.toml`, else `~/.config/...`."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return base / "fx-updater" / "config.toml"


def has_control_char(text: str) -> bool:
    """True if `text` holds a newline, NUL or other control character.

    Every value here ends up in a systemd unit file, one directive per
    line: a newline in a path would end the line and start a new directive.
    """
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def check_build_dir(name: str) -> None:
    """Raise ConfigError unless `name` is a plain dir name under out/."""
    # A build dir is a name under out/, never a path; "../x" or "a/b"
    # would point fx at something other than what the user meant. No
    # whitespace either: the hook gets the names space-separated.
    if (
        not name
        or "/" in name
        or name in (".", "..")
        or has_control_char(name)
        or any(c.isspace() for c in name)
    ):
        raise ConfigError(f"build dir {name!r} is not a dir name")


def validate(cfg: Config) -> Config:
    """Raise ConfigError for any value `run` could not use; else return cfg."""
    if not cfg.fuchsia_dir.is_absolute():
        raise ConfigError(f"fuchsia_dir must be absolute, not {cfg.fuchsia_dir}")
    for name in cfg.build_dirs:
        check_build_dir(name)
    if not cfg.schedule.strip():
        raise ConfigError("schedule must not be empty")
    for key, value in (
        ("fuchsia_dir", str(cfg.fuchsia_dir)),
        ("schedule", cfg.schedule),
        ("prom_dir", str(cfg.prom_dir or "")),
        ("post_build_hook", cfg.post_build_hook),
    ):
        if has_control_char(value):
            raise ConfigError(f"{key} must not contain control characters")
    try:
        shlex.split(cfg.post_build_hook)
    except ValueError as e:
        raise ConfigError(f"post_build_hook: {e}") from e
    if cfg.min_free_gb < 0:
        raise ConfigError(f"min_free_gb must be 0 or more, not {cfg.min_free_gb}")
    if cfg.prom_dir is not None and not cfg.prom_dir.is_absolute():
        raise ConfigError(f"prom_dir must be absolute, not {cfg.prom_dir}")
    return cfg


_TYPES = {
    "fuchsia_dir": str,
    "build_dirs": list,
    "schedule": str,
    "min_free_gb": (int, float),
    "prom_dir": str,
    "post_build_hook": str,
}


def load(path: pathlib.Path) -> Config:
    """Parse and validate the config file at `path`."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"no config file at {path}") from e
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    unknown = sorted(set(data) - set(_TYPES))
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(unknown)}")
    for key, value in data.items():
        # bool is an int subclass; `min_free_gb = true` is still a mistake.
        if not isinstance(value, _TYPES[key]) or isinstance(value, bool):
            raise ConfigError(f"{path}: {key} has the wrong type")
    if "fuchsia_dir" not in data:
        raise ConfigError(f"{path}: fuchsia_dir is required")
    if not all(isinstance(b, str) for b in data.get("build_dirs", [])):
        raise ConfigError(f"{path}: build_dirs must be a list of strings")
    prom = data.get("prom_dir", "")
    return validate(
        Config(
            fuchsia_dir=pathlib.Path(data["fuchsia_dir"]),
            build_dirs=list(data.get("build_dirs", [])),
            schedule=data.get("schedule", DEFAULT_SCHEDULE),
            min_free_gb=float(data.get("min_free_gb", DEFAULT_MIN_FREE_GB)),
            prom_dir=pathlib.Path(prom) if prom else None,
            post_build_hook=data.get("post_build_hook", ""),
        )
    )


def _toml_str(value: str) -> str:
    # A JSON string with ASCII escapes is a valid TOML basic string.
    return json.dumps(value, ensure_ascii=True)


def render(cfg: Config) -> str:
    """The TOML text for `cfg`, keys in a fixed order."""
    dirs = ", ".join(_toml_str(b) for b in cfg.build_dirs)
    return (
        "# fx-updater config. `fx-updater install` rewrites this whole file:\n"
        "# edited values are kept, comments are not.\n"
        f"fuchsia_dir = {_toml_str(str(cfg.fuchsia_dir))}\n"
        f"build_dirs = [{dirs}]\n"
        f"schedule = {_toml_str(cfg.schedule)}\n"
        f"min_free_gb = {float(cfg.min_free_gb)!r}\n"
        f"prom_dir = {_toml_str(str(cfg.prom_dir) if cfg.prom_dir else '')}\n"
        f"post_build_hook = {_toml_str(cfg.post_build_hook)}\n"
    )


def write(path: pathlib.Path, cfg: Config) -> None:
    """Validate `cfg` and replace `path` with it atomically."""
    validate(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render(cfg))
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
