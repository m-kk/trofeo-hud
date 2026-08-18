"""TrofeoPanel — drives the physical LCD via trcc's device classes.

macOS-specific path validated in spike/send_test_frame.py: trcc's own
platform layer routes HID through libusb (blocked by macOS), so we use
HidApiTransport (IOHIDManager) + HidLcd directly. Firmware 4.07 on this
panel blanks when idle, so callers must stream frames continuously.
"""

from __future__ import annotations

import io
import logging

from PIL import Image

log = logging.getLogger(__name__)

VID, PID = 0x0416, 0x5302
_BCD_FIRMWARE = 0x0407  # our unit; quirk row keys on this


def _enumerate(vid: int, pid: int) -> list[dict]:
    """HID devices matching vid:pid, as hidapi reports them (`path` included)."""
    import hid

    return hid.enumerate(vid, pid)


class TrofeoPanel:
    def __init__(self) -> None:
        self._dev = None
        self._size: tuple[int, int] | None = None
        # hidapi path of the device instance we opened. On macOS this is
        # `DevSrvsID:<IORegistry entry id>` — unique per plug-in, so a replug
        # shows up as a *different* path even though VID:PID is back.
        self._path: bytes | None = None

    @property
    def size(self) -> tuple[int, int] | None:
        return self._size

    @property
    def connected(self) -> bool:
        return self._dev is not None

    def connect(self) -> tuple[int, int]:
        from trcc.adapters.device.hid_lcd import HidLcd
        from trcc.adapters.device.transport import HidApiTransport
        from trcc.core.models import quirks_for
        from trcc.core.registry import find_product

        info = find_product(VID, PID)
        if info is None:
            raise RuntimeError(f"{VID:04x}:{PID:04x} not in trcc registry")
        dev = HidLcd(info, HidApiTransport(VID, PID))
        dev.set_quirks(quirks_for(VID, PID, _BCD_FIRMWARE))
        result = dev.connect()
        self._dev = dev
        self._size = result.resolution
        self._path = _first_path()
        log.info("panel connected: %sx%s PM=%s", *result.resolution, result.pm_byte)
        return result.resolution

    def send(self, img: Image.Image, quality: int = 90) -> bool:
        if self._dev is None:
            raise RuntimeError("panel not connected")
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=quality)
        return bool(self._dev.send(buf.getvalue()))

    def still_attached(self) -> bool:
        """Is the device instance we opened still on the bus?

        `send()` cannot answer this: on macOS hidapi's write to an unplugged
        device returns -1 rather than raising, which trcc reports as a soft
        "short chunk write" — the same False as a transient decline. And a
        replugged panel is *present* again under a new path while our handle
        still points at the old instance. Enumeration tells both apart.
        Returns True when enumeration itself fails: no verdict, no reconnect.
        """
        if self._dev is None:
            return False
        try:
            paths = [d.get("path") for d in _enumerate(VID, PID)]
        except Exception as e:  # hidapi missing, IOKit hiccup — not evidence
            log.debug("HID enumeration failed (%s); assuming still attached", e)
            return True
        if self._path is None:
            return bool(paths)
        return self._path in paths

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.disconnect()
            except Exception as e:  # after an unplug the driver may object
                log.debug("panel disconnect raised (%s); dropping the handle", e)
            finally:
                self._dev = None
                self._path = None


def _first_path() -> bytes | None:
    try:
        devices = _enumerate(VID, PID)
    except Exception as e:
        log.debug("HID enumeration failed at connect (%s)", e)
        return None
    return devices[0].get("path") if devices else None
