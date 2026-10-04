import subprocess
import sys
import time

import psutil
from fps_booster import core
from fps_booster.core import MAX_PRESETS, Preset, PresetStore


def test_store_starts_with_empty_slots(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    assert store.slots == [None] * MAX_PRESETS


def test_store_round_trip(tmp_path):
    path = tmp_path / "sub" / "presets.json"
    store = PresetStore(path)
    store.set(2, Preset("Valorant", ["chrome.exe", "Discord.exe"]))

    reloaded = PresetStore(path)
    assert reloaded.slots[2] == Preset("Valorant", ["chrome.exe", "Discord.exe"])
    assert reloaded.slots[0] is None

    reloaded.delete(2)
    assert PresetStore(path).slots == [None] * MAX_PRESETS


def test_store_ignores_corrupt_file(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text("{not json")
    assert PresetStore(path).slots == [None] * MAX_PRESETS


def test_store_caps_slots_and_dedupes(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(
        '{"slots": ['
        + ",".join(['{"name": "x", "processes": ["a.exe", "A.EXE", "b.exe", ""]}'] * 8)
        + "]}"
    )
    store = PresetStore(path)
    assert len(store.slots) == MAX_PRESETS
    assert store.slots[0].processes == ["a.exe", "b.exe"]


def test_protected_names_are_case_insensitive():
    assert core.is_protected("SVCHOST.EXE")
    assert core.is_protected("explorer.exe")
    assert not core.is_protected("chrome.exe")


def test_list_process_groups_sorted_and_excludes_self():
    groups = core.list_process_groups()
    mems = [g.memory_bytes for g in groups]
    assert mems == sorted(mems, reverse=True)
    all_pids = {pid for g in groups for pid in g.pids}
    assert psutil.Process().pid not in all_pids
    assert not any(core.is_protected(g.name) for g in groups)


def test_kill_processes_kills_target_and_reports(monkeypatch):
    # A uniquely named copy of a sleeping process we can safely kill.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        name = psutil.Process(proc.pid).name()
        # Only let kill_processes see our child, never other python processes.
        original = psutil.process_iter

        def only_child(attrs=None):
            return [p for p in original(attrs) if p.pid == proc.pid]

        monkeypatch.setattr(core.psutil, "process_iter", only_child)
        report = core.kill_processes([name, "definitely-not-running.exe", "svchost.exe"])

        assert report.killed == [name]
        assert report.freed_bytes > 0
        assert report.not_running == ["definitely-not-running.exe"]
        assert report.skipped_protected == ["svchost.exe"]
        assert not report.failed
        time.sleep(0.1)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
