"""Pick the fastest reliable OCR engine for this OS.

Every engine exposes `name` and `recognize(frame) -> [OcrLine]`, top to bottom.
"""

import sys


def createOcrEngine():
    errors = []
    if sys.platform == "darwin":
        try:
            from .appleVision import AppleVisionOcr

            return AppleVisionOcr()
        except ImportError as e:
            errors.append(f"Apple Vision: {e}")
    try:
        from .rapid import RapidOcr

        return RapidOcr()
    except ImportError as e:
        errors.append(f"RapidOCR: {e}")
    try:
        from .tesseract import TesseractOcr

        return TesseractOcr()
    except (ImportError, RuntimeError) as e:
        errors.append(f"Tesseract: {e}")
    raise RuntimeError("No OCR engine available. Tried: " + "; ".join(errors))
