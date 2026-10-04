"""Process scanning, killing and preset storage. No GUI code lives here."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psutil

MAX_PRESETS = 5
MB = 1024 * 1024

# Processes that must never be killed: doing so would crash or freeze the OS
# (or the desktop session). Compared case-insensitively.
PROTECTED_NAMES = frozenset(
    name.lower()
    for name in (
        # Windows
        "System", "System Idle Process", "Registry", "Memory Compression",
        "Secure System", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
        "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe",
        "explorer.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
        "audiodg.exe", "spoolsv.exe", "taskhostw.exe", "conhost.exe",
        "RuntimeBroker.exe", "SearchHost.exe", "StartMenuExperienceHost.exe",
        "ShellExperienceHost.exe", "TextInputHost.exe", "MsMpEng.exe",
        "SecurityHealthService.exe", "WmiPrvSE.exe", "dllhost.exe",
        "LogonUI.exe", "userinit.exe", "Taskmgr.exe",
        # Linux / macOS
        "systemd", "init", "kthreadd", "Xorg", "Xwayland", "gnome-shell",
        "kwin_x11", "kwin_wayland", "plasmashell", "dbus-daemon", "pipewire",
        "pulseaudio", "launchd", "WindowServer", "kernel_task", "loginwindow",
    )
)


def _own_pids() -> set[int]:
    """This app's own process and its ancestors (python launcher, terminal...)."""
    pids = {os.getpid()}
    try:
        for parent in psutil.Process().parents():
            pids.add(parent.pid)
    except psutil.Error:
        pass
    return pids


def is_protected(name: str) -> bool:
    return name.lower() in PROTECTED_NAMES


@dataclass
class ProcessGroup:
    """All running instances of one executable (e.g. every chrome.exe)."""

    name: str
    pids: list[int] = field(default_factory=list)
    memory_bytes: int = 0

    @property
    def count(self) -> int:
        return len(self.pids)

    @property
    def memory_mb(self) -> float:
        return self.memory_bytes / MB


def list_process_groups(min_memory_mb: float = 0) -> list[ProcessGroup]:
    """Running processes grouped by name, biggest memory users first.

    Protected system processes and this app itself are left out, since they
    can never be selected for killing anyway.
    """
    own = _own_pids()
    groups: dict[str, ProcessGroup] = {}
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        info = proc.info
        name = info.get("name")
        mem = info.get("memory_info")
        if not name or mem is None or info["pid"] in own or is_protected(name):
            continue
        if mem.rss == 0:  # kernel threads; nothing to free
            continue
        group = groups.setdefault(name.lower(), ProcessGroup(name=name))
        group.pids.append(info["pid"])
        group.memory_bytes += mem.rss

    result = [g for g in groups.values() if g.memory_mb >= min_memory_mb]
    result.sort(key=lambda g: g.memory_bytes, reverse=True)
    return result


@dataclass
class KillReport:
    killed: list[str] = field(default_factory=list)  # one entry per process
    freed_bytes: int = 0
    failed: dict[str, str] = field(default_factory=dict)  # name -> reason
    not_running: list[str] = field(default_factory=list)
    skipped_protected: list[str] = field(default_factory=list)

    @property
    def freed_mb(self) -> float:
        return self.freed_bytes / MB


def kill_processes(names: list[str], timeout: float = 3.0) -> KillReport:
    """Terminate every running process whose name is in ``names``.

    Each process is asked to close first and force-killed if it is still
    alive after ``timeout`` seconds.
    """
    report = KillReport()
    wanted: dict[str, str] = {}
    for name in names:
        if is_protected(name):
            report.skipped_protected.append(name)
        else:
            wanted[name.lower()] = name

    own = _own_pids()
    targets: list[tuple[psutil.Process, str, int]] = []
    found: set[str] = set()
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        info = proc.info
        name = info.get("name")
        if not name or name.lower() not in wanted or info["pid"] in own:
            continue
        found.add(name.lower())
        mem = info.get("memory_info")
        targets.append((proc, name, mem.rss if mem else 0))

    report.not_running = [orig for key, orig in wanted.items() if key not in found]

    for proc, _, _ in targets:
        try:
            proc.terminate()
        except psutil.Error:
            pass  # handled below when we check what is still alive

    _, alive = psutil.wait_procs([t[0] for t in targets], timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, still_alive = psutil.wait_procs(alive, timeout=1.0)
    still_alive_pids = {p.pid for p in still_alive}

    for proc, name, rss in targets:
        if proc.pid in still_alive_pids or proc.is_running():
            # Almost always AccessDenied: it belongs to another user/service.
            report.failed[name] = (
                "Access denied - try running FPS Booster as administrator"
            )
        else:
            report.killed.append(name)
            report.freed_bytes += rss
    return report


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------


@dataclass
class Preset:
    name: str
    processes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "processes": list(self.processes)}

    @classmethod
    def from_dict(cls, data: dict) -> "Preset":
        procs = [str(p) for p in data.get("processes", []) if str(p).strip()]
        # Dedupe case-insensitively, keep first spelling and order.
        seen: set[str] = set()
        unique = [p for p in procs if not (p.lower() in seen or seen.add(p.lower()))]
        return cls(name=str(data.get("name") or "Preset"), processes=unique)


def default_config_path() -> Path:
    override = os.environ.get("FPS_BOOSTER_CONFIG")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "FPSBooster" / "presets.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "fps_booster" / "presets.json"


class PresetStore:
    """Exactly ``MAX_PRESETS`` slots, each holding a Preset or None."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_config_path()
        self.slots: list[Preset | None] = [None] * MAX_PRESETS
        self.load()

    def load(self) -> None:
        self.slots = [None] * MAX_PRESETS
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        raw_slots = data.get("slots", []) if isinstance(data, dict) else []
        for i, raw in enumerate(raw_slots[:MAX_PRESETS]):
            if isinstance(raw, dict):
                self.slots[i] = Preset.from_dict(raw)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "slots": [p.to_dict() if p else None for p in self.slots],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def set(self, index: int, preset: Preset) -> None:
        self.slots[index] = preset
        self.save()

    def delete(self, index: int) -> None:
        self.slots[index] = None
        self.save()
