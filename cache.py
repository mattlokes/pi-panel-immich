import io
import logging
import math
import random
import threading
import time
from typing import Callable, Optional

import pygame
from PIL import Image

from client import ImmichClient
from config import Config
from models import Photo

log = logging.getLogger("pi-panel-immich")


def decode_photo(image_bytes: bytes, oversized: tuple[int, int], asset_id: str = "") -> Optional[Photo]:
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
            asset_id      = asset_id,
        )
    except Exception as e:
        log.error(f"Decode failed: {e}")
        return None


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
                        self._stop.wait(5)
                        continue
                asset = self._pool.pop(0)
                aid   = asset.get("id")
                if not aid:
                    continue
                log.info(f"Prefetching {aid[:8]}…")
                raw = self.client.download_thumbnail(aid)
                if raw:
                    photo = decode_photo(raw, oversized, aid)
                    if photo:
                        with self._lock:
                            self._queue.append(photo)
            else:
                self._stop.wait(0.3)

    def push_back(self, photo: Photo) -> None:
        """Return a photo to the front of the queue (e.g. when it doesn't fit a group)."""
        with self._lock:
            self._queue.insert(0, photo)

    def get_next(self, abort: Callable[[], bool] = lambda: False) -> Optional[Photo]:
        """The next prefetched photo, waiting for one if needed.

        Returns None as soon as |abort()| is true. Without that, an unreachable
        Immich server leaves this waiting forever, and SIGTERM (which only sets
        a flag) could never stop the app: systemd would have to SIGKILL it."""
        while not abort():
            with self._lock:
                if self._queue:
                    return self._queue.pop(0)
            time.sleep(0.2)
        return None

    def stop(self):
        self._stop.set()


def portrait_group_size(cfg: Config, first: Photo) -> int:
    if cfg.portrait_grouping == "2":
        return 2
    if cfg.portrait_grouping == "3":
        return 3
    return 3  # auto: try for 3, collect_slide_photos falls back to fewer if needed


def collect_slide_photos(cache: PhotoCache, cfg: Config,
                         abort: Callable[[], bool] = lambda: False) -> list[Photo]:
    """Return 1 landscape photo, or 2-3 portrait photos, as appropriate.
    Empty only if |abort()| became true while waiting."""
    first = cache.get_next(abort)
    if first is None:
        return []
    if not first.is_portrait:
        return [first]
    group = [first]
    target = portrait_group_size(cfg, first)
    for _ in range(target - 1):
        nxt = cache.get_next(abort)
        if nxt is None:
            break
        if not nxt.is_portrait:
            cache.push_back(nxt)
            break
        group.append(nxt)
    return group
