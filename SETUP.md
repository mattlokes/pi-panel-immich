# Pi Panel Immich — Setup Guide

## 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env   # or open a new terminal
```

## 2. Copy files to your Pi

```
pi-panel-immich/
├── pi_panel_immich.py
└── config.yaml
```

## 3. Configure

Edit `config.yaml`:

```yaml
immich_url: "http://192.168.1.100:2283"
api_key: "your-api-key-here"
album_id: ""        # or paste an album UUID
renderer: "sw"      # or "gpu" on Pi 5
```

**Getting your API key:** Immich → Account Settings → API Keys → Create

**Getting an Album ID:** copy the UUID from the album URL:
`/albums/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

## 4. Run

```bash
chmod +x pi_panel_immich.py
./pi_panel_immich.py
# or with a custom config:
./pi_panel_immich.py --config /path/to/config.yaml
```

## 5. Keyboard controls

| Key | Action |
|-----|--------|
| `→` Right arrow | Skip to next photo |
| `Q` or `Escape` | Quit |

---

## Signal controls

Send signals to control the running process without restarting it.

### Finding the PID

```bash
# If running directly
pidof python3

# If running as a systemd service
systemctl show pi-panel-immich --property MainPID --value
```

### Direct signals

```bash
PID=$(pidof python3)

kill -USR1 $PID    # Pause  — photo stays on screen, animation freezes
kill -USR2 $PID    # Resume — timer resets, current photo gets a full display period
kill -HUP  $PID    # Reload — re-reads config.yaml at next frame boundary
kill -TERM $PID    # Stop   — clean shutdown
```

### Via systemd (if running as a service)

```bash
systemctl kill --signal=USR1 pi-panel-immich   # pause
systemctl kill --signal=USR2 pi-panel-immich   # resume
systemctl kill --signal=HUP  pi-panel-immich   # reload config
systemctl stop               pi-panel-immich   # stop (sends SIGTERM automatically)
```

### What SIGHUP reloads

All config values take effect immediately **except** `renderer` — switching between
`sw` and `gpu` requires a full restart since the display backend is initialised at
startup. A warning is logged if you try to change it via reload.

---

## 6. Autostart on boot (systemd)

```bash
sudo nano /etc/systemd/system/pi-panel-immich.service
```

```ini
[Unit]
Description=Pi Panel Immich
After=network-online.target graphical.target
Wants=network-online.target

[Service]
User=pi
Environment=DISPLAY=:0
WorkingDirectory=/home/pi/pi-panel-immich
ExecStart=/home/pi/.cargo/bin/uv run /home/pi/pi-panel-immich/pi_panel_immich.py
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM

[Install]
WantedBy=graphical.target
```

Note the `ExecReload` line — this means `systemctl reload pi-panel-immich` will send
`SIGHUP` and trigger a live config reload without restarting the process.

```bash
sudo systemctl daemon-reload
sudo systemctl enable pi-panel-immich
sudo systemctl start pi-panel-immich

# Reload config without restarting:
sudo systemctl reload pi-panel-immich
```

---

## Transition types

**`ken_burns`** (default) — each photo slowly pans and zooms with a crossfade into
the next. Pausing mid-transition freezes cleanly on the current frame.

**`crossfade`** — simple opacity blend between two static images.

---

## Renderer options

**`sw`** — pygame software renderer. Works everywhere, no extra setup.
Recommended for testing and for any system without a supported GPU.

**`gpu`** — pysdl2 hardware renderer. Uses the VideoCore VII GPU on Pi 5 for
Ken Burns crop+scale, dropping CPU load significantly. Will crash with a clear
error message if the GPU is unavailable rather than silently falling back.

---

## Performance tuning

```yaml
# Halves CPU load vs 60fps, imperceptible on slow Ken Burns
target_fps: 30

# Reduce zoom range to lower smoothscale work (SW renderer)
ken_burns_zoom_min: 1.0
ken_burns_zoom_max: 1.08

# Fewer prefetched photos = less RAM
cache_size: 3
```
