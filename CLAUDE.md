# pi-panel-immich

An Immich photo-frame slideshow (pygame / pysdl2), packaged as a pi-panel app.
`pi-panel.toml` is its manifest. Installed on jazz, it lives in
`~/pi-panel-apps/immich` and runs as `pi-panel-app@immich.service`.

## Running it

It is a PEP 723 script: dependencies are declared inline and resolved by
`uv`. There is no `pyproject.toml` or venv to manage. `pi-panel-varlink` comes
from `../../pi-panel-core/pi-panel-core-ctl/packages/pi-panel-varlink` by
relative path, which is why the install location matters.

```bash
uv run --script pi_panel_immich.py --config config.yaml     # standalone
```

Under pi-panel, `pi-panel-run` supplies the environment and the unit template
sets `SDL_VIDEODRIVER=wayland`. The manifest sets `SDL_APP_ID=immich`, which is
the correct variable on SDL 2.26+; `SDL_VIDEO_WAYLAND_APP_ID` is ignored.

## The config holds an API key

It lives in `~/.config/pi-panel/apps/immich/config.yaml` on the Pi. A local
`config.yaml` is gitignored. Do not commit it, print it, or paste it into a
log or a message. `config.yaml.example` is the one to edit for documentation.

The Immich server is `photos.home.local` (192.168.4.90).

## Threads and flags

Three things poke the main loop:
- signal handlers
- the `AppService` thread (`io.pipanel.App` and `io.pipanel.app.Immich`)
- the cache's prefetch thread

The first two only set flags on `SignalState`. The main loop does all real
work.

Pause has **reasons** (`user`, `hidden`). A user pause must survive pi-panel
hiding and re-showing the app. While `hidden`, the loop does no rendering at
all.

## Shutdown must be prompt

The app contract says: exit within 5s of SIGTERM, or systemd SIGKILLs it.
`PhotoCache.get_next()` takes an `abort` callback for exactly this reason.
Without it, an unreachable Immich server blocks the main loop forever and the
SIGTERM flag is never read. Any new blocking wait must check `sig.shutdown`
too. If signals seem to do nothing, check `grep SigBlk /proc/<pid>/status`.
