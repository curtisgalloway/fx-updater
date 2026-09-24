# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""The config file: round trip, defaults, and every rejection."""

import pathlib

import pytest

from fx_updater import config


def _cfg(**kw):
    kw.setdefault("fuchsia_dir", pathlib.Path("/src/fuchsia"))
    return config.Config(**kw)


def test_round_trip_keeps_every_field(tmp_path):
    cfg = _cfg(
        build_dirs=["core.x64", "bringup.x64"],
        schedule="Mon..Fri 04:00",
        min_free_gb=250.5,
        prom_dir=pathlib.Path("/var/lib/prom"),
    )
    path = tmp_path / "config.toml"
    config.write(path, cfg)
    assert config.load(path) == cfg


def test_round_trip_survives_awkward_paths(tmp_path):
    cfg = _cfg(fuchsia_dir=pathlib.Path('/src/a "quoted" dir\\with ünïcode'))
    path = tmp_path / "config.toml"
    config.write(path, cfg)
    assert config.load(path) == cfg


def test_only_fuchsia_dir_is_required(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('fuchsia_dir = "/src/fuchsia"\n')
    cfg = config.load(path)
    assert not cfg.build_dirs
    assert cfg.schedule == config.DEFAULT_SCHEDULE
    assert cfg.min_free_gb == config.DEFAULT_MIN_FREE_GB
    assert cfg.prom_dir is None


def test_render_is_stable():
    assert config.render(_cfg(build_dirs=["core.x64"])) == (
        "# fx-updater config. `fx-updater install` rewrites this whole file:\n"
        "# edited values are kept, comments are not.\n"
        'fuchsia_dir = "/src/fuchsia"\n'
        'build_dirs = ["core.x64"]\n'
        'schedule = "*-*-* 05:30:00"\n'
        "min_free_gb = 100.0\n"
        'prom_dir = ""\n'
    )


def test_default_path_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.default_path() == tmp_path / "fx-updater/config.toml"


@pytest.mark.parametrize(
    "text, message",
    [
        ('fuchsia_dir = "/f"\nmin_free_bg = 5\n', "unknown key(s): min_free_bg"),
        ('fuchsia_dir = "/f"\nmin_free_gb = true\n', "min_free_gb has the wrong type"),
        ('fuchsia_dir = "/f"\nmin_free_gb = "5"\n', "min_free_gb has the wrong type"),
        ('fuchsia_dir = "/f"\nmin_free_gb = -1\n', "min_free_gb must be 0 or more"),
        ('fuchsia_dir = "/f"\nbuild_dirs = "core"\n', "build_dirs has the wrong type"),
        ('fuchsia_dir = "/f"\nbuild_dirs = [1]\n', "list of strings"),
        ('fuchsia_dir = "/f"\nbuild_dirs = ["../x"]\n', "not a dir name"),
        ('fuchsia_dir = "/f\\nExecStartPre=/bin/x"\n', "control characters"),
        ('fuchsia_dir = "/f"\nschedule = "daily\\n[Service]"\n', "control characters"),
        ('fuchsia_dir = "/f"\nprom_dir = "/p\\u0000"\n', "control characters"),
        ('fuchsia_dir = "/f"\nbuild_dirs = ["out/x"]\n', "not a dir name"),
        ('fuchsia_dir = "/f"\nschedule = " "\n', "schedule must not be empty"),
        ('fuchsia_dir = "rel"\n', "fuchsia_dir must be absolute"),
        ('fuchsia_dir = "/f"\nprom_dir = "rel"\n', "prom_dir must be absolute"),
        ('schedule = "daily"\n', "fuchsia_dir is required"),
        ("fuchsia_dir = \n", "config.toml"),
    ],
)
def test_bad_config_is_refused(tmp_path, text, message):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=None) as e:
        config.load(path)
    assert message in str(e.value)


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(config.ConfigError, match="no config file"):
        config.load(tmp_path / "nope.toml")


def test_write_refuses_an_invalid_config(tmp_path):
    path = tmp_path / "config.toml"
    with pytest.raises(config.ConfigError):
        config.write(path, _cfg(fuchsia_dir=pathlib.Path("relative")))
    assert not path.exists()
