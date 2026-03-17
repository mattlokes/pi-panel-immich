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
import logging
import time
from pathlib import Path

import pygame

from cache import PhotoCache, collect_slide_photos
from client import ImmichClient
from config import load_config
from models import STATIC_KB, ease_in_out, random_ken_burns
from renderers import create_renderer
from signals import SignalState, install_signal_handlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("immich-frame")


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
