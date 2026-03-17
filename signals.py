import logging
import signal
import threading
import time
from typing import Optional

log = logging.getLogger("immich-frame")


class SignalState:
    """
    Thread-safe signal flags consumed by the main loop.
    Signal handlers only set flags — all real work happens on the main thread.
    """
    def __init__(self):
        self._lock        = threading.Lock()
        self.paused       = False
        self.reload       = False
        self.shutdown     = False
        self._pause_start: Optional[float] = None
        self._total_paused: float          = 0.0

    # ── pause / resume ───────────────────────────────────────

    def pause(self):
        with self._lock:
            if not self.paused:
                self.paused       = True
                self._pause_start = time.time()
                log.info("Paused (SIGUSR1)")

    def resume(self):
        with self._lock:
            if self.paused:
                if self._pause_start is not None:
                    self._total_paused += time.time() - self._pause_start
                    self._pause_start   = None
                self.paused = False
                log.info("Resumed (SIGUSR2)")

    def paused_duration(self) -> float:
        """Total seconds spent paused so far (used to offset elapsed timer)."""
        with self._lock:
            extra = (time.time() - self._pause_start) if self._pause_start else 0.0
            return self._total_paused + extra

    def reset_pause_accum(self):
        """Call after each slide switch to zero the accumulated pause time."""
        with self._lock:
            self._total_paused = 0.0
            self._pause_start  = time.time() if self.paused else None

    # ── reload / shutdown ────────────────────────────────────

    def request_reload(self):
        with self._lock:
            self.reload = True
            log.info("Config reload requested (SIGHUP)")

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
