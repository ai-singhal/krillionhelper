"""Text color sampling and Krillion tier colors.

Accepted answers show in their rarity tier's color (grey Plankton, blue Schooler, red Rare,
purple Deep Cut, gold One in a Krillion). The tracker samples each line's ink color and learns
the exact on-screen color of each tier whenever the tier name is written out.
"""

import colorsys

import numpy as np

# Starting points; replaced by colors learned from the screen (see GameTracker.learnTierColor).
DEFAULT_TIER_COLORS = {
    "PLANKTON": (150, 155, 165),
    "SCHOOLER": (80, 150, 255),
    "RARE": (235, 75, 75),
    "DEEP CUT": (170, 95, 235),
    "ONE IN A KRILLION": (240, 190, 60),
}


def inkColor(frame, line):
    """Average RGB of the text pixels in a line's box: the pixels farthest from the box's
    background (its median color)."""
    if line.top is None or line.left is None or not line.width or not line.height:
        return None
    rowBytes = len(frame.raw) // frame.height
    pixels = np.frombuffer(frame.raw, np.uint8).reshape(frame.height, rowBytes // 4, 4)
    top, left = max(int(line.top), 0), max(int(line.left), 0)
    box = pixels[top : top + int(line.height), left : left + int(line.width), :3]
    if box.size < 30:
        return None
    flat = box.reshape(-1, 3).astype(np.int16)
    background = np.median(flat, axis=0)
    distance = np.abs(flat - background).sum(axis=1)
    ink = flat[distance >= np.percentile(distance, 85)]
    b, g, r = ink.mean(axis=0)  # mss frames are BGRA
    return (int(r), int(g), int(b))


def isPlainText(rgb):
    """White or near-white text (normal typing) is not a tier color."""
    r, g, b = (c / 255 for c in rgb)
    _, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    return saturation < 0.15 and value > 0.82


def tierForColor(rgb, tierColors, maxDistance=70):
    """The tier whose color is closest to `rgb`, if it's close enough."""
    if rgb is None or isPlainText(rgb):
        return None
    best, bestDistance = None, maxDistance
    for tier, color in tierColors.items():
        distance = sum((a - b) ** 2 for a, b in zip(rgb, color)) ** 0.5
        if distance < bestDistance:
            best, bestDistance = tier, distance
    return best
