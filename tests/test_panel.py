"""Panel JPEG encoding honours the configured quality."""

from __future__ import annotations

from PIL import Image

from trofeo_hud.display.panel import TrofeoPanel


class _FakeDev:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def send(self, data: bytes) -> bool:
        self.frames.append(data)
        return True


def _panel() -> tuple[TrofeoPanel, _FakeDev]:
    p = TrofeoPanel()
    p._dev = _FakeDev()
    return p, p._dev


def _noisy() -> Image.Image:
    from random import Random

    rnd = Random(0)
    img = Image.new("RGB", (128, 48))
    img.putdata([(rnd.randrange(256),) * 3 for _ in range(128 * 48)])
    return img


def test_send_encodes_at_the_requested_quality():
    p, dev = _panel()
    img = _noisy()
    assert p.send(img, quality=20) and p.send(img, quality=95)
    low, high = dev.frames
    assert len(low) < len(high)


def test_send_default_quality_is_90():
    p, dev = _panel()
    img = _noisy()
    p.send(img)
    p.send(img, quality=90)
    assert dev.frames[0] == dev.frames[1]


# ── Is the device we opened still the one on the bus? ────────────────────
#
# On macOS, hidapi's write() to an unplugged device returns -1 rather than
# raising, so trcc reports a "short chunk write" and returns False — the same
# signal as a transient soft decline. And with the keepalive_stream quirk trcc
# converts even a raise into False. send() therefore cannot tell an unplug
# from a hiccup; enumeration can. The macOS hidapi path is
# `DevSrvsID:<IORegistry entry id>`, unique per plug-in, so a replug shows up
# as a different path even though the VID:PID is present again.

from trofeo_hud.display import panel as panel_mod  # noqa: E402


class _FakeHid:
    def __init__(self, devices):
        self.devices = devices
        self.calls = 0

    def enumerate(self, vid, pid):
        self.calls += 1
        assert (vid, pid) == (panel_mod.VID, panel_mod.PID)
        return [{"path": p, "vendor_id": vid, "product_id": pid} for p in self.devices]


def test_still_attached_when_the_same_path_is_enumerated(monkeypatch):
    p, _ = _panel()
    p._path = b"DevSrvsID:1"
    monkeypatch.setattr(panel_mod, "_enumerate", _FakeHid([b"DevSrvsID:1"]).enumerate)
    assert p.still_attached() is True


def test_not_attached_when_nothing_is_enumerated(monkeypatch):
    p, _ = _panel()
    p._path = b"DevSrvsID:1"
    monkeypatch.setattr(panel_mod, "_enumerate", _FakeHid([]).enumerate)
    assert p.still_attached() is False


def test_not_attached_when_the_device_came_back_under_a_new_path(monkeypatch):
    """Replugged: VID:PID is present, but it is a new device instance and our
    handle points at the old one."""
    p, _ = _panel()
    p._path = b"DevSrvsID:1"
    monkeypatch.setattr(panel_mod, "_enumerate", _FakeHid([b"DevSrvsID:2"]).enumerate)
    assert p.still_attached() is False


def test_unknown_path_falls_back_to_presence_alone(monkeypatch):
    p, _ = _panel()
    p._path = None
    monkeypatch.setattr(panel_mod, "_enumerate", _FakeHid([b"DevSrvsID:9"]).enumerate)
    assert p.still_attached() is True
    monkeypatch.setattr(panel_mod, "_enumerate", _FakeHid([]).enumerate)
    assert p.still_attached() is False


def test_enumeration_failure_is_not_a_verdict(monkeypatch):
    """If we can't enumerate, don't force a reconnect off a guess."""
    p, _ = _panel()

    def boom(vid, pid):
        raise OSError("hidapi unavailable")

    monkeypatch.setattr(panel_mod, "_enumerate", boom)
    assert p.still_attached() is True


def test_disconnected_panel_is_not_attached():
    p = TrofeoPanel()
    assert p.still_attached() is False


def test_connect_records_the_enumerated_path(monkeypatch):
    class _Result:
        resolution = (1280, 480)
        pm_byte = 128

    class _Dev:
        def __init__(self, info, transport):
            pass

        def set_quirks(self, q):
            pass

        def connect(self):
            return _Result()

    import types

    monkeypatch.setattr(panel_mod, "_enumerate", _FakeHid([b"DevSrvsID:7"]).enumerate)
    fake_trcc = {
        "trcc.adapters.device.hid_lcd": types.SimpleNamespace(HidLcd=_Dev),
        "trcc.adapters.device.transport": types.SimpleNamespace(
            HidApiTransport=lambda v, p: None
        ),
        "trcc.core.models": types.SimpleNamespace(quirks_for=lambda *a: None),
        "trcc.core.registry": types.SimpleNamespace(find_product=lambda v, p: object()),
    }
    import sys

    for name, mod in fake_trcc.items():
        monkeypatch.setitem(sys.modules, name, mod)
    p = TrofeoPanel()
    assert p.connect() == (1280, 480)
    assert p._path == b"DevSrvsID:7"


def test_close_swallows_a_failing_disconnect():
    """After an unplug the driver's disconnect may itself raise; close() must
    still leave the panel disconnected and not propagate."""
    p = TrofeoPanel()

    class _Dev:
        def disconnect(self):
            raise OSError("device gone")

    p._dev = _Dev()
    p._path = b"x"
    p.close()
    assert p.connected is False and p._path is None


def test_enumerate_delegates_to_hidapi(monkeypatch):
    import sys
    import types

    seen = []
    fake_hid = types.SimpleNamespace(
        enumerate=lambda v, p: seen.append((v, p)) or [{"path": b"x"}]
    )
    monkeypatch.setitem(sys.modules, "hid", fake_hid)
    assert panel_mod._enumerate(1, 2) == [{"path": b"x"}]
    assert seen == [(1, 2)]


def test_first_path_is_none_when_enumeration_fails_or_finds_nothing(monkeypatch):
    def boom(vid, pid):
        raise OSError("no IOKit for you")

    monkeypatch.setattr(panel_mod, "_enumerate", boom)
    assert panel_mod._first_path() is None
    monkeypatch.setattr(panel_mod, "_enumerate", _FakeHid([]).enumerate)
    assert panel_mod._first_path() is None
