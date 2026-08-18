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
