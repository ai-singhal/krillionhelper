"""Fallback OCR through Tesseract (needs the tesseract binary, pytesseract and Pillow)."""

import shutil

import pytesseract
from PIL import Image

from .line import OcrLine


class TesseractOcr:
    name = "Tesseract"

    def __init__(self):
        if not shutil.which("tesseract"):
            raise RuntimeError("tesseract binary not found on PATH")

    def recognize(self, frame):
        rowBytes = len(frame.raw) // frame.height
        image = Image.frombuffer("RGB", (frame.width, frame.height), frame.raw, "raw", "BGRX", rowBytes, 1)
        text = pytesseract.image_to_string(image, config="--psm 6")
        return [OcrLine(line) for line in text.splitlines() if line.strip()]
