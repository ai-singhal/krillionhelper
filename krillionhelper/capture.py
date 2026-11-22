"""Grab the prompt region with mss and decide whether the pixels changed."""

import sys
import time
from dataclasses import dataclass

import mss
import numpy as np

PIXEL_DELTA = 30  # per-channel difference that counts as a changed pixel


@dataclass
class Frame:
    raw: bytes  # BGRA
    width: int
    height: int
    capturedAt: float  # time.time() when grabbed


class RegionWatcher:
    """Not thread-safe: create and use it on one thread (mss requirement)."""

    def __init__(self, region, changeThreshold=0.002, retina=True):
        self.region = region
        self.changeThreshold = changeThreshold
        if sys.platform == "darwin":
            setRetinaCapture(retina)
        self.sct = mss.MSS()
        self.previous = None

    def grab(self):
        shot = self.sct.grab(self.region)
        return Frame(bytes(shot.raw), shot.width, shot.height, time.time())

    def changed(self, frame):
        """True on the first frame and whenever enough sampled pixels differ."""
        rowBytes = len(frame.raw) // frame.height
        pixels = np.frombuffer(frame.raw, np.uint8).reshape(frame.height, rowBytes // 4, 4)
        sample = pixels[::2, : frame.width : 2, :3].astype(np.int16)
        previous, self.previous = self.previous, sample
        if previous is None or previous.shape != sample.shape:
            return True
        diff = np.abs(sample - previous).max(axis=2)
        return (diff > PIXEL_DELTA).mean() > self.changeThreshold

    def close(self):
        self.sct.close()


def setRetinaCapture(enabled):
    """mss asks macOS for 1x ("nominal") pixels; dropping that flag gives full Retina pixels."""
    import mss.darwin as darwin

    options = darwin.kCGWindowImageBoundsIgnoreFraming | darwin.kCGWindowImageShouldBeOpaque
    if not enabled:
        options |= darwin.kCGWindowImageNominalResolution
    darwin.IMAGE_OPTIONS = options
