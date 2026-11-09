"""config.toml: the shipped example, validation, --check-config, and the
migration from config.yaml. Offline: no Immich server, no display."""

from __future__ import annotations

import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from pi_panel_immich.cli import main
from pi_panel_immich.config import Config, ConfigError, convert_yaml, load_config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.toml.example"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_shipped_example_is_valid():
    cfg = load_config(EXAMPLE)
    assert cfg.renderer == "sw" and cfg.portrait_grouping == "auto"
    assert set(tomllib.loads(EXAMPLE.read_text())) == {f for f in Config.__dataclass_fields__}


@pytest.mark.parametrize("extra, message", [
    ("colour = 1", "unknown key(s): colour"),
    ('renderer = "vulkan"', "renderer must be one of sw, gpu"),
    ('display_seconds = "ten"', "display_seconds must be a number"),
    ("fullscreen = 1", "fullscreen must be true or false"),
    ("window_width = 12.5", "window_width must be a whole number"),
    ("portrait_fill = 1.5", "portrait_fill must be in 0-1"),
    ('portrait_grouping = "4"', "portrait_grouping must be one of"),
    ("ken_burns_zoom_min = 1.2", "ken_burns_zoom_min must not be above"),
])
def test_bad_values_are_refused(tmp_path, extra, message):
    text = EXAMPLE.read_text()
    key = extra.split(" = ")[0]
    lines = [ln for ln in text.splitlines() if not ln.startswith(f"{key} =")]
    path = write(tmp_path, "\n".join(lines) + f"\n{extra}\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert message in str(exc.value) and str(path) in str(exc.value)


def test_url_and_key_are_required(tmp_path):
    with pytest.raises(ConfigError, match="immich_url must start with http"):
        load_config(write(tmp_path, 'immich_url = "photos.local"\n'))
    with pytest.raises(ConfigError, match="api_key must not be empty"):
        load_config(write(tmp_path, 'immich_url = "http://p"\napi_key = ""\n'))


def test_missing_and_unparseable_files(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml")
    with pytest.raises(ConfigError, match="config.toml"):
        load_config(write(tmp_path, "renderer = \n"))


def test_check_config_flag(tmp_path, capsys):
    assert main(["--check-config", str(EXAMPLE)]) == 0
    assert "config ok" in capsys.readouterr().out
    assert main(["--check-config", str(write(tmp_path, "colour = 1\n"))]) == 1
    assert "unknown key" in capsys.readouterr().err


def test_check_config_does_not_load_pygame():
    """The manifest's [config] check must run without SDL or a display."""
    code = ("import sys; from pi_panel_immich.cli import main; "
            f"rc = main(['--check-config', {str(EXAMPLE)!r}]); "
            "assert 'pygame' not in sys.modules, 'pygame was imported'; sys.exit(rc)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_convert_yaml(tmp_path):
    old = tmp_path / "config.yaml"
    old.write_text("immich_url: \"http://photos:2283\"\napi_key: \"k\"\n"
                   "portrait_grouping: 2\nfullscreen: false\nalbum_id:\n")
    new = tmp_path / "config.toml"
    assert main(["--convert-yaml", str(old), str(new)]) == 0
    cfg = load_config(new)
    assert cfg.portrait_grouping == "2" and cfg.fullscreen is False
    assert cfg.immich_url == "http://photos:2283" and cfg.album_id == ""
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    # Never overwrites.
    with pytest.raises(ConfigError, match="already exists"):
        convert_yaml(old, new)


def test_convert_yaml_refuses_a_bad_file(tmp_path):
    old = tmp_path / "config.yaml"
    old.write_text("immich_url: \"http://p\"\napi_key: \"k\"\nrenderer: \"vulkan\"\n")
    assert main(["--convert-yaml", str(old), str(tmp_path / "c.toml")]) == 1
    assert not (tmp_path / "c.toml").exists()
