import time

from fps_booster import controller
from fps_booster.controller import Controller, PollTest, Reader, nearest_standard, summarize


def ticks(rate, seconds, start=100.0):
    return [start + i / rate for i in range(int(rate * seconds))]


def test_summarize_steady_rates():
    for rate in (125, 250, 500, 1000):
        result = summarize(ticks(rate, 3))
        assert abs(result.rate_hz - rate) / rate < 0.02
        assert result.standard_hz == rate
        assert abs(result.interval_ms - 1000 / rate) < 0.2


def test_summarize_ignores_pauses():
    # Moving for 1s, stick released for 2s, moving again for 1s.
    stamps = ticks(1000, 1) + ticks(1000, 1, start=103.0)
    assert summarize(stamps).standard_hz == 1000


def test_summarize_handles_bursty_delivery():
    # 250 Hz but delivered in bursts of 4 every 16 ms (OS buffering).
    stamps = [100 + (i // 4) * 0.016 + (i % 4) * 0.0001 for i in range(250 * 3)]
    assert summarize(stamps).standard_hz == 250


def test_summarize_too_few_reports():
    assert summarize([]).rate_hz == 0
    assert summarize([1.0, 1.1]).standard_hz is None


def test_nearest_standard():
    assert nearest_standard(990) == 1000
    assert nearest_standard(131) == 125
    assert nearest_standard(700) is None


class FakeReader(Reader):
    def __init__(self, rate):
        self.interval, self.next = 1 / rate, time.perf_counter()
        self.closed = False

    def wait_report(self, timeout):
        now = time.perf_counter()
        if self.next > now:
            time.sleep(min(self.next - now, timeout))
        self.next += self.interval
        return True

    def close(self):
        self.closed = True


def test_poll_test_measures_fake_controller():
    reader = FakeReader(250)
    test = PollTest(Controller("Fake", "USB", "0000:0000", lambda: reader), seconds=1.2).start()
    deadline = time.time() + 5
    while not test.done and time.time() < deadline:
        time.sleep(0.05)
    assert test.done and test.error is None and reader.closed
    assert 200 < test.result().rate_hz < 300


def test_poll_test_reports_open_errors():
    def boom():
        raise OSError("unplugged")

    test = PollTest(Controller("Fake", "USB", "", boom), seconds=1).start()
    deadline = time.time() + 5
    while not test.done and time.time() < deadline:
        time.sleep(0.02)
    assert isinstance(test.error, OSError)


def test_list_controllers_is_safe_without_devices():
    assert isinstance(controller.list_controllers(), list)


def test_bluetooth_detection_from_windows_path():
    bt = {"path": b"\\\\?\\HID#{00001124-0000-1000-8000-00805f9b34fb}_VID&0002054c_PID&0ce6"}
    usb = {"path": b"\\\\?\\HID#VID_054C&PID_0CE6&MI_03#7&abc"}
    assert controller._is_bluetooth(bt)
    assert not controller._is_bluetooth(usb)
    assert controller._is_bluetooth({"bus_type": 2, "path": b""})


def test_describe_levels():
    usb = Controller("Pad", "USB", "", lambda: None)
    bt = Controller("Pad", "Bluetooth", "", lambda: None)
    assert controller.describe(summarize(ticks(1000, 2)), usb)[0] == "good"
    level, text = controller.describe(summarize(ticks(125, 2)), usb)
    assert level == "warn" and "7.0 ms" in text
    assert "Bluetooth" in controller.describe(summarize(ticks(250, 2)), bt)[1]
    assert controller.describe(summarize([]), usb)[0] == "bad"
