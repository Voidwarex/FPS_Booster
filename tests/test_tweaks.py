import pytest

from fps_booster import tweaks
from fps_booster.tweaks import (
    DWORD, HKCU, HKLM, SZ, Backup, Context, PowerPlanTweak, Registry, RegistryTweak,
    RegValue, TweakError,
)


class FakeRegistry(Registry):
    def __init__(self, data=None):
        self.data = dict(data or {})  # (hive, key, name) -> (kind, value)

    def read(self, hive, key, name):
        return self.data.get((hive, key, name))

    def write(self, hive, key, name, kind, value):
        self.data[(hive, key, name)] = (kind, value)

    def delete(self, hive, key, name):
        self.data.pop((hive, key, name), None)

    def subkeys(self, hive, key):
        prefix = key + "\\"
        return sorted({k[len(prefix):].split("\\")[0] for h, k, _ in self.data if h == hive and k.startswith(prefix)})


def make_ctx(tmp_path, data=None, run=None):
    return Context(FakeRegistry(data), Backup(tmp_path / "backup.json"), run or (lambda cmd: ""))


def tweak(**kw):
    return RegistryTweak(id="t", title="T", description="", category="FPS", **kw)


def test_apply_then_revert_restores_exact_originals(tmp_path):
    ctx = make_ctx(tmp_path, {(HKCU, "K", "a"): (DWORD, 5)})
    t = tweak(values=[RegValue(HKCU, "K", "a", DWORD, 0, default=1), RegValue(HKCU, "K", "b", SZ, "x")])
    assert not t.is_applied(ctx)

    t.apply(ctx)
    assert t.is_applied(ctx)
    t.apply(ctx)  # applying twice must not back up our own values

    t.revert(ctx)
    assert ctx.registry.data == {(HKCU, "K", "a"): (DWORD, 5)}  # b deleted again, a back to 5
    assert not t.is_applied(ctx)
    assert ctx.backup.get("t") is None


def test_backup_survives_restart(tmp_path):
    ctx = make_ctx(tmp_path, {(HKCU, "K", "a"): (DWORD, 5)})
    t = tweak(values=[RegValue(HKCU, "K", "a", DWORD, 0)])
    t.apply(ctx)

    ctx2 = Context(ctx.registry, Backup(tmp_path / "backup.json"), ctx.run)
    t.revert(ctx2)
    assert ctx.registry.read(HKCU, "K", "a") == (DWORD, 5)


def test_revert_without_backup_uses_windows_defaults(tmp_path):
    ctx = make_ctx(tmp_path, {(HKCU, "K", "a"): (DWORD, 0), (HKCU, "K", "b"): (DWORD, 0)})
    t = tweak(values=[RegValue(HKCU, "K", "a", DWORD, 0, default=1), RegValue(HKCU, "K", "b", DWORD, 0)])
    t.revert(ctx)
    assert ctx.registry.data == {(HKCU, "K", "a"): (DWORD, 1)}


def test_nagle_targets_only_interfaces_with_an_ip(tmp_path):
    base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    ctx = make_ctx(tmp_path, {
        (HKLM, base + r"\{A}", "DhcpIPAddress"): (SZ, "192.168.1.5"),
        (HKLM, base + r"\{B}", "EnableDHCP"): (DWORD, 1),
    })
    nagle = next(t for t in tweaks.TWEAKS if t.id == "nagle")
    nagle.apply(ctx)
    assert ctx.registry.read(HKLM, base + r"\{A}", "TCPNoDelay") == (DWORD, 1)
    assert ctx.registry.read(HKLM, base + r"\{B}", "TCPNoDelay") is None
    assert nagle.is_applied(ctx)
    nagle.revert(ctx)
    assert ctx.registry.read(HKLM, base + r"\{A}", "TCPNoDelay") is None


def test_nagle_with_no_interfaces_is_an_error(tmp_path):
    nagle = next(t for t in tweaks.TWEAKS if t.id == "nagle")
    ctx = make_ctx(tmp_path)
    assert not nagle.is_applied(ctx)
    with pytest.raises(TweakError):
        nagle.apply(ctx)


class FakePowercfg:
    def __init__(self, active, has_high=True):
        self.active, self.has_high, self.deleted = active, has_high, []

    def __call__(self, cmd):
        if cmd[1] == "/getactivescheme":
            return f"Power Scheme GUID: {self.active}  (Plan)"
        if cmd[1] == "/setactive":
            if cmd[2] == tweaks.HIGH_PERFORMANCE and not self.has_high:
                raise TweakError("Invalid Parameters")
            self.active = cmd[2]
            return ""
        if cmd[1] == "-duplicatescheme":
            return "Power Scheme GUID: 11111111-2222-3333-4444-555555555555  (Ultimate Performance)"
        if cmd[1] == "/delete":
            self.deleted.append(cmd[2])
            return ""
        raise AssertionError(cmd)


def power_tweak():
    return PowerPlanTweak(id="power_plan", title="", description="", category="FPS")


def test_power_plan_switches_and_restores(tmp_path):
    pc = FakePowercfg(tweaks.BALANCED)
    ctx, t = make_ctx(tmp_path, run=pc), power_tweak()
    t.apply(ctx)
    assert pc.active == tweaks.HIGH_PERFORMANCE and t.is_applied(ctx)
    t.revert(ctx)
    assert pc.active == tweaks.BALANCED and not pc.deleted


def test_power_plan_falls_back_to_ultimate_and_cleans_up(tmp_path):
    pc = FakePowercfg(tweaks.BALANCED, has_high=False)
    ctx, t = make_ctx(tmp_path, run=pc), power_tweak()
    t.apply(ctx)
    created = "11111111-2222-3333-4444-555555555555"
    assert pc.active == created and t.is_applied(ctx)
    t.revert(ctx)
    assert pc.active == tweaks.BALANCED and pc.deleted == [created]


def test_every_tweak_has_unique_id_and_text():
    ids = [t.id for t in tweaks.TWEAKS]
    assert len(ids) == len(set(ids))
    assert all(t.title and t.description and t.category in ("FPS", "Input lag", "Network") for t in tweaks.TWEAKS)


def test_hklm_tweaks_need_admin():
    for t in tweaks.TWEAKS:
        if isinstance(t, RegistryTweak) and not callable(t.values):
            if any(v.hive == HKLM for v in t.values):
                assert t.admin, t.id
