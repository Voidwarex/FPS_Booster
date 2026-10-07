"""Windows tweaks that can raise FPS or reduce input lag.

Every tweak can be switched back off: before changing anything, the original
values are saved to a backup file, and reverting restores them exactly (or
the Windows default when no backup exists). Only well-known, documented
settings are used; nothing here disables security features.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HKCU = "HKCU"
HKLM = "HKLM"
DWORD = "dword"
SZ = "sz"

DELETE = None  # as a default: "remove the value" (it didn't exist before)


class TweakError(Exception):
    pass


# ---- Registry access (swappable so the logic is testable off Windows) ----


class Registry:
    def read(self, hive: str, key: str, name: str) -> tuple[str, object] | None:
        raise NotImplementedError

    def write(self, hive: str, key: str, name: str, kind: str, value) -> None:
        raise NotImplementedError

    def delete(self, hive: str, key: str, name: str) -> None:
        raise NotImplementedError

    def subkeys(self, hive: str, key: str) -> list[str]:
        raise NotImplementedError


class WinRegistry(Registry):
    def __init__(self):
        import winreg

        self.w = winreg
        self.hives = {HKCU: winreg.HKEY_CURRENT_USER, HKLM: winreg.HKEY_LOCAL_MACHINE}
        self.kinds = {DWORD: winreg.REG_DWORD, SZ: winreg.REG_SZ}
        self.view = winreg.KEY_WOW64_64KEY

    def read(self, hive, key, name):
        try:
            with self.w.OpenKey(self.hives[hive], key, 0, self.w.KEY_READ | self.view) as k:
                value, kind = self.w.QueryValueEx(k, name)
        except FileNotFoundError:
            return None
        return (DWORD if kind == self.w.REG_DWORD else SZ, value)

    def write(self, hive, key, name, kind, value):
        try:
            with self.w.CreateKeyEx(self.hives[hive], key, 0, self.w.KEY_SET_VALUE | self.view) as k:
                self.w.SetValueEx(k, name, 0, self.kinds[kind], value)
        except PermissionError as exc:
            raise TweakError("Needs administrator rights") from exc

    def delete(self, hive, key, name):
        try:
            with self.w.OpenKey(self.hives[hive], key, 0, self.w.KEY_SET_VALUE | self.view) as k:
                self.w.DeleteValue(k, name)
        except FileNotFoundError:
            pass
        except PermissionError as exc:
            raise TweakError("Needs administrator rights") from exc

    def subkeys(self, hive, key):
        names = []
        try:
            with self.w.OpenKey(self.hives[hive], key, 0, self.w.KEY_READ | self.view) as k:
                i = 0
                while True:
                    try:
                        names.append(self.w.EnumKey(k, i))
                    except OSError:
                        break
                    i += 1
        except FileNotFoundError:
            pass
        return names


# ---- Backup of original values -------------------------------------------


class Backup:
    """Original values per tweak, saved before the tweak first changes them."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get(self, tweak_id: str):
        return self.data.get(tweak_id)

    def keep(self, tweak_id: str, originals) -> None:
        # Never overwrite: applying twice must not back up our own values.
        if tweak_id not in self.data:
            self.data[tweak_id] = originals
            self._save()

    def update(self, tweak_id: str, **fields) -> None:
        self.data.setdefault(tweak_id, {}).update(fields)
        self._save()

    def drop(self, tweak_id: str) -> None:
        if self.data.pop(tweak_id, None) is not None:
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass
class Context:
    registry: Registry
    backup: Backup
    run: Callable[[list[str]], str]  # run a command, return stdout


# ---- Tweak types ---------------------------------------------------------


@dataclass
class Tweak:
    id: str
    title: str
    description: str
    category: str  # "FPS", "Input lag", "Network"
    admin: bool = False
    reboot: bool = False
    recommended: bool = True

    def is_applied(self, ctx: Context) -> bool:
        raise NotImplementedError

    def apply(self, ctx: Context) -> None:
        raise NotImplementedError

    def revert(self, ctx: Context) -> None:
        raise NotImplementedError


@dataclass
class RegValue:
    hive: str
    key: str
    name: str
    kind: str
    value: object
    default: object = DELETE  # Windows' own value, used if there's no backup

    @property
    def path(self) -> str:
        return f"{self.hive}\\{self.key}\\{self.name}"


@dataclass
class RegistryTweak(Tweak):
    values: list[RegValue] | Callable[[Registry], list[RegValue]] = field(default_factory=list)
    on_change: Callable[[Context], None] | None = None

    def _values(self, ctx: Context) -> list[RegValue]:
        return self.values(ctx.registry) if callable(self.values) else self.values

    def is_applied(self, ctx):
        values = self._values(ctx)
        if not values:
            return False
        for v in values:
            current = ctx.registry.read(v.hive, v.key, v.name)
            if current is None or current[1] != v.value:
                return False
        return True

    def apply(self, ctx):
        values = self._values(ctx)
        if not values:
            raise TweakError("Nothing to change on this PC")
        originals = {}
        for v in values:
            current = ctx.registry.read(v.hive, v.key, v.name)
            originals[v.path] = list(current) if current else None
        ctx.backup.keep(self.id, originals)
        for v in values:
            ctx.registry.write(v.hive, v.key, v.name, v.kind, v.value)
        if self.on_change:
            self.on_change(ctx)

    def revert(self, ctx):
        originals: dict = ctx.backup.get(self.id) or {}
        current = {v.path: v for v in self._values(ctx)}
        # Values with a backup go back to exactly what they were...
        for path, original in originals.items():
            hive, rest = path.split("\\", 1)
            key, name = rest.rsplit("\\", 1)
            if original is None:
                ctx.registry.delete(hive, key, name)
            else:
                ctx.registry.write(hive, key, name, original[0], original[1])
        # ...anything else goes back to the Windows default.
        for path, v in current.items():
            if path in originals:
                continue
            if v.default is DELETE:
                ctx.registry.delete(v.hive, v.key, v.name)
            else:
                ctx.registry.write(v.hive, v.key, v.name, v.kind, v.default)
        ctx.backup.drop(self.id)
        if self.on_change:
            self.on_change(ctx)


HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE_TEMPLATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def active_scheme(ctx: Context) -> str:
    match = _GUID.search(ctx.run(["powercfg", "/getactivescheme"]))
    if not match:
        raise TweakError("Couldn't read the active power plan")
    return match.group(0).lower()


@dataclass
class PowerPlanTweak(Tweak):
    """Switch to High performance (or Ultimate Performance where High is hidden)."""

    def _ours(self, ctx) -> set[str]:
        saved = ctx.backup.get(self.id) or {}
        return {HIGH_PERFORMANCE} | ({saved["created"]} if saved.get("created") else set())

    def is_applied(self, ctx):
        return active_scheme(ctx) in self._ours(ctx)

    def apply(self, ctx):
        ctx.backup.keep(self.id, {"previous": active_scheme(ctx), "created": None})
        try:
            ctx.run(["powercfg", "/setactive", HIGH_PERFORMANCE])
            return
        except TweakError:
            pass  # hidden on many laptops (Modern Standby): fall back below
        # Ultimate Performance has to be copied from its template first.
        out = ctx.run(["powercfg", "-duplicatescheme", ULTIMATE_TEMPLATE])
        match = _GUID.findall(out)
        if not match:
            raise TweakError("Couldn't create a high-performance power plan")
        created = match[-1].lower()
        ctx.backup.update(self.id, created=created)
        ctx.run(["powercfg", "/setactive", created])

    def revert(self, ctx):
        saved = ctx.backup.get(self.id) or {}
        ctx.run(["powercfg", "/setactive", saved.get("previous") or BALANCED])
        if saved.get("created"):
            try:
                ctx.run(["powercfg", "/delete", saved["created"]])
            except TweakError:
                pass  # leaving an unused plan behind is harmless
        ctx.backup.drop(self.id)


USB_SUBGROUP = "2a737441-1930-4402-8d77-b2bebba308a3"
USB_SELECTIVE_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"
_HEX = re.compile(r"0x([0-9a-f]{8})", re.I)


@dataclass
class UsbSuspendTweak(Tweak):
    """Turn off USB selective suspend in the active power plan (plugged in + battery)."""

    def _values(self, ctx, scheme: str) -> tuple[int, int]:
        out = ctx.run(["powercfg", "/query", scheme, USB_SUBGROUP, USB_SELECTIVE_SUSPEND])
        # Labels are translated, but the two current values are always hex.
        found = _HEX.findall(out)
        if len(found) < 2:
            raise TweakError("This PC's power plan has no USB suspend setting")
        return int(found[-2], 16), int(found[-1], 16)

    def _set(self, ctx, scheme: str, ac: int, dc: int):
        for flag, value in (("/setacvalueindex", ac), ("/setdcvalueindex", dc)):
            ctx.run(["powercfg", flag, scheme, USB_SUBGROUP, USB_SELECTIVE_SUSPEND, str(value)])
        ctx.run(["powercfg", "/setactive", active_scheme(ctx)])  # reload so it takes effect

    def is_applied(self, ctx):
        return self._values(ctx, active_scheme(ctx)) == (0, 0)

    def apply(self, ctx):
        scheme = active_scheme(ctx)
        ac, dc = self._values(ctx, scheme)
        ctx.backup.keep(self.id, {"scheme": scheme, "ac": ac, "dc": dc})
        self._set(ctx, scheme, 0, 0)

    def revert(self, ctx):
        saved = ctx.backup.get(self.id)
        if saved:
            self._set(ctx, saved["scheme"], saved["ac"], saved["dc"])
        else:
            self._set(ctx, active_scheme(ctx), 1, 1)  # Windows default: enabled
        ctx.backup.drop(self.id)


# ---- Live-apply helpers --------------------------------------------------


def _apply_mouse_settings(ctx: Context) -> None:
    """Make the mouse registry values take effect now, not at next sign-in."""
    if sys.platform != "win32":
        return
    key = r"Control Panel\Mouse"
    read = lambda name, default: int((ctx.registry.read(HKCU, key, name) or (SZ, default))[1])
    params = (ctypes.c_int * 3)(read("MouseThreshold1", 6), read("MouseThreshold2", 10), read("MouseSpeed", 1))
    SPI_SETMOUSE, SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 0x0004, 0x01, 0x02
    ctypes.windll.user32.SystemParametersInfoW(SPI_SETMOUSE, 0, params, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)


def _network_interfaces(registry: Registry) -> list[RegValue]:
    base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    values = []
    for guid in registry.subkeys(HKLM, base):
        key = f"{base}\\{guid}"
        # Only adapters that actually have an IP address.
        if registry.read(HKLM, key, "DhcpIPAddress") or registry.read(HKLM, key, "IPAddress"):
            values += [
                RegValue(HKLM, key, "TcpAckFrequency", DWORD, 1),
                RegValue(HKLM, key, "TCPNoDelay", DWORD, 1),
            ]
    return values


# ---- The tweak list ------------------------------------------------------

MM = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
GAMES_TASK = MM + r"\Tasks\Games"

TWEAKS: list[Tweak] = [
    PowerPlanTweak(
        id="power_plan",
        title="High performance power plan",
        description="Stops Windows from down-clocking the CPU and parking cores mid-game, which "
        "causes stutter and frame drops. Laptops will run warmer and use more battery.",
        category="FPS",
    ),
    UsbSuspendTweak(
        id="usb_suspend",
        title="Turn off USB selective suspend",
        description="Stops Windows putting USB ports into power-saving mode. A controller, mouse or "
        "headset that gets suspended can miss inputs or hitch for a moment when it wakes up.",
        category="Input lag",
    ),
    RegistryTweak(
        id="game_mode",
        title="Turn on Game Mode",
        description="Windows Game Mode gives the game priority for CPU and GPU time and stops "
        "Windows Update from installing drivers or showing restart prompts while you play.",
        category="FPS",
        values=[
            RegValue(HKCU, r"Software\Microsoft\GameBar", "AutoGameModeEnabled", DWORD, 1),
            RegValue(HKCU, r"Software\Microsoft\GameBar", "AllowAutoGameMode", DWORD, 1),
        ],
    ),
    RegistryTweak(
        id="game_dvr",
        title="Turn off Xbox background recording",
        description="Game DVR keeps recording your gameplay in the background so you can save "
        "clips. That costs FPS and adds latency. Game Bar itself still works.",
        category="FPS",
        values=[
            RegValue(HKCU, r"System\GameConfigStore", "GameDVR_Enabled", DWORD, 0, default=1),
            RegValue(HKCU, r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
                     "AppCaptureEnabled", DWORD, 0),
        ],
    ),
    RegistryTweak(
        id="mouse_accel",
        title="Turn off mouse acceleration",
        description="Turns off \"Enhance pointer precision\", so the same hand movement always "
        "moves the cursor the same distance. Gives consistent aim in games that don't use raw input.",
        category="Input lag",
        values=[
            RegValue(HKCU, r"Control Panel\Mouse", "MouseSpeed", SZ, "0", default="1"),
            RegValue(HKCU, r"Control Panel\Mouse", "MouseThreshold1", SZ, "0", default="6"),
            RegValue(HKCU, r"Control Panel\Mouse", "MouseThreshold2", SZ, "0", default="10"),
        ],
        on_change=_apply_mouse_settings,
    ),
    RegistryTweak(
        id="mmcss_games",
        title="Prioritise games in the scheduler",
        description="Reserves less CPU time for background tasks (10% instead of 20%) and raises "
        "the CPU and disk priority of games that register with the Windows multimedia scheduler.",
        category="FPS",
        admin=True,
        reboot=True,
        values=[
            RegValue(HKLM, MM, "SystemResponsiveness", DWORD, 10, default=20),
            RegValue(HKLM, GAMES_TASK, "GPU Priority", DWORD, 8, default=8),
            RegValue(HKLM, GAMES_TASK, "Priority", DWORD, 6, default=2),
            RegValue(HKLM, GAMES_TASK, "Scheduling Category", SZ, "High", default="Medium"),
            RegValue(HKLM, GAMES_TASK, "SFIO Priority", SZ, "High", default="Normal"),
        ],
    ),
    RegistryTweak(
        id="power_throttling",
        title="Turn off power throttling",
        description="Stops Windows from slowing down apps it thinks are in the background, which "
        "can catch game launchers, overlays and voice chat. Mostly matters on laptops.",
        category="FPS",
        admin=True,
        reboot=True,
        values=[
            RegValue(HKLM, r"SYSTEM\CurrentControlSet\Control\Power\PowerThrottling",
                     "PowerThrottlingOff", DWORD, 1),
        ],
    ),
    RegistryTweak(
        id="network_throttling",
        title="Turn off network throttling",
        description="Windows limits network traffic while media is playing. Turning that off "
        "keeps online games from being slowed down while music or a video plays.",
        category="Network",
        admin=True,
        reboot=True,
        values=[RegValue(HKLM, MM, "NetworkThrottlingIndex", DWORD, 0xFFFFFFFF, default=10)],
    ),
    RegistryTweak(
        id="nagle",
        title="Turn off Nagle's algorithm",
        description="Sends small network packets straight away instead of bundling them. Lowers "
        "ping in games that use TCP (mostly older games and MMOs); most modern shooters use UDP "
        "and aren't affected.",
        category="Network",
        admin=True,
        reboot=True,
        values=_network_interfaces,
    ),
    RegistryTweak(
        id="hags",
        title="Hardware-accelerated GPU scheduling",
        description="Lets the graphics card manage its own memory, which can cut latency a little. "
        "Needs a GTX 1000 / RX 5000 series or newer GPU. Results vary by game, so it's off in "
        "\"Apply recommended\".",
        category="Input lag",
        admin=True,
        reboot=True,
        recommended=False,
        values=[
            RegValue(HKLM, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
                     "HwSchMode", DWORD, 2, default=1),
        ],
    ),
]


# ---- Admin helpers + standby memory --------------------------------------


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    if not is_windows():
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Start a new elevated copy of the app (UAC prompt). True if it launched."""
    if not is_windows():
        return False
    if getattr(sys, "frozen", False):  # packaged .exe
        exe, params, cwd = sys.executable, "", None
    else:
        exe, params = sys.executable, "-m fps_booster"
        cwd = str(Path(__file__).resolve().parent.parent)
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, cwd, 1)
    return result > 32  # ShellExecute returns >32 on success


def run_command(cmd: list[str]) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    if proc.returncode != 0:
        raise TweakError((proc.stderr or proc.stdout).strip() or f"{cmd[0]} failed")
    return proc.stdout


def default_context(backup_path: Path) -> Context | None:
    if not is_windows():
        return None
    return Context(WinRegistry(), Backup(backup_path), run_command)


def clear_standby_memory() -> None:
    """Empty the standby list (cached files in RAM), like Microsoft's RAMMap.

    A large standby list can cause stutter in some games. Needs admin.
    """
    if not is_windows():
        raise TweakError("Windows only")
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Luid", LUID), ("Attributes", wintypes.DWORD)]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, wintypes.LPVOID, wintypes.ULONG]
    ntdll.NtSetSystemInformation.restype = ctypes.c_long

    TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY, SE_PRIVILEGE_ENABLED = 0x20, 0x8, 0x2
    ERROR_NOT_ALL_ASSIGNED = 1300
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
    ):
        raise TweakError("Couldn't open process token")
    try:
        privs = TOKEN_PRIVILEGES(1, LUID(), SE_PRIVILEGE_ENABLED)
        if not advapi32.LookupPrivilegeValueW(None, "SeProfileSingleProcessPrivilege", ctypes.byref(privs.Luid)):
            raise TweakError("Couldn't look up privilege")
        ok = advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privs), 0, None, None)
        if not ok or ctypes.get_last_error() == ERROR_NOT_ALL_ASSIGNED:
            raise TweakError("Needs administrator rights")
    finally:
        kernel32.CloseHandle(token)

    SystemMemoryListInformation, MemoryPurgeStandbyList = 80, 4
    command = ctypes.c_int(MemoryPurgeStandbyList)
    status = ntdll.NtSetSystemInformation(
        SystemMemoryListInformation, ctypes.byref(command), ctypes.sizeof(command)
    )
    if status != 0:
        raise TweakError(f"Windows refused (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")
