import math
import random
from dataclasses import dataclass

import pygame
from PIL import Image

from .config import Config


@dataclass
class Photo:
    """Raw decoded image — orientation detected, not yet composited into a slide."""
    pil_image: Image.Image      # Kept as PIL for GPU path (uploaded as texture)
    surface: pygame.Surface     # pygame surface for SW path
    is_portrait: bool
    original_size: tuple[int, int]  # (w, h) before any scaling
    asset_id: str = ""              # Immich asset id, for io.pipanel.app.Immich


@dataclass
class KenBurnsParams:
    start_zoom: float
    end_zoom: float
    start_pan_x: float
    start_pan_y: float
    end_pan_x: float
    end_pan_y: float


STATIC_KB = KenBurnsParams(
    start_zoom=1.0, end_zoom=1.0,
    start_pan_x=0.5, start_pan_y=0.5,
    end_pan_x=0.5,   end_pan_y=0.5,
)


def random_ken_burns(cfg: Config) -> KenBurnsParams:
    z_min, z_max = cfg.ken_burns_zoom_min, cfg.ken_burns_zoom_max
    margin = 0.15
    return KenBurnsParams(
        start_zoom  = random.uniform(z_min, z_max),
        end_zoom    = random.uniform(z_min, z_max),
        start_pan_x = random.uniform(margin, 1 - margin),
        start_pan_y = random.uniform(margin, 1 - margin),
        end_pan_x   = random.uniform(margin, 1 - margin),
        end_pan_y   = random.uniform(margin, 1 - margin),
    )


def ease_in_out(t: float) -> float:
    return (1 - math.cos(math.pi * t)) / 2
