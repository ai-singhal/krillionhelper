"""End-to-end latency test: show prompts on screen, time how long until the app has answers.

1. .venv/bin/python tools/latencyHarness.py --write-config tests/harness_config.json
2. .venv/bin/python -m krillionhelper --config tests/harness_config.json --no-open   (another terminal)
3. .venv/bin/python tools/latencyHarness.py

Latency here is measured from the moment the prompt is drawn on screen (true screen change),
so it includes the wait for the next 300ms capture tick.
"""

import argparse
import json
import statistics
import sys
import time
import tkinter as tk
import urllib.request

import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from krillionhelper.config import DEFAULTS  # noqa: E402
from krillionhelper.textnorm import isSamePrompt  # noqa: E402

WINDOW = {"left": 60, "top": 140, "width": 680, "height": 210}
PANEL = (24, 20, 656, 190)  # panel box inside the window (x0, y0, x1, y1), like the game's prompt card
PROMPTS = [
    "Name a body part spelled with three letters",
    "Name a fruit",
    "Name a country in Africa",
    "Name an animal that starts with the letter B",
    "Name a famous painter",
    "Name a body part spelled with three letters",  # repeat: should come from the cache
]
ASSETS = Path(__file__).resolve().parent.parent / "tests" / "assets"
STATE_URL = "http://127.0.0.1:{port}/api/state"


class GameLookalike:
    """Draws a Puffer Party style prompt card: pixel font, letter-spaced pink header,
    scanlines, and an animated water background so pixels change every frame."""

    def __init__(self, width, height):
        from PIL import ImageFont

        self.width, self.height = width, height
        self.promptFont = ImageFont.truetype(str(ASSETS / "VT323-Regular.ttf"), 38)
        self.headerFont = ImageFont.truetype(str(ASSETS / "VT323-Regular.ttf"), 20)
        rng = np.random.default_rng(7)
        self.water = rng.integers(0, 28, size=(height + 64, width + 64), dtype=np.uint8)
        self.card = None

    def setCard(self, category, prompt):
        from PIL import Image, ImageDraw

        if prompt is None:
            self.card = None
            return
        x0, y0, x1, y1 = PANEL
        card = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)
        draw.rectangle(PANEL, fill=(27, 32, 45, 236))
        draw.text((x0 + 22, y0 + 16), "   ".join("CATEGORY") + "    " + str(category), font=self.headerFont, fill=(232, 87, 126, 255))
        y = y0 + 52
        for line in self._wrap(prompt, x1 - x0 - 44):
            draw.text((x0 + 22, y), line, font=self.promptFont, fill=(236, 240, 246, 255))
            y += 44
        self.card = card

    def frame(self, t):
        from PIL import Image

        shift = int(t * 30) % 64
        noise = self.water[shift : shift + self.height, shift // 2 : shift // 2 + self.width].astype(np.int16)
        rgb = np.stack([38 + noise // 2, 66 + noise, 98 + noise], axis=2).clip(0, 255).astype(np.uint8)
        image = Image.fromarray(rgb, "RGB").convert("RGBA")
        if self.card is not None:
            image.alpha_composite(self.card)
        pixels = np.asarray(image.convert("RGB")).copy()
        pixels[::3] = (pixels[::3] * 0.82).astype(np.uint8)  # CRT scanlines over everything
        return Image.fromarray(pixels, "RGB")

    def _wrap(self, text, maxWidth):
        lines, current = [], ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if self.promptFont.getlength(trial) <= maxWidth:
                current = trial
            else:
                lines.append(current)
                current = word
        return lines + [current]


def writeConfig(path):
    """Region = the prompt card, the way you'd drag-select it in the real game."""
    x0, y0, x1, y1 = PANEL
    cfg = dict(DEFAULTS)
    cfg["region"] = {"left": WINDOW["left"] + x0, "top": WINDOW["top"] + y0, "width": x1 - x0, "height": y1 - y0}
    Path(path).write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"wrote {path} with region {cfg['region']}")


def fetchState(port):
    with urllib.request.urlopen(STATE_URL.format(port=port), timeout=2) as r:
        return json.loads(r.read())


def run(port, gapSeconds):
    from PIL import ImageTk

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.update()  # macOS Tk ignores a borderless window's position until it has been drawn
    root.overrideredirect(True)  # and the first window in a process can regain a title bar
    root.geometry(f"{WINDOW['width']}x{WINDOW['height']}+{WINDOW['left']}+{WINDOW['top']}")
    label = tk.Label(root, borderwidth=0, highlightthickness=0)
    label.pack()
    scene = GameLookalike(WINDOW["width"], WINDOW["height"])
    started = time.time()

    def draw():
        photo = ImageTk.PhotoImage(scene.frame(time.time() - started))
        label.configure(image=photo)
        label.image = photo
        root.update_idletasks()
        root.update()

    def show(category, prompt):
        scene.setCard(category, prompt)
        draw()
        return time.time()

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            draw()
            time.sleep(0.04)

    show(0, None)
    pump(2.0)
    results = []
    for category, prompt in enumerate(PROMPTS, start=1):
        show(category, None)  # card hidden between rounds
        pump(gapSeconds)
        shownAt = show(category, prompt)
        firstAt = doneAt = None
        state = {}
        while time.time() - shownAt < 10:
            draw()
            state = fetchState(port)
            if isSamePrompt(state.get("prompt"), prompt) and isSamePrompt(prompt, state.get("prompt")):
                if firstAt is None and state["answers"]:
                    firstAt = time.time()
                if state["status"] in ("done", "error"):
                    doneAt = time.time()
                    break
            time.sleep(0.015)
        results.append(
            {
                "prompt": prompt,
                "ocrRead": state.get("prompt"),
                "firstAnswerMs": round((firstAt - shownAt) * 1000) if firstAt else None,
                "allAnswersMs": round((doneAt - shownAt) * 1000) if doneAt else None,
                "answers": len(state.get("answers", [])),
                "cached": state.get("cached"),
                "status": state.get("status"),
                "error": state.get("error"),
                "appLatency": state.get("latency"),
                "sample": state.get("answers", [])[:5],
            }
        )
        print(json.dumps(results[-1]), flush=True)
    root.destroy()

    fresh = [r["firstAnswerMs"] for r in results if r["firstAnswerMs"] and not r["cached"]]
    if fresh:
        print(
            f"\nfresh prompts: first answer median {statistics.median(fresh)} ms, "
            f"min {min(fresh)} ms, max {max(fresh)} ms (n={len(fresh)})"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-config")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--gap", type=float, default=2.5, help="seconds of 'Get ready' between prompts")
    args = parser.parse_args()
    if args.write_config:
        writeConfig(args.write_config)
    else:
        run(args.port, args.gap)
