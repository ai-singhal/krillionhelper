"""One recognized line of text with its position (pixels, top-left origin)."""

from typing import NamedTuple, Optional


class OcrLine(NamedTuple):
    text: str
    height: Optional[float] = None
    top: Optional[float] = None
    left: Optional[float] = None
