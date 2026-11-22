"""Windows/Linux OCR through RapidOCR (PaddleOCR models on onnxruntime)."""

import numpy as np
from rapidocr_onnxruntime import RapidOCR

from .line import OcrLine


class RapidOcr:
    name = "RapidOCR"

    def __init__(self):
        self.engine = RapidOCR()

    def recognize(self, frame):
        rowPixels = len(frame.raw) // frame.height // 4
        bgra = np.frombuffer(frame.raw, np.uint8).reshape(frame.height, rowPixels, 4)
        bgr = np.ascontiguousarray(bgra[:, : frame.width, :3])
        result, _ = self.engine(bgr)
        if not result:
            return []
        # Each item: [box (4 points), text, score]. Group into rows, then left to right.
        result.sort(key=lambda r: (round(min(p[1] for p in r[0]) / 12), min(p[0] for p in r[0])))
        return [
            OcrLine(text, max(p[1] for p in box) - min(p[1] for p in box), min(p[1] for p in box), min(p[0] for p in box))
            for box, text, _ in result
        ]
