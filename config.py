import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("immich-frame")


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


def load_config(path: Path) -> Config:
    if not path.exists():
        log.warning(f"Config not found at {path}, using defaults.")
        return Config()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    cfg = Config()
    for key, value in data.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
        else:
            log.warning(f"Unknown config key '{key}' — ignoring.")
    return cfg
