import ctypes
import logging
from abc import ABC, abstractmethod
from typing import Optional

import pygame

from config import Config
from models import KenBurnsParams, Photo

log = logging.getLogger("pi-panel-immich")


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
        pygame.display.set_caption("Pi Panel Immich")
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
            b"Pi Panel Immich",
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
        self._SDL_RenderClear        = SDL_RenderClear
        self._SDL_RenderCopyF        = SDL_RenderCopyF
        self._SDL_RenderPresent      = SDL_RenderPresent
        self._SDL_SetRenderDrawColor = SDL_SetRenderDrawColor
        self._SDL_SetTextureAlphaMod = SDL_SetTextureAlphaMod
        self._SDL_SetTextureBlendMode = SDL_SetTextureBlendMode
        self._SDL_DestroyTexture     = SDL_DestroyTexture
        self._SDL_CreateTexture      = SDL_CreateTexture
        self._SDL_UpdateTexture      = SDL_UpdateTexture
        self._SDL_PIXELFORMAT_RGB24  = SDL_PIXELFORMAT_RGB24
        self._SDL_TEXTUREACCESS_STATIC = SDL_TEXTUREACCESS_STATIC
        self._SDL_BLENDMODE_BLEND    = SDL_BLENDMODE_BLEND
        self._SDL_BLENDMODE_NONE     = SDL_BLENDMODE_NONE
        self._SDL_FRect              = SDL_FRect
        self._SDL_Rect               = SDL_Rect
        self._SDL_RenderSetClipRect  = SDL_RenderSetClipRect
        self._SDL_GetWindowSize      = SDL_GetWindowSize

        self._cfg             = cfg
        self._current_textures: list = []
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

    def _upload_texture(self, pil_img):
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
            raise RuntimeError("SDL_CreateTexture failed")
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
