"""Measure how often a game controller sends updates (its polling rate).

Two ways to read a controller:
- XInput (Xbox controllers, and anything pretending to be one) on Windows.
- Raw HID reports via hidapi (PlayStation and most other controllers).

Either way we count how many times per second a fresh update arrives while
the user keeps a stick moving.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

STANDARD_RATES = (125, 250, 500, 1000, 2000, 4000, 8000)
MICROSOFT_VID = 0x045E
WINDOW = 0.25  # seconds per bucket when estimating the rate


class Reader:
    """An open controller. ``wait_report`` returns True when an update arrives."""

    def wait_report(self, timeout: float) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        pass


@dataclass
class Controller:
    name: str
    connection: str  # "USB", "Bluetooth", "XInput"
    detail: str  # e.g. "054C:0CE6" or "Slot 1"
    open: Callable[[], Reader]

    @property
    def wireless(self) -> bool:
        return self.connection == "Bluetooth"


# ---- Rate maths ------------------------------------------------------------


@dataclass
class PollResult:
    reports: int
    rate_hz: float  # measured updates per second while the stick was moving
    standard_hz: int | None  # nearest standard polling rate, if close to one

    @property
    def interval_ms(self) -> float:
        return 1000 / self.rate_hz if self.rate_hz else 0.0


def nearest_standard(rate: float) -> int | None:
    best = min(STANDARD_RATES, key=lambda r: abs(r - rate))
    return best if abs(best - rate) <= best * 0.15 else None


def summarize(timestamps: list[float]) -> PollResult:
    """Estimate the polling rate from report arrival times.

    The test is split into short windows and only the windows where updates
    were flowing are averaged, so moments where the user paused the stick
    don't drag the number down, and bursts from OS buffering average out.
    """
    n = len(timestamps)
    if n < 3:
        return PollResult(n, 0.0, None)
    start, span = timestamps[0], timestamps[-1] - timestamps[0]
    full_windows = int(span / WINDOW)
    if full_windows < 2:
        rate = (n - 1) / span if span > 0 else 0.0
    else:
        counts = [0] * full_windows
        for t in timestamps:
            idx = int((t - start) / WINDOW)
            if idx < full_windows:  # ignore the last, partial window
                counts[idx] += 1
        # Windows where the stick was (mostly) moving; pauses fall below half.
        moving = [c for c in counts if c >= max(counts) / 2]
        rate = sum(moving) / len(moving) / WINDOW
    return PollResult(n, rate, nearest_standard(rate))


class PollTest:
    """Run a measurement on a background thread; poll ``timestamps`` live."""

    def __init__(self, controller: Controller, seconds: float = 5.0):
        self.controller = controller
        self.seconds = seconds
        self.timestamps: list[float] = []
        self.started = 0.0
        self.done = False
        self.error: Exception | None = None
        self._stop = threading.Event()

    def start(self) -> "PollTest":
        self.started = time.perf_counter()
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def cancel(self) -> None:
        self._stop.set()

    @property
    def progress(self) -> float:
        return min(1.0, (time.perf_counter() - self.started) / self.seconds)

    def live_rate(self) -> float:
        """Updates per second over the last half second."""
        now = time.perf_counter()
        recent = [t for t in self.timestamps[-20000:] if now - t <= 0.5]
        return len(recent) / 0.5

    def result(self) -> PollResult:
        return summarize(list(self.timestamps))

    def _run(self):
        reader = None
        try:
            reader = self.controller.open()
            end = self.started + self.seconds
            while not self._stop.is_set() and time.perf_counter() < end:
                if reader.wait_report(0.1):
                    self.timestamps.append(time.perf_counter())
        except Exception as exc:
            self.error = exc
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
            self.done = True


# ---- XInput (Windows) ------------------------------------------------------


class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", _XInputGamepad)]


def _xinput_dll():
    if sys.platform != "win32":
        return None
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            dll = ctypes.WinDLL(name)
        except OSError:
            continue
        dll.XInputGetState.argtypes = [ctypes.c_uint32, ctypes.POINTER(_XInputState)]
        dll.XInputGetState.restype = ctypes.c_uint32
        return dll
    return None


class XInputReader(Reader):
    """XInput has no events: poll fast and watch the packet number change."""

    def __init__(self, dll, slot: int):
        self.dll, self.slot = dll, slot
        self.state = _XInputState()
        if dll.XInputGetState(slot, ctypes.byref(self.state)) != 0:
            raise OSError("Controller disconnected")
        self.last = self.state.dwPacketNumber

    def wait_report(self, timeout):
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if self.dll.XInputGetState(self.slot, ctypes.byref(self.state)) != 0:
                raise OSError("Controller disconnected")
            if self.state.dwPacketNumber != self.last:
                self.last = self.state.dwPacketNumber
                return True
            time.sleep(0)  # yield, but keep sub-millisecond resolution
        return False


def _xinput_controllers() -> list[Controller]:
    dll = _xinput_dll()
    if dll is None:
        return []
    found = []
    for slot in range(4):
        state = _XInputState()
        if dll.XInputGetState(slot, ctypes.byref(state)) == 0:
            found.append(Controller(
                f"Xbox controller {slot + 1}", "XInput", f"Slot {slot + 1}",
                lambda slot=slot: XInputReader(dll, slot),
            ))
    return found


# ---- Raw HID (hidapi) ------------------------------------------------------


class HidReader(Reader):
    def __init__(self, path: bytes):
        import hid

        self.dev = hid.device()
        self.dev.open_path(path)

    def wait_report(self, timeout):
        return bool(self.dev.read(256, max(1, int(timeout * 1000))))

    def close(self):
        self.dev.close()


def _is_bluetooth(info: dict) -> bool:
    if info.get("bus_type") == 2:  # hidapi >= 0.14 on most platforms
        return True
    path = (info.get("path") or b"").lower()
    # Windows Bluetooth HID / BLE HID service GUIDs in the device path.
    return b"00001124-0000-1000-8000-00805f9b34fb" in path or b"00001812-0000-1000-8000-00805f9b34fb" in path


def _hid_controllers(skip_microsoft: bool) -> list[Controller]:
    try:
        import hid
    except ImportError:
        return []
    found, seen = [], set()
    for info in hid.enumerate():
        # Generic Desktop page, Joystick (4) or Game Pad (5)
        if info.get("usage_page") != 0x01 or info.get("usage") not in (0x04, 0x05):
            continue
        if skip_microsoft and info.get("vendor_id") == MICROSOFT_VID:
            continue  # same Xbox pad already listed through XInput
        path = info.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        vid, pid = info.get("vendor_id", 0), info.get("product_id", 0)
        name = (info.get("product_string") or "").strip() or "Game controller"
        found.append(Controller(
            name, "Bluetooth" if _is_bluetooth(info) else "USB", f"{vid:04X}:{pid:04X}",
            lambda path=path: HidReader(path),
        ))
    return found


def list_controllers() -> list[Controller]:
    xinput = _xinput_controllers()
    return xinput + _hid_controllers(skip_microsoft=bool(xinput))


def hid_available() -> bool:
    try:
        import hid  # noqa: F401
    except ImportError:
        return False
    return True


def describe(result: PollResult, controller: Controller) -> tuple[str, str]:
    """(level, message) explaining a result in plain words. level: good/ok/warn/bad."""
    bluetooth = (
        " This controller is connected over Bluetooth, which sets its own timing. "
        "Overclocking only works with a USB cable."
        if controller.wireless else ""
    )
    if result.reports < 20 or result.rate_hz <= 0:
        return "bad", "No movement picked up. Keep a stick moving for the whole test and try again."
    if result.rate_hz < 100 and result.standard_hz is None:
        return "warn", (
            "Fewer updates than expected. Most controllers only send an update when something "
            "changes, so keep a stick moving in circles until the test ends." + bluetooth
        )
    gap = 1000 / result.rate_hz
    if result.rate_hz >= 900:
        return "good", (
            f"A new update every {gap:.2f} ms. That's 1000 Hz or faster, about as quick as "
            "controllers get." + bluetooth
        )
    return ("warn" if result.rate_hz < 400 else "ok"), (
        f"A new update every {gap:.1f} ms. At 1000 Hz that would be 1 ms, so overclocking "
        f"could cut up to {gap - 1:.1f} ms of input delay." + bluetooth
    )
