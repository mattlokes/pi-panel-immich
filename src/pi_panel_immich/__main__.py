"""
Pi Panel Immich - Native Raspberry Pi Photo Frame
Two render backends selectable via config.toml:
  renderer = "sw"  — pygame software renderer (CPU smoothscale)
  renderer = "gpu" — pysdl2 hardware renderer (VideoCore VII on Pi 5)

Usage (the command-line handling is in cli.py, which imports this only to run):
    pi-panel-immich --config config.toml

As a pi-panel app (pi-panel-app@immich.service) it also serves io.pipanel.App
on $PI_PANEL_APP_SOCKET: it pauses while hidden, and offers next / pause /
resume / reload actions, plus io.pipanel.app.Immich.GetCurrentPhotos.
"""

import logging
import time
from pathlib import Path

import pygame
from pi_panel_varlink import AppService, Interface

from .cache import PhotoCache, collect_slide_photos
from .client import ImmichClient
from .config import ConfigError, load_config
from .models import STATIC_KB, ease_in_out, random_ken_burns
from .renderers import create_renderer
from .signals import SignalState, install_signal_handlers

IMMICH_INTERFACE = """\
# Immich photo frame: details beyond the generic io.pipanel.App.
interface io.pipanel.app.Immich

type Photo (asset_id: string, portrait: bool)

# The photo(s) on screen now: one landscape photo, or a group of portraits.
method GetCurrentPhotos() -> (photos: []Photo, paused: bool)
"""

log = logging.getLogger("pi-panel-immich")


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


def start_app_service(sig: SignalState, current: list) -> AppService:
    """io.pipanel.App for pi-panel. Handlers run on the service thread, so they
    only set flags on |sig|, exactly as the signal handlers do."""
    immich = Interface(IMMICH_INTERFACE)

    @immich.method("GetCurrentPhotos")
    async def get_current_photos(call):
        # The main loop replaces the contents wholesale (current[:] = ...);
        # list() takes a consistent copy under the GIL.
        photos = list(current)
        return {"photos": [{"asset_id": p.asset_id, "portrait": p.is_portrait} for p in photos],
                "paused": sig.paused}

    service = AppService(
        product="pi-panel-immich",
        on_visible=sig.set_visible,
        actions={
            "next":   ("Next photo", sig.request_next),
            "pause":  ("Pause", sig.pause),
            "resume": ("Resume", sig.resume),
            "reload": ("Reload config", sig.request_reload),
        },
        interfaces=[immich],
    )
    if service.start():
        log.info(f"Serving io.pipanel.App on {service.address}")
    return service


def run(config_path: Path, cfg) -> None:
    """The slideshow, with an already validated config (see cli.py)."""
    # ── Signal state (installed before renderer so signals are safe immediately)
    sig = SignalState()
    install_signal_handlers(sig)

    log.info(
        f"renderer={cfg.renderer} transition={cfg.transition_type} "
        f"display={cfg.display_seconds}s fade={cfg.fade_duration}s "
        f"fps={cfg.target_fps} portrait={cfg.portrait_grouping} gap={cfg.portrait_gap_px}px"
    )

    current: list = []
    service  = start_app_service(sig, current)
    renderer = create_renderer(cfg)
    client   = ImmichClient(base_url=cfg.immich_url, api_key=cfg.api_key)
    cache    = PhotoCache(client, renderer.screen_size, cfg)

    def stopping() -> bool:
        return sig.shutdown

    log.info("Waiting for first slide…")
    first_photos = collect_slide_photos(cache, cfg, abort=stopping)
    if not first_photos:
        log.info("Stopped before the first photo arrived.")
        cache.stop()
        service.stop()
        renderer.shutdown()
        return
    current[:] = first_photos
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
            try:
                new_cfg = load_config(config_path)
            except ConfigError as exc:
                log.error(f"reload refused, keeping the running config: {exc}")
                continue
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

        # ── Hidden by pi-panel (or display off): do no work ──
        if sig.hidden:
            ok, _ = handle_events()
            if not ok:
                break
            time.sleep(0.2)
            continue

        # ── SIGUSR1 / "pause" action: paused ─────────────────
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
        force_next = sig.consume_next() or force_next

        # Subtract paused time so the display timer doesn't advance while paused
        elapsed    = time.time() - last_switch - sig.paused_duration()
        crossfade  = cfg.transition_type == "crossfade"
        total      = cfg.ken_burns_duration
        t_ease     = 0.0 if crossfade else ease_in_out(min(elapsed / total, 1.0))

        # ── Time to transition? ───────────────────────────────
        if elapsed >= cfg.display_seconds or force_next:
            next_photos = collect_slide_photos(cache, cfg, abort=stopping)
            if not next_photos:
                break   # shutting down
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
                    if sig.hidden:
                        time.sleep(0.2)   # off screen: no need to redraw at all
                    else:
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
            current[:]  = next_photos
            last_switch = time.time()
            sig.reset_pause_accum()     # zero pause accumulator for the new slide

        else:
            # ── Idle: Ken Burns or static depending on transition mode ──
            renderer.render_frame(t_ease=t_ease, kb=kb_current if not crossfade else STATIC_KB, alpha=1.0)
            renderer.flip()

    cache.stop()
    service.stop()
    renderer.shutdown()
    log.info("Pi Panel Immich stopped.")


if __name__ == "__main__":
    from .cli import main
    raise SystemExit(main())
