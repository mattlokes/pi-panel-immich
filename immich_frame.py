#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pygame>=2.5",
#   "requests>=2.31",
#   "pillow>=10.0",
#   "pyyaml>=6.0",
#   "pysdl2>=0.9.16",
#   "pysdl2-dll>=2.28.0",
# ]
# ///
"""
Immich Frame - Native Raspberry Pi Photo Frame
Two render backends selectable via config.yaml:
  renderer: "sw"  — pygame software renderer (CPU smoothscale)
  renderer: "gpu" — pysdl2 hardware renderer (VideoCore VII on Pi 5)

Usage:
    ./immich_frame.py                        # uses config.yaml in same directory
    ./immich_frame.py --config /path/to.yaml # custom config path
"""

import argparse
import ctypes
import io
import logging
import math
import random
import signal
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pygame
import requests
import yaml
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("immich-frame")


# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
# IMMICH CLIENT
# ══════════════════════════════════════════════════════════════

@dataclass
class ImmichClient:
    base_url: str
    api_key: str
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        self.session.headers.update({"x-api-key": self.api_key, "Accept": "application/json"})
        self.base_url = self.base_url.rstrip("/")

    def get_random_assets(self, count: int = 50) -> list[dict]:
        try:
            r = self.session.get(f"{self.base_url}/api/assets/random", params={"count": count}, timeout=10)
            r.raise_for_status()
            return [a for a in r.json() if a.get("type") == "IMAGE"]
        except Exception as e:
            log.error(f"Failed to fetch random assets: {e}")
            return []

    def get_album_assets(self, album_id: str) -> list[dict]:
        try:
            r = self.session.get(f"{self.base_url}/api/albums/{album_id}", timeout=10)
            r.raise_for_status()
            return [a for a in r.json().get("assets", []) if a.get("type") == "IMAGE"]
        except Exception as e:
            log.error(f"Failed to fetch album assets: {e}")
            return []

    def download_thumbnail(self, asset_id: str, size: str = "preview") -> Optional[bytes]:
        try:
            r = self.session.get(f"{self.base_url}/api/assets/{asset_id}/thumbnail", params={"size": size}, timeout=15)
            r.raise_for_status()
            return r.content
        except Exception as e:
            log.error(f"Failed to download {asset_id}: {e}")
            return None


# ══════════════════════════════════════════════════════════════
# SHARED DATA TYPES
# ══════════════════════════════════════════════════════════════

@dataclass
class Photo:
    """Raw decoded image — orientation detected, not yet composited into a slide."""
    pil_image: Image.Image      # Kept as PIL for GPU path (uploaded as texture)
    surface: pygame.Surface     # pygame surface for SW path
    is_portrait: bool
    original_size: tuple[int, int]  # (w, h) before any scaling


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


# ══════════════════════════════════════════════════════════════
# IMAGE DECODE  (shared between both renderers)
# ══════════════════════════════════════════════════════════════

def decode_photo(image_bytes: bytes, oversized: tuple[int, int]) -> Optional[Photo]:
    """
    Decode JPEG/PNG bytes into a Photo.
    - PIL image kept at original size (GPU path uploads as-is)
    - pygame surface scaled to `oversized` (SW path uses this for Ken Burns room)
    """
    try:
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        iw, ih = pil_img.size
        is_portrait = ih > iw

        # SW surface: cover-scale to oversized so Ken Burns can crop without black bars
        tw, th = oversized
        scale = max(tw / iw, th / ih)
        new_w, new_h = int(math.ceil(iw * scale)), int(math.ceil(ih * scale))
        sw_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
        raw_surf = pygame.image.fromstring(sw_img.tobytes(), (new_w, new_h), "RGB")
        # .convert() optimises blitting but requires an active pygame display mode.
        # Skip it when no display is initialised (e.g. GPU renderer uses SDL2, not pygame).
        surface = raw_surf.convert() if pygame.display.get_surface() else raw_surf

        return Photo(
            pil_image     = pil_img,
            surface       = surface,
            is_portrait   = is_portrait,
            original_size = (iw, ih),
        )
    except Exception as e:
        log.error(f"Decode failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════
# PHOTO CACHE
# ══════════════════════════════════════════════════════════════

class PhotoCache:
    def __init__(self, client: ImmichClient, screen_size: tuple[int, int], cfg: Config):
        self.client      = client
        self.screen_size = screen_size
        self.cfg         = cfg
        self._queue: list[Photo] = []
        self._pool:  list[dict]  = []
        self._lock   = threading.Lock()
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _refill(self):
        if self.cfg.album_id:
            self._pool = self.client.get_album_assets(self.cfg.album_id)
            random.shuffle(self._pool)
        else:
            self._pool = self.client.get_random_assets(count=50)

    def _worker(self):
        oversized = (int(self.screen_size[0] * 1.3), int(self.screen_size[1] * 1.3))
        while not self._stop.is_set():
            with self._lock:
                qlen = len(self._queue)
            if qlen < self.cfg.cache_size:
                if not self._pool:
                    self._refill()
                    if not self._pool:
                        time.sleep(5)
                        continue
                asset = self._pool.pop(0)
                aid   = asset.get("id")
                if not aid:
                    continue
                log.info(f"Prefetching {aid[:8]}…")
                raw = self.client.download_thumbnail(aid)
                if raw:
                    photo = decode_photo(raw, oversized)
                    if photo:
                        with self._lock:
                            self._queue.append(photo)
            else:
                time.sleep(0.3)

    def push_back(self, photo: Photo) -> None:
        """Return a photo to the front of the queue (e.g. when it doesn't fit a group)."""
        with self._lock:
            self._queue.insert(0, photo)

    def get_next(self) -> Photo:
        while True:
            with self._lock:
                if self._queue:
                    return self._queue.pop(0)
            time.sleep(0.2)

    def stop(self):
        self._stop.set()


# ══════════════════════════════════════════════════════════════
# PORTRAIT GROUPING  (shared logic, renderer-agnostic)
# ══════════════════════════════════════════════════════════════

def portrait_group_size(cfg: Config, first: Photo) -> int:
    if cfg.portrait_grouping == "2":
        return 2
    if cfg.portrait_grouping == "3":
        return 3
    return 3  # auto: try for 3, collect_slide_photos falls back to fewer if needed


def collect_slide_photos(cache: PhotoCache, cfg: Config) -> list[Photo]:
    """Return 1 landscape photo, or 2-3 portrait photos, as appropriate."""
    first = cache.get_next()
    if not first.is_portrait:
        return [first]
    group = [first]
    target = portrait_group_size(cfg, first)
    for _ in range(target - 1):
        nxt = cache.get_next()
        if not nxt.is_portrait:
            cache.push_back(nxt)
            break
        group.append(nxt)
    return group


# ══════════════════════════════════════════════════════════════
# RENDERER ABSTRACTION
# ══════════════════════════════════════════════════════════════

class Renderer(ABC):
    """
    Abstract renderer — both SW and GPU implement this interface.
    The main loop only calls methods on this object.
    """

    @abstractmethod
    def present_slide(self, photos: list[Photo], cfg: Config) -> None:
        """Composite photos and store as current slide ready to display."""

    @abstractmethod
    def render_frame(
        self,
        t_ease: float,
        kb: KenBurnsParams,
        alpha: float = 1.0,
        kb_prev: Optional[KenBurnsParams] = None,
        alpha_prev: float = 0.0,
        prev_photos: Optional[list[Photo]] = None,
        cfg: Optional[Config] = None,
    ) -> None:
        """Draw one frame. Called every tick by the main loop."""

    @abstractmethod
    def flip(self) -> None:
        """Present rendered frame to display."""

    @abstractmethod
    def shutdown(self) -> None:
        """Clean up GPU/window resources."""

    @property
    @abstractmethod
    def screen_size(self) -> tuple[int, int]:
        ...


# ══════════════════════════════════════════════════════════════
# SW RENDERER  (pygame / CPU smoothscale)
# ══════════════════════════════════════════════════════════════

class SWRenderer(Renderer):
    """
    Software renderer using pygame surfaces.
    Ken Burns crop + scale runs entirely on the CPU via pygame.transform.smoothscale.
    """

    def __init__(self, cfg: Config):
        pygame.init()
        pygame.mouse.set_visible(False)
        flags = pygame.FULLSCREEN if cfg.fullscreen else 0
        size  = (0, 0) if cfg.fullscreen else (cfg.window_width, cfg.window_height)
        self._screen = pygame.display.set_mode(size, flags)
        pygame.display.set_caption("Immich Frame")
        self._screen.fill((0, 0, 0))
        pygame.display.flip()
        self._clock       = pygame.time.Clock()
        self._current_surf: Optional[pygame.Surface] = None
        self._prev_surf:    Optional[pygame.Surface] = None
        self._cfg         = cfg
        log.info(f"[SW] display {self._screen.get_size()}")

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._screen.get_size()

    def present_slide(self, photos: list[Photo], cfg: Config) -> None:
        """Composite photos into a single pygame surface stored as current slide."""
        sw, sh = self.screen_size
        if len(photos) == 1:
            # Single image — keep oversized for Ken Burns room
            self._prev_surf    = self._current_surf
            self._current_surf = photos[0].surface
        else:
            # Portrait group — composite onto a black canvas at screen size
            n         = len(photos)
            gap       = cfg.portrait_gap_px
            total_gap = gap * (n - 1)
            slot_w    = (sw - total_gap) // n
            canvas    = pygame.Surface((sw, sh))
            canvas.fill((0, 0, 0))
            fill = max(0.0, min(1.0, cfg.portrait_fill))
            for i, photo in enumerate(photos):
                src_w, src_h = photo.surface.get_size()
                scale_fit    = min(slot_w / src_w, sh / src_h)
                scale_cover  = max(slot_w / src_w, sh / src_h)
                scale        = scale_fit + (scale_cover - scale_fit) * fill
                scaled_w     = int(src_w * scale)
                scaled_h     = int(src_h * scale)
                scaled       = pygame.transform.smoothscale(photo.surface, (scaled_w, scaled_h))
                slot_x       = i * (slot_w + gap)
                blit_x       = slot_x + (slot_w - scaled_w) // 2
                blit_y       = (sh - scaled_h) // 2
                canvas.set_clip(pygame.Rect(slot_x, 0, slot_w, sh))
                canvas.blit(scaled, (blit_x, blit_y))
            canvas.set_clip(None)
            self._prev_surf    = self._current_surf
            self._current_surf = canvas

    def _crop_and_scale(
        self,
        source: pygame.Surface,
        zoom: float,
        pan_x: float,
        pan_y: float,
    ) -> pygame.Surface:
        sw, sh   = self.screen_size
        src_w, src_h = source.get_size()
        crop_w   = int(sw / zoom)
        crop_h   = int(sh / zoom)
        cx       = int(pan_x * src_w)
        cy       = int(pan_y * src_h)
        left     = max(0, min(cx - crop_w // 2, src_w - crop_w))
        top      = max(0, min(cy - crop_h // 2, src_h - crop_h))
        cropped  = source.subsurface(pygame.Rect(left, top, crop_w, crop_h)).copy()
        return pygame.transform.smoothscale(cropped, (sw, sh))

    def render_frame(
        self,
        t_ease: float,
        kb: KenBurnsParams,
        alpha: float = 1.0,
        kb_prev: Optional[KenBurnsParams] = None,
        alpha_prev: float = 0.0,
        prev_photos: Optional[list[Photo]] = None,
        cfg: Optional[Config] = None,
    ) -> None:
        if self._current_surf is None:
            return

        zoom = kb.start_zoom + (kb.end_zoom - kb.start_zoom) * t_ease
        px   = kb.start_pan_x + (kb.end_pan_x - kb.start_pan_x) * t_ease
        py   = kb.start_pan_y + (kb.end_pan_y - kb.start_pan_y) * t_ease
        frame_cur = self._crop_and_scale(self._current_surf, zoom, px, py)

        if kb_prev and self._prev_surf is not None and alpha_prev > 0:
            zoom_p = kb_prev.start_zoom + (kb_prev.end_zoom - kb_prev.start_zoom) * t_ease
            px_p   = kb_prev.start_pan_x + (kb_prev.end_pan_x - kb_prev.start_pan_x) * t_ease
            py_p   = kb_prev.start_pan_y + (kb_prev.end_pan_y - kb_prev.start_pan_y) * t_ease
            frame_prev = self._crop_and_scale(self._prev_surf, zoom_p, px_p, py_p)
            self._screen.blit(frame_prev, (0, 0))
            frame_cur.set_alpha(int(alpha * 255))
            self._screen.blit(frame_cur, (0, 0))
        else:
            self._screen.blit(frame_cur, (0, 0))

    def flip(self) -> None:
        pygame.display.flip()
        self._clock.tick(self._cfg.target_fps)

    def shutdown(self) -> None:
        pygame.quit()


# ══════════════════════════════════════════════════════════════
# GPU RENDERER  (pysdl2 SDL_Renderer + SDL_Texture)
# ══════════════════════════════════════════════════════════════

class GPURenderer(Renderer):
    """
    Hardware-accelerated renderer using pysdl2's SDL_Renderer.
    Ken Burns crop + scale is expressed as an SDL_RenderCopyF destination rect,
    which the VideoCore VII GPU handles entirely — zero CPU scaling per frame.

    How Ken Burns works on GPU:
      Instead of cropping the source and upscaling it (CPU approach), we do the
      inverse: we render the full texture to a destination rect that is *larger*
      than the screen and *offset* so the correct region lands on screen.
      SDL clips to the screen bounds automatically, so the GPU handles it in one
      draw call with hardware bilinear filtering.
    """

    def __init__(self, cfg: Config):
        try:
            from sdl2 import (
                SDL_Init, SDL_Quit, SDL_INIT_VIDEO,
                SDL_CreateWindow, SDL_DestroyWindow,
                SDL_WINDOW_FULLSCREEN_DESKTOP, SDL_WINDOW_SHOWN,
                SDL_CreateRenderer, SDL_DestroyRenderer,
                SDL_RENDERER_ACCELERATED, SDL_RENDERER_PRESENTVSYNC,
                SDL_CreateTexture, SDL_DestroyTexture, SDL_UpdateTexture,
                SDL_SetTextureBlendMode, SDL_RenderCopy, SDL_RenderCopyF,
                SDL_RenderClear, SDL_RenderPresent, SDL_SetRenderDrawColor,
                SDL_PIXELFORMAT_RGB24, SDL_TEXTUREACCESS_STATIC,
                SDL_BLENDMODE_BLEND, SDL_BLENDMODE_NONE,
                SDL_GetWindowSize, SDL_Rect, SDL_FRect,
                SDL_RenderSetClipRect,
                SDL_GetError,
            )
            from sdl2 import SDL_SetTextureAlphaMod
        except ImportError as e:
            raise RuntimeError(
                f"GPU renderer requires pysdl2. Install it with:\n"
                f"  uv add pysdl2 pysdl2-dll\n"
                f"Original error: {e}"
            )

        self._sdl2_imports = True

        if SDL_Init(SDL_INIT_VIDEO) != 0:
            raise RuntimeError(f"SDL_Init failed: {SDL_GetError().decode()}")

        flags = SDL_WINDOW_FULLSCREEN_DESKTOP if cfg.fullscreen else SDL_WINDOW_SHOWN
        self._window = SDL_CreateWindow(
            b"Immich Frame",
            0x1FFF0000, 0x1FFF0000,   # SDL_WINDOWPOS_CENTERED
            cfg.window_width, cfg.window_height,
            flags,
        )
        if not self._window:
            raise RuntimeError(f"SDL_CreateWindow failed: {SDL_GetError().decode()}")

        self._renderer = SDL_CreateRenderer(
            self._window, -1,
            SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC,
        )
        if not self._renderer:
            raise RuntimeError(
                f"SDL_CreateRenderer failed: {SDL_GetError().decode()}\n"
                f"Make sure the VideoCore GPU driver is active (check /boot/firmware/config.txt)."
            )

        # Stash SDL2 symbols we'll need at render time
        self._SDL_RenderClear       = SDL_RenderClear
        self._SDL_RenderCopyF       = SDL_RenderCopyF
        self._SDL_RenderPresent     = SDL_RenderPresent
        self._SDL_SetRenderDrawColor = SDL_SetRenderDrawColor
        self._SDL_SetTextureAlphaMod = SDL_SetTextureAlphaMod
        self._SDL_SetTextureBlendMode = SDL_SetTextureBlendMode
        self._SDL_DestroyTexture    = SDL_DestroyTexture
        self._SDL_CreateTexture     = SDL_CreateTexture
        self._SDL_UpdateTexture     = SDL_UpdateTexture
        self._SDL_PIXELFORMAT_RGB24 = SDL_PIXELFORMAT_RGB24
        self._SDL_TEXTUREACCESS_STATIC = SDL_TEXTUREACCESS_STATIC
        self._SDL_BLENDMODE_BLEND   = SDL_BLENDMODE_BLEND
        self._SDL_BLENDMODE_NONE    = SDL_BLENDMODE_NONE
        self._SDL_FRect              = SDL_FRect
        self._SDL_Rect               = SDL_Rect
        self._SDL_RenderSetClipRect  = SDL_RenderSetClipRect
        self._SDL_GetWindowSize      = SDL_GetWindowSize

        self._cfg             = cfg
        self._current_textures: list = []   # list of (texture, orig_w, orig_h)
        self._prev_textures:    list = []
        self._current_photos:   list[Photo] = []
        self._prev_photos:      list[Photo] = []

        # Get actual window size (may differ from requested in fullscreen)
        w, h = ctypes.c_int(0), ctypes.c_int(0)
        SDL_GetWindowSize(self._window, ctypes.byref(w), ctypes.byref(h))
        self._sw, self._sh = w.value, h.value

        log.info(f"[GPU] SDL_Renderer initialised — display {self._sw}x{self._sh}")

        # pygame still needed for event handling + clock
        pygame.init()
        pygame.mouse.set_visible(False)
        self._clock = pygame.time.Clock()

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._sw, self._sh)

    def _upload_texture(self, pil_img: Image.Image):
        """Upload a PIL image as an SDL_Texture and return (texture, w, h)."""
        iw, ih = pil_img.size
        raw    = pil_img.tobytes()
        tex    = self._SDL_CreateTexture(
            self._renderer,
            self._SDL_PIXELFORMAT_RGB24,
            self._SDL_TEXTUREACCESS_STATIC,
            iw, ih,
        )
        if not tex:
            raise RuntimeError(f"SDL_CreateTexture failed")
        pitch = iw * 3
        self._SDL_UpdateTexture(tex, None, raw, pitch)
        return tex, iw, ih

    def _compose_portrait_textures(self, photos: list[Photo], cfg: Config) -> list:
        """
        For portrait groups, upload each photo as its own texture.
        Returns list of (texture, dest_x, dest_y, dest_w, dest_h, clip_x, clip_w).
        clip_x/clip_w define the slot bounds used to prevent bleed when portrait_fill > 0.
        """
        sw, sh    = self._sw, self._sh
        n         = len(photos)
        gap       = cfg.portrait_gap_px
        total_gap = gap * (n - 1)
        slot_w    = (sw - total_gap) // n
        fill      = max(0.0, min(1.0, cfg.portrait_fill))
        result    = []
        for i, photo in enumerate(photos):
            iw, ih      = photo.original_size
            scale_fit   = min(slot_w / iw, sh / ih)
            scale_cover = max(slot_w / iw, sh / ih)
            scale       = scale_fit + (scale_cover - scale_fit) * fill
            dw          = int(iw * scale)
            dh          = int(ih * scale)
            slot_x      = i * (slot_w + gap)
            dx          = slot_x + (slot_w - dw) // 2
            dy          = (sh - dh) // 2
            tex, tw, th = self._upload_texture(photo.pil_image)
            result.append((tex, dx, dy, dw, dh, slot_x, slot_w))
        return result

    def present_slide(self, photos: list[Photo], cfg: Config) -> None:
        # Destroy previous textures
        for item in self._prev_textures:
            self._SDL_DestroyTexture(item[0])
        self._prev_textures    = self._current_textures
        self._prev_photos      = self._current_photos
        self._current_photos   = photos

        if len(photos) == 1:
            tex, tw, th = self._upload_texture(photos[0].pil_image)
            fill        = max(0.0, min(1.0, cfg.landscape_fill))
            scale_fit   = min(self._sw / tw, self._sh / th)
            scale_cover = max(self._sw / tw, self._sh / th)
            scale       = scale_fit + (scale_cover - scale_fit) * fill
            eff_w       = int(tw * scale)
            eff_h       = int(th * scale)
            self._current_textures = [(tex, 0, 0, eff_w, eff_h)]
        else:
            self._current_textures = self._compose_portrait_textures(photos, cfg)

    def _render_texture_ken_burns(
        self,
        texture_info: list,
        photos: list[Photo],
        kb: KenBurnsParams,
        t_ease: float,
        alpha: int,
        is_portrait_group: bool,
    ) -> None:
        """
        Render texture(s) with Ken Burns effect at given alpha.

        For a single landscape image: we express zoom+pan as an enlarged dst rect
        that is offset so the correct crop region appears on screen. SDL clips for us.

        For portrait groups: each photo gets its own dest rect; Ken Burns applies
        a subtle zoom/pan within each slot.
        """
        zoom = kb.start_zoom + (kb.end_zoom - kb.start_zoom) * t_ease
        px   = kb.start_pan_x + (kb.end_pan_x - kb.start_pan_x) * t_ease
        py   = kb.start_pan_y + (kb.end_pan_y - kb.start_pan_y) * t_ease
        sw, sh = self._sw, self._sh

        for item in texture_info:
            tex, base_x, base_y, base_w, base_h, *clip_info = item

            self._SDL_SetTextureAlphaMod(tex, alpha)
            if alpha < 255:
                self._SDL_SetTextureBlendMode(tex, self._SDL_BLENDMODE_BLEND)
            else:
                self._SDL_SetTextureBlendMode(tex, self._SDL_BLENDMODE_NONE)

            if not is_portrait_group:
                # Landscape: zoom out the dest rect and pan its position
                dst_w = base_w * zoom
                dst_h = base_h * zoom
                # Pan offset: px,py is the normalised centre we want on screen centre
                dst_x = sw / 2 - px * dst_w
                dst_y = sh / 2 - py * dst_h
            else:
                # Portrait group: apply a gentler Ken Burns within the slot
                slot_zoom = 1.0 + (zoom - 1.0) * 0.5
                dst_w = base_w * slot_zoom
                dst_h = base_h * slot_zoom
                # Pan within the slot
                slot_cx = base_x + base_w / 2
                slot_cy = base_y + base_h / 2
                dst_x = slot_cx - px * dst_w
                dst_y = slot_cy - py * dst_h
                # Clip to slot bounds so fill/KB don't bleed into adjacent slots
                clip_x, clip_w = clip_info
                self._SDL_RenderSetClipRect(
                    self._renderer,
                    self._SDL_Rect(int(clip_x), 0, int(clip_w), sh),
                )

            dst_rect = self._SDL_FRect(dst_x, dst_y, dst_w, dst_h)
            self._SDL_RenderCopyF(self._renderer, tex, None, dst_rect)

        if is_portrait_group:
            self._SDL_RenderSetClipRect(self._renderer, None)

    def render_frame(
        self,
        t_ease: float,
        kb: KenBurnsParams,
        alpha: float = 1.0,
        kb_prev: Optional[KenBurnsParams] = None,
        alpha_prev: float = 0.0,
        prev_photos: Optional[list[Photo]] = None,
        cfg: Optional[Config] = None,
    ) -> None:
        self._SDL_SetRenderDrawColor(self._renderer, 0, 0, 0, 255)
        self._SDL_RenderClear(self._renderer)

        is_portrait = len(self._current_photos) > 1

        # Draw previous slide fading out
        if self._prev_textures and kb_prev and alpha_prev > 0:
            is_prev_portrait = len(self._prev_photos) > 1
            self._render_texture_ken_burns(
                self._prev_textures, self._prev_photos,
                kb_prev, t_ease, int(alpha_prev * 255), is_prev_portrait,
            )

        # Draw current slide fading in (or fully opaque)
        if self._current_textures:
            self._render_texture_ken_burns(
                self._current_textures, self._current_photos,
                kb, t_ease, int(alpha * 255), is_portrait,
            )

    def flip(self) -> None:
        self._SDL_RenderPresent(self._renderer)
        self._clock.tick(self._cfg.target_fps)

    def shutdown(self) -> None:
        for item in self._current_textures + self._prev_textures:
            self._SDL_DestroyTexture(item[0])
        from sdl2 import SDL_DestroyRenderer, SDL_DestroyWindow, SDL_Quit
        SDL_DestroyRenderer(self._renderer)
        SDL_DestroyWindow(self._window)
        SDL_Quit()
        pygame.quit()


# ══════════════════════════════════════════════════════════════
# RENDERER FACTORY
# ══════════════════════════════════════════════════════════════

def create_renderer(cfg: Config) -> Renderer:
    if cfg.renderer == "gpu":
        log.info("Initialising GPU renderer (pysdl2)…")
        try:
            return GPURenderer(cfg)
        except Exception as e:
            raise SystemExit(
                f"\n[FATAL] GPU renderer failed to initialise:\n  {e}\n\n"
                f"To use the software renderer instead, set in config.yaml:\n"
                f"  renderer: \"sw\"\n"
            )
    else:
        log.info("Initialising SW renderer (pygame)…")
        return SWRenderer(cfg)


# ══════════════════════════════════════════════════════════════
# SIGNAL HANDLING
# ══════════════════════════════════════════════════════════════
#
#   SIGUSR1  — pause   (photo stays on screen, animation freezes)
#   SIGUSR2  — resume  (timer reset so current photo gets a full display period)
#   SIGHUP   — reload  (re-reads config.yaml at next frame boundary)
#   SIGTERM  — shutdown (clean teardown, same as pressing Q)
#
# Usage:
#   kill -USR1  $(pidof immich_frame.py)   # pause
#   kill -USR2  $(pidof immich_frame.py)   # resume
#   kill -HUP   $(pidof immich_frame.py)   # reload config
#   kill -TERM  $(pidof immich_frame.py)   # stop
#
# Or with systemd:
#   systemctl kill --signal=USR1  immich-frame
#   systemctl kill --signal=USR2  immich-frame
#   systemctl kill --signal=HUP   immich-frame
#   systemctl stop                immich-frame   # sends SIGTERM automatically

class SignalState:
    """
    Thread-safe signal flags consumed by the main loop.
    Signal handlers only set flags — all real work happens on the main thread.
    """
    def __init__(self):
        self._lock        = threading.Lock()
        self.paused       = False
        self.reload       = False
        self.shutdown     = False
        self._pause_start: Optional[float] = None
        self._total_paused: float          = 0.0

    # ── pause / resume ───────────────────────────────────────

    def pause(self):
        with self._lock:
            if not self.paused:
                self.paused       = True
                self._pause_start = time.time()
                log.info("Paused (SIGUSR1)")

    def resume(self):
        with self._lock:
            if self.paused:
                if self._pause_start is not None:
                    self._total_paused += time.time() - self._pause_start
                    self._pause_start   = None
                self.paused = False
                log.info("Resumed (SIGUSR2)")

    def paused_duration(self) -> float:
        """Total seconds spent paused so far (used to offset elapsed timer)."""
        with self._lock:
            extra = (time.time() - self._pause_start) if self._pause_start else 0.0
            return self._total_paused + extra

    def reset_pause_accum(self):
        """Call after each slide switch to zero the accumulated pause time."""
        with self._lock:
            self._total_paused = 0.0
            self._pause_start  = time.time() if self.paused else None

    # ── reload / shutdown ────────────────────────────────────

    def request_reload(self):
        with self._lock:
            self.reload = True
            log.info("Config reload requested (SIGHUP)")

    def consume_reload(self) -> bool:
        with self._lock:
            v = self.reload
            self.reload = False
            return v

    def request_shutdown(self):
        with self._lock:
            self.shutdown = True
            log.info("Shutdown requested (SIGTERM)")


def install_signal_handlers(state: SignalState) -> None:
    """Wire OS signals to SignalState flag setters."""
    signal.signal(signal.SIGUSR1, lambda s, f: state.pause())
    signal.signal(signal.SIGUSR2, lambda s, f: state.resume())
    signal.signal(signal.SIGHUP,  lambda s, f: state.request_reload())
    signal.signal(signal.SIGTERM, lambda s, f: state.request_shutdown())
    log.info("Signal handlers installed (USR1=pause USR2=resume HUP=reload TERM=stop)")


# ══════════════════════════════════════════════════════════════
# EVENT HANDLING
# ══════════════════════════════════════════════════════════════

def handle_events() -> tuple[bool, bool]:
    """Returns (keep_running, force_next)."""
    force_next = False
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return False, False
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_q):
                return False, False
            if event.key == pygame.K_RIGHT:
                force_next = True
    return True, force_next


# ══════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Immich Frame — native Pi photo frame")
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path(__file__).parent / "config.yaml",
        help="Path to YAML config file",
    )
    args = parser.parse_args()

    # ── Signal state (installed before renderer so signals are safe immediately)
    sig = SignalState()
    install_signal_handlers(sig)

    config_path = args.config
    cfg         = load_config(config_path)
    log.info(
        f"renderer={cfg.renderer} transition={cfg.transition_type} "
        f"display={cfg.display_seconds}s fade={cfg.fade_duration}s "
        f"fps={cfg.target_fps} portrait={cfg.portrait_grouping} gap={cfg.portrait_gap_px}px"
    )

    renderer = create_renderer(cfg)
    client   = ImmichClient(base_url=cfg.immich_url, api_key=cfg.api_key)
    cache    = PhotoCache(client, renderer.screen_size, cfg)

    log.info("Waiting for first slide…")
    first_photos = collect_slide_photos(cache, cfg)
    renderer.present_slide(first_photos, cfg)
    kb_current = STATIC_KB if cfg.transition_type == "crossfade" else random_ken_burns(cfg)

    last_switch = time.time()
    sig.reset_pause_accum()
    running = True

    # Draw the very first frame before entering the loop
    renderer.render_frame(t_ease=0.0, kb=kb_current, alpha=1.0)
    renderer.flip()

    while running:

        # ── SIGTERM: clean shutdown ───────────────────────────
        if sig.shutdown:
            log.info("Shutting down cleanly…")
            break

        # ── SIGHUP: reload config ─────────────────────────────
        if sig.consume_reload():
            new_cfg = load_config(config_path)
            if new_cfg.renderer != cfg.renderer:
                log.warning(
                    "renderer changed in config — renderer switch requires restart, ignoring"
                )
                new_cfg.renderer = cfg.renderer
            cfg = new_cfg
            # Rebuild the Immich client in case URL/key changed
            client = ImmichClient(base_url=cfg.immich_url, api_key=cfg.api_key)
            # Restart the cache with the new config (album_id / cache_size may differ)
            cache.stop()
            cache = PhotoCache(client, renderer.screen_size, cfg)
            log.info(
                f"Config reloaded — display={cfg.display_seconds}s "
                f"fade={cfg.fade_duration}s fps={cfg.target_fps}"
            )

        # ── SIGUSR1: paused ───────────────────────────────────
        if sig.paused:
            ok, _ = handle_events()
            if not ok:
                break
            # Render a static frame so the display stays alive
            t_paused = min(
                (time.time() - last_switch - sig.paused_duration()) / cfg.ken_burns_duration,
                1.0,
            )
            renderer.render_frame(t_ease=ease_in_out(t_paused), kb=kb_current, alpha=1.0)
            renderer.flip()
            continue

        # ── Normal playback ───────────────────────────────────
        ok, force_next = handle_events()
        if not ok:
            break

        # Subtract paused time so the display timer doesn't advance while paused
        elapsed    = time.time() - last_switch - sig.paused_duration()
        crossfade  = cfg.transition_type == "crossfade"
        total      = cfg.ken_burns_duration
        t_ease     = 0.0 if crossfade else ease_in_out(min(elapsed / total, 1.0))

        # ── Time to transition? ───────────────────────────────
        if elapsed >= cfg.display_seconds or force_next:
            next_photos = collect_slide_photos(cache, cfg)
            kb_next     = STATIC_KB if crossfade else random_ken_burns(cfg)
            log.info(f"Transition → {len(next_photos)} photo(s)")

            trans_start = time.time()
            fade_total  = cfg.fade_duration
            fade_begins = 0.0

            renderer.present_slide(next_photos, cfg)
            kb_entering = kb_next
            kb_leaving  = STATIC_KB if crossfade else kb_current

            while True:
                # Honour shutdown/pause during transition too
                if sig.shutdown:
                    running = False
                    break
                if sig.paused:
                    # Freeze the transition frame — don't advance trans_start
                    ok, _ = handle_events()
                    if not ok:
                        running = False
                        break
                    renderer.flip()
                    continue

                trans_elapsed = time.time() - trans_start
                if trans_elapsed >= fade_total:
                    break

                tt_ease = ease_in_out(min(trans_elapsed / fade_total, 1.0))

                if trans_elapsed >= fade_begins and cfg.fade_duration > 0:
                    fade_t    = (trans_elapsed - fade_begins) / cfg.fade_duration
                    alpha_new = ease_in_out(min(fade_t, 1.0))
                    alpha_old = 1.0 - alpha_new
                else:
                    alpha_new = 0.0
                    alpha_old = 1.0

                renderer.render_frame(
                    t_ease     = tt_ease,
                    kb         = kb_entering,
                    alpha      = alpha_new,
                    kb_prev    = kb_leaving,
                    alpha_prev = alpha_old,
                )
                renderer.flip()

                ok, fn = handle_events()
                if not ok:
                    running = False
                    break

            if not running:
                break

            kb_current  = kb_next
            last_switch = time.time()
            sig.reset_pause_accum()     # zero pause accumulator for the new slide

        else:
            # ── Idle: Ken Burns or static depending on transition mode ──
            renderer.render_frame(t_ease=t_ease, kb=kb_current if not crossfade else STATIC_KB, alpha=1.0)
            renderer.flip()

    cache.stop()
    renderer.shutdown()
    log.info("Immich Frame stopped.")


if __name__ == "__main__":
    main()
