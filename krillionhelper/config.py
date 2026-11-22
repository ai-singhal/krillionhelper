"""Load and save config.json (screen region plus tunables)."""

import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"

DEFAULTS = {
    # Screen regions in global screen points: {"left", "top", "width", "height"}.
    # region: the prompt card (read every captureIntervalMs). gameRegion: the whole game window,
    # read every 0.5s to record other players' guesses, turns and players left.
    "region": None,
    "gameRegion": None,
    # "party" (Puffer Party) or "daily" (the daily Krillion game: looks up today's published answers).
    "mode": "party",
    # Your in-game name, so it is never mistaken for another player's guess.
    "playerName": None,
    # Ignore the bottom part of the game window when looking for guesses (your own typing box).
    "trackIgnoreBottom": 0.22,
    "port": 8765,
    # Benchmarked 2026-11-22 (tools/modelBench.py): Opus 5.5 at low effort in fast mode had the
    # fewest wrong answers (1 of 181) at ~0.7s to the first answer; Haiku 5.5 was 0.4s with 6 of 162 wrong.
    "model": "claude-opus-5-5",
    "effort": "low",
    "fastMode": True,
    # Backup model, started together with the main one. Its answers are shown when the main model
    # has no answer yet after hedgeAfterMs, and right away on prompts with a spelling rule
    # ("three letters", "starts with B"), where the main model thinks first and takes 2-6s.
    # The main model's answers fill the remaining slots. Set ruleModel to null to turn this off.
    "ruleModel": "claude-haiku-5-5",
    "hedgeAfterMs": 600,
    # Rerank answers with TypeSafe's Jev decision model (needs TYPESAFE_API_KEY): rarest answers
    # the checker likely accepts go first. Validity AUC 0.90 against judged answers (2026-11-22).
    "rerank": True,
    # Only near-certain junk is dropped (Jev is weak on franchise characters, so a higher cut
    # loses real answers); unsure ones (validity under ~0.55) stay but sink to the bottom.
    "dropBelowValidity": 0.1,
    "jevModel": "jev-latest",
    # Writes the learned checker rules from accumulated verdicts (in the background, not per prompt).
    "learnModel": "claude-opus-5-5",
    # Thinking off keeps time-to-first-answer low. Opus 5.5 can't turn it off; low effort keeps it short.
    "thinking": False,
    "answerCount": 30,
    # Extra answers requested so taken or rejected ones can be replaced without asking again.
    "spareAnswers": 10,
    "captureIntervalMs": 300,
    # macOS: true captures full Retina pixels. 1x read the real game card fine and is faster.
    "retinaCapture": False,
    # Fraction of sampled pixels that must change before OCR runs again.
    "changeThreshold": 0.002,
    # Text after one of these headers is the prompt (Puffer Party shows "CATEGORY 1").
    "promptHeaders": ["category"],
    # Every Puffer Party prompt starts with "Name", so require it even under the header
    # (stops "Your puffer..." and text from other windows becoming prompts).
    "requireStarter": True,
    # Without a header, a prompt must start with one of these. Keep this list short: looser
    # words ("give", "say") match ordinary chat text if another window covers the region.
    "promptStarters": ["name"],
    # Lines containing any of these are never a prompt (game instructions, this app's own page).
    "ignoreText": [
        "pass the puffer", "OCR missed it", "Screen text:", "first answer", "Watching the screen",
        "Asking Claude", "Streaming", "Taken:", "learned:", "Monitor event", "Apple Vision",
    ],
}


def loadConfig(path=DEFAULT_CONFIG_PATH):
    path = Path(path)
    cfg = dict(DEFAULTS)
    if path.exists():
        with path.open() as f:
            cfg.update(json.load(f))
    return cfg


def saveConfig(cfg, path=DEFAULT_CONFIG_PATH):
    path = Path(path)
    # Only save what differs from the defaults, so improved defaults still apply later.
    changed = {key: value for key, value in cfg.items() if DEFAULTS.get(key) != value}
    with path.open("w") as f:
        json.dump(changed, f, indent=2)
        f.write("\n")
