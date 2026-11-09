# System packages

Measured on a fresh Raspberry Pi OS Lite (trixie, arm64) on a Pi 5, with
pi-panel-core already installed and `--no-install-recommends`. They match
`[system]` in `pi-panel.toml`.

```
libegl1                 # SDL's GPU renderer (Depends bring in libegl-mesa0)
libgl1                  # SDL loads libGL for its OpenGL renderer (via libglx0 -> libglx-mesa0)
libwayland-egl1         # SDL's Wayland video driver
libwayland-cursor0      # SDL's Wayland video driver
```

SDL itself is not from apt: pygame and pysdl2-dll ship their own (SDL 2.28 and 2.32).
SDL `dlopen`s the libraries above at run time, so a missing one shows up only
when the app starts, not at build time. On a panel that also builds the
compositor, the core's `-dev` packages happen to pull these in. An image with
runtime packages only does not have them.

## Not from apt

- `uv` (from the core), for `[build]` only. Since 0.4.0 everything lives in the package's own `.venv` (no longer in `~/.cache/uv`), so a read-only image needs nothing at first boot.
- Python is the system `python3` (3.13, pinned by `.python-version`): pygame publishes no wheels for newer versions yet.
