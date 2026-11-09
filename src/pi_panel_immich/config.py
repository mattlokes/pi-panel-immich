"""config.toml: the Immich server, the album, and how photos are shown.

Every key is optional except the server's; config.toml.example documents
them. An unknown key or a bad value is a ConfigError naming the key, so
`--check-config` (the manifest's `[config] check`) and a SIGHUP reload refuse
a broken file instead of running with half of it.
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger("pi-panel-immich")

RENDERERS = ("sw", "gpu")
TRANSITIONS = ("ken_burns", "crossfade")
GROUPINGS = ("2", "3", "auto")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    # Immich connection
    immich_url: str   = "http://your-nas-ip:2283"
    api_key: str      = "your-api-key-here"
    album_id: str     = ""

    # Display
    display_seconds: float = 10.0
    fullscreen: bool       = True
    window_width: int      = 1280
    window_height: int     = 720

    # Renderer: "sw" (pygame CPU) or "gpu" (pysdl2 VideoCore)
    renderer: str = "sw"

    # Transitions
    transition_type: str  = "ken_burns"   # "ken_burns" | "crossfade"
    fade_duration: float  = 1.5

    # Ken Burns
    ken_burns_zoom_min: float  = 1.0
    ken_burns_zoom_max: float  = 1.15
    ken_burns_duration: float  = 10.0

    # Portrait grouping
    portrait_grouping: str = "auto"   # "2" | "3" | "auto"
    portrait_gap_px: int   = 10
    portrait_fill: float   = 0.0      # 0.0 = contain (letterbox), 1.0 = cover (crop to fill slot)
    landscape_fill: float  = 1.0      # 0.0 = contain (letterbox), 1.0 = cover (crop to fill screen)

    # Performance
    cache_size: int  = 6
    target_fps: int  = 30

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown key(s): {', '.join(sorted(unknown))}")
        data = dict(data)
        # YAML let `portrait_grouping: 2` through as a number; TOML wants "2".
        if isinstance(data.get("portrait_grouping"), int) and not isinstance(
                data["portrait_grouping"], bool):
            data["portrait_grouping"] = str(data["portrait_grouping"])
        cfg = cls(**data)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        def number(key: str, lo: float | None = None, hi: float | None = None,
                   integer: bool = False) -> None:
            v = getattr(self, key)
            ok_type = int if integer else (int, float)
            if isinstance(v, bool) or not isinstance(v, ok_type):
                raise ConfigError(f"{key} must be {'a whole number' if integer else 'a number'}")
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                bounds = f"{lo if lo is not None else ''}-{hi if hi is not None else ''}"
                raise ConfigError(f"{key} must be in {bounds}")
            if not integer:
                setattr(self, key, float(v))

        def text(key: str, choices: tuple[str, ...] | None = None, empty: bool = True) -> None:
            v = getattr(self, key)
            if not isinstance(v, str):
                raise ConfigError(f"{key} must be a string")
            if not empty and not v:
                raise ConfigError(f"{key} must not be empty")
            if choices and v not in choices:
                raise ConfigError(f"{key} must be one of {', '.join(choices)}")

        text("immich_url", empty=False)
        if not self.immich_url.startswith(("http://", "https://")):
            raise ConfigError("immich_url must start with http:// or https://")
        text("api_key", empty=False)
        text("album_id")
        number("display_seconds", lo=1)
        if not isinstance(self.fullscreen, bool):
            raise ConfigError("fullscreen must be true or false")
        number("window_width", lo=1, integer=True)
        number("window_height", lo=1, integer=True)
        text("renderer", RENDERERS)
        text("transition_type", TRANSITIONS)
        number("fade_duration", lo=0)
        number("ken_burns_zoom_min", lo=1, hi=2)
        number("ken_burns_zoom_max", lo=1, hi=2)
        if self.ken_burns_zoom_min > self.ken_burns_zoom_max:
            raise ConfigError("ken_burns_zoom_min must not be above ken_burns_zoom_max")
        number("ken_burns_duration", lo=0.1)
        text("portrait_grouping", GROUPINGS)
        number("portrait_gap_px", lo=0, integer=True)
        number("portrait_fill", lo=0, hi=1)
        number("landscape_fill", lo=0, hi=1)
        number("cache_size", lo=1, hi=100, integer=True)
        number("target_fps", lo=1, hi=240, integer=True)


def load_config(path: Path) -> Config:
    """The config at `path`. Raises ConfigError (naming the file) if it is
    missing, does not parse, or has an unknown key or a bad value."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
    try:
        return Config.from_dict(data)
    except (ConfigError, TypeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


# --- migrating from config.yaml -------------------------------------------------

def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v, ensure_ascii=False)   # a valid TOML basic string


def convert_yaml(src: Path, dest: Path) -> Config:
    """Write config.yaml's settings as config.toml (the format until 0.4.0).
    Validates first; refuses to overwrite `dest`. Needs PyYAML, which the app
    itself no longer depends on: run it with `uv run --with pyyaml`."""
    try:
        import yaml
    except ImportError:
        raise ConfigError("converting needs PyYAML: run it with `uv run --with pyyaml ...`") from None
    if dest.exists():
        raise ConfigError(f"{dest} already exists; not overwriting it")
    try:
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{src}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{src}: not a mapping of settings")
    data = {k: v for k, v in data.items() if v is not None}
    try:
        cfg = Config.from_dict(data)
    except (ConfigError, TypeError) as exc:
        raise ConfigError(f"{src}: {exc}") from None
    lines = [f"# pi-panel-immich: converted from {src.name}. Every key is described in",
             "# config.toml.example in the app's directory.", ""]
    lines += [f"{f.name} = {_toml_value(getattr(cfg, f.name))}" for f in fields(cfg)
              if f.name in data]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dest.chmod(0o600)          # it holds the API key
    return cfg
