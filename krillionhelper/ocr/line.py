"""One recognized line of text with its position (pixels, top-left origin) and ink color."""

from typing import NamedTuple, Optional, Tuple


class OcrLine(NamedTuple):
    text: str
    height: Optional[float] = None
    top: Optional[float] = None
    left: Optional[float] = None
    width: Optional[float] = None
    color: Optional[Tuple[int, int, int]] = None  # RGB of the text itself, filled in by the tracker
