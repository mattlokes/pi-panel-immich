# pi-panel-immich

An Immich photo-frame slideshow (pygame / pysdl2). Runs as a view inside
[pi-panel-compositor](../pi-panel-compositor) on `pi@jazz`.

## Running it

A PEP 723 single-file script — dependencies are declared inline and resolved by
`uv`, so there is no `pyproject.toml` or venv to manage:

```bash
uv run --script pi_panel_immich.py --config config.yaml
```

`uv` lives at `~/.local/bin/uv` on the Pi. See the `remote-target` skill for
driving jazz over SSH.

## As a compositor view

The compositor launches it from `~/.config/pi-panel/compositor.conf`. Two
environment variables matter and are easy to get wrong:

- **`SDL_VIDEODRIVER=wayland`** — otherwise SDL tries kmsdrm and fights the
  compositor for the DRM device.
- **`SDL_APP_ID=immich`** — sets the Wayland `app_id`. This is the correct
  variable on SDL 2.26+; the older `SDL_VIDEO_WAYLAND_APP_ID` is ignored and the
  app_id comes out as `python3.13`.

`uv` execs Python as a child, so the client's PID differs from the one the
compositor launched. That works because the compositor matches a client to its
slot by walking up the process tree — but it is why `view->pid` and the running
GUI process are not the same number.

## config.yaml holds an API key

It is gitignored. Do not commit it, print it, or paste it into a log or a
message. `config.yaml.example` is the one to edit for documentation.

The Immich server is `photos.home.local` (192.168.4.90).

## The Makefile is stale

It still refers to `immich_frame.py` (renamed to `pi_panel_immich.py`),
deploys to `~/immich-frame`, and targets a host called `prime` rather than
`jazz`. Treat it as unmaintained — deploy with `rsync` and run through the
compositor instead. Worth either fixing or deleting.

## Signals

`SIGUSR1` pause, `SIGUSR2` resume, `SIGHUP` reload config, `SIGTERM` stop. All
are flag-setters consumed by the main loop (`signals.py`).

Note these only work if the process can actually receive them. Under an older
compositor that leaked its blocked signal mask to children, SIGTERM was masked
and every one of these was silently inert — if signals appear to do nothing,
check `grep SigBlk /proc/<pid>/status` before suspecting the handler.
