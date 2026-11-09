# pi-panel-immich

A photo frame for pi-panel. It shows photos from an [Immich](https://immich.app)
server as a Ken Burns slideshow, and groups portrait photos two or three to a
screen.

## Install

On the panel, as the panel user:

```bash
sudo apt install --no-install-recommends libegl1 libgl1 libwayland-egl1 libwayland-cursor0
pi-panel-pkg install https://github.com/mattlokes/pi-panel-immich.git
$EDITOR /persistent/pi-panel/config/apps/immich/config.toml     # server URL + API key
pi-panel-ctl config check
pi-panel-ctl enable immich
```

`pi-panel-pkg` runs the build step (`uv sync --frozen --no-dev`), which puts
every dependency in the package's own `.venv`, and copies `config.toml.example`
to the config directory the first time only. Later updates keep your config.

An old `config.yaml` (before 0.4.0) converts with
`uv run --with pyyaml pi-panel-immich --convert-yaml config.yaml config.toml`,
which checks it and never overwrites.

- **API key:** in Immich, go to Account Settings → API Keys → Create.
- **Album:** copy the UUID from the album URL (`/albums/<uuid>`). Leave
  `album_id` empty to show random photos from the whole library.

The config file holds the API key, so keep it private:
`chmod 600 /persistent/pi-panel/config/apps/immich/config.toml`.

## Control

```bash
pi-panel-ctl show immich --seconds 60        # put it on screen for a minute
pi-panel-ctl action immich next              # next photo
pi-panel-ctl action immich pause             # also: resume, reload
pi-panel-ctl app immich io.pipanel.app.Immich.GetCurrentPhotos
journalctl -u pi-panel-app@immich -f
```

The same actions appear as Home Assistant buttons when the MQTT plugin is
installed.

While the frame is not on screen, or the display is off, it stops rendering
completely. pi-panel tells it through `io.pipanel.App.SetVisible`. A pause you
set yourself survives being hidden and shown again.

Signals still work, since pi-panel's polkit rule allows `systemctl kill` on
panel units:

```bash
systemctl kill -s USR1 pi-panel-app@immich   # pause
systemctl kill -s USR2 pi-panel-app@immich   # resume
systemctl kill -s HUP  pi-panel-app@immich   # reload config.toml (a bad file is refused)
```

### What a reload changes

Every setting takes effect immediately except `renderer`. Switching between
`sw` and `gpu` needs a restart (`pi-panel-ctl restart immich`), because the
display backend is set up at startup.

## Running it standalone

It still runs outside pi-panel, e.g. on a desktop. In that case it serves no
Varlink socket:

```bash
uv sync
uv run pi-panel-immich --config config.toml
```

| Key | Action |
|---|---|
| `→` | Next photo |
| `Q` / `Escape` | Quit |

## Transitions

- **`ken_burns`** (the default): each photo slowly pans and zooms, with a crossfade into the next. Pausing mid-transition freezes cleanly on the current frame.
- **`crossfade`**: a simple opacity blend between two still images.

## Renderers

- **`sw`**: the pygame software renderer. Works everywhere with no extra setup.
- **`gpu`**: the pysdl2 hardware renderer. On a Pi 5 it uses the VideoCore VII GPU for the Ken Burns crop and scale, which cuts CPU load a lot. If the GPU is unavailable it fails with a clear error rather than falling back silently.

## Performance tuning

```toml
target_fps = 30            # halves CPU vs 60; imperceptible on a slow Ken Burns
ken_burns_zoom_min = 1.0   # a smaller zoom range means less scaling work (sw renderer)
ken_burns_zoom_max = 1.08
cache_size = 3             # fewer prefetched photos, less RAM
```
