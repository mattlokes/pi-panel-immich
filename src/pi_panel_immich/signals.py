import logging
import signal
import threading
import time
from typing import Optional

log = logging.getLogger("pi-panel-immich")

# Why the slideshow is paused. Pauses from different sources must not cancel
# each other: a slideshow the user paused stays paused after the panel hides
# and re-shows it.
USER = "user"        # SIGUSR1 / the "pause" action
HIDDEN = "hidden"    # pi-panel-ctl: not on screen (or the display is off)


class SignalState:
    """
    Thread-safe flags consumed by the main loop.

    Signal handlers and the io.pipanel.App service thread only set flags —
    all real work happens on the main thread.
    """
    def __init__(self):
        self._lock        = threading.Lock()
        self._reasons: set[str] = set()
        self.reload       = False
        self.shutdown     = False
        self._next        = False
        self._pause_start: Optional[float] = None
        self._total_paused: float          = 0.0

    # ── pause / resume ───────────────────────────────────────

    @property
    def paused(self) -> bool:
        return bool(self._reasons)

    @property
    def hidden(self) -> bool:
        return HIDDEN in self._reasons

    def pause(self, reason: str = USER):
        with self._lock:
            if not self._reasons:
                self._pause_start = time.time()
            if reason not in self._reasons:
                self._reasons.add(reason)
                log.info(f"Paused ({reason})")

    def resume(self, reason: str = USER):
        with self._lock:
            if reason not in self._reasons:
                return
            self._reasons.discard(reason)
            log.info(f"Resumed ({reason})")
            if not self._reasons and self._pause_start is not None:
                self._total_paused += time.time() - self._pause_start
                self._pause_start   = None

    def set_visible(self, visible: bool):
        (self.resume if visible else self.pause)(HIDDEN)

    def paused_duration(self) -> float:
        """Total seconds spent paused so far (used to offset elapsed timer)."""
        with self._lock:
            extra = (time.time() - self._pause_start) if self._pause_start else 0.0
            return self._total_paused + extra

    def reset_pause_accum(self):
        """Call after each slide switch to zero the accumulated pause time."""
        with self._lock:
            self._total_paused = 0.0
            self._pause_start  = time.time() if self._reasons else None

    # ── next / reload / shutdown ─────────────────────────────

    def request_next(self):
        with self._lock:
            self._next = True

    def consume_next(self) -> bool:
        with self._lock:
            v = self._next
            self._next = False
            return v

    def request_reload(self):
        with self._lock:
            self.reload = True
            log.info("Config reload requested")

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
