"""Main loop: config reaches the panel."""
from __future__ import annotations

from trofeo_hud import app
from trofeo_hud.collectors.base import SharedState
from trofeo_hud.config import Config


class _FakePanel:
    instances: list[_FakePanel] = []

    def __init__(self) -> None:
        self.qualities: list[int] = []
        self.connected = False
        _FakePanel.instances.append(self)

    def connect(self):
        self.connected = True
        return (1280, 480)

    def send(self, img, quality: int = 90) -> bool:
        self.qualities.append(quality)
        return True

    def close(self) -> None:
        self.connected = False


def test_loop_sends_frames_at_the_configured_jpeg_quality(monkeypatch):
    monkeypatch.setattr(app, "TrofeoPanel", _FakePanel)
    cfg = Config(fps=1000.0, jpeg_quality=42)
    app.run_loop(SharedState(), cfg, stop_after_s=0.05)
    panel = _FakePanel.instances[-1]
    assert panel.qualities and set(panel.qualities) == {42}
