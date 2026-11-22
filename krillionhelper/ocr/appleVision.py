"""macOS OCR through Apple's Vision framework (VNRecognizeTextRequest)."""

import objc
import Quartz
import Vision

from .line import OcrLine


class AppleVisionOcr:
    name = "Apple Vision"

    def __init__(self):
        self.colorSpace = Quartz.CGColorSpaceCreateDeviceRGB()

    def recognize(self, frame):
        with objc.autorelease_pool():
            image = self._toCgImage(frame)
            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
            request = Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(True)
            request.setRecognitionLanguages_(["en-US"])
            ok, error = handler.performRequests_error_([request], None)
            if not ok:
                raise RuntimeError(f"Vision OCR failed: {error}")
            pieces = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                box = observation.boundingBox()  # normalized, origin bottom-left
                top = 1 - (box.origin.y + box.size.height)
                pieces.append(
                    (top * frame.height, box.size.height * frame.height, box.origin.x * frame.width,
                     box.size.width * frame.width, str(candidates[0].string()))
                )
            return groupIntoLines(pieces)

    def _toCgImage(self, frame):  # noqa: D102 (BGRA bytes -> CGImage, no copy through PNG)
        rowBytes = len(frame.raw) // frame.height
        provider = Quartz.CGDataProviderCreateWithData(None, frame.raw, len(frame.raw), None)
        return Quartz.CGImageCreate(
            frame.width,
            frame.height,
            8,
            32,
            rowBytes,
            self.colorSpace,
            Quartz.kCGBitmapByteOrder32Little | Quartz.kCGImageAlphaNoneSkipFirst,
            provider,
            None,
            False,
            Quartz.kCGRenderingIntentDefault,
        )


def groupIntoLines(pieces):
    """Vision can split one text line into several boxes. Merge boxes that sit on the same
    baseline, are about the same size and are close together; a button level with the
    prompt but far to the side ("CHAT") stays its own line.

    pieces: (top, height, left, width, text) in pixels. Returns [OcrLine] top to bottom.
    """
    rows = []
    for top, height, left, width, text in sorted(pieces):
        center, right = top + height / 2, left + width
        for row in rows:
            sameLine = abs(center - row["center"]) < max(height, row["height"]) * 0.5
            sameSize = 0.6 <= height / row["height"] <= 1.67
            gap = max(left - row["right"], row["left"] - right)
            if sameLine and sameSize and gap < 2.5 * max(height, row["height"]):
                row["items"].append((left, text))
                row["left"], row["right"] = min(row["left"], left), max(row["right"], right)
                row["top"], row["height"] = min(row["top"], top), max(row["height"], height)
                break
        else:
            rows.append({"center": center, "top": top, "height": height, "left": left, "right": right, "items": [(left, text)]})
    rows.sort(key=lambda r: (r["top"], r["left"]))
    return [
        OcrLine(" ".join(text for _, text in sorted(row["items"])), row["height"], row["top"], row["left"])
        for row in rows
    ]
