"""Daily mode: look up today's published Krillion answer sheet.

Answer sites (GameRant, The Word Finder, ...) publish each day's 7 prompts with answers grouped
by rarity tier, including the "One in a Krillion" answer. Claude finds and reads today's sheet
with its web search and web fetch tools (about 20s, once a day); every prompt read from the
screen is then matched against it instantly.
"""

import datetime
import json
import re
import sqlite3
import threading
import time
from difflib import SequenceMatcher

from .textnorm import promptKey

TIERS = [("krillion", "ONE IN A KRILLION"), ("deepCut", "DEEP CUT"), ("rare", "RARE"), ("schooler", "SCHOOLER")]
SYSTEM = "You find published answer sheets for the Krillion daily word game (krillion.io). Reply with only one JSON object, no other text."
REQUEST = """Today is {long}. Find today's Krillion daily puzzle answers. Answer sites (for example GameRant or thewordfinder.com "Krillion Answers Today") list each day's prompts with answers grouped by rarity tier, including the "One in a Krillion" answer.

Return JSON: {{"date": "YYYY-MM-DD", "source": "url", "prompts": [{{"prompt": "...", "krillion": ["..."], "deepCut": ["..."], "rare": ["..."], "schooler": ["..."]}}]}}
Only include what the page actually lists for {iso}. If you can't find today's sheet, return {{"date": null, "prompts": []}}. Never guess answers."""
TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 4},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4},
]


class DailySheet:
    def __init__(self, client, dbPath, model="claude-opus-5-5"):
        self.client = client
        self.model = model
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(dbPath), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS dailySheets (date TEXT PRIMARY KEY, source TEXT, sheet TEXT NOT NULL, fetchedAt REAL NOT NULL)"
        )
        self.db.commit()
        self.sheet = None

    def load(self, date=None):
        """Today's sheet from the cache, or fetched from the web (blocking, ~20s). Returns it or None."""
        date = date or datetime.date.today()
        with self.lock:
            row = self.db.execute("SELECT sheet FROM dailySheets WHERE date = ?", (date.isoformat(),)).fetchone()
        if row:
            self.sheet = json.loads(row[0])
            return self.sheet
        sheet = self._fetch(date)
        if sheet and sheet.get("date") == date.isoformat() and sheet.get("prompts"):
            with self.lock:
                self.db.execute(
                    "INSERT OR REPLACE INTO dailySheets (date, source, sheet, fetchedAt) VALUES (?, ?, ?, ?)",
                    (date.isoformat(), sheet.get("source"), json.dumps(sheet), time.time()),
                )
                self.db.commit()
            self.sheet = sheet
        return self.sheet

    def _fetch(self, date):
        messages = [{"role": "user", "content": REQUEST.format(long=f"{date:%B} {date.day}, {date.year}", iso=date.isoformat())}]
        response = None
        for _ in range(4):  # a long server-tool turn can pause; send it back to continue
            # Searching and reading a page takes 20-40s: longer than the answer client's timeout.
            response = self.client.with_options(timeout=120.0, max_retries=1).messages.create(
                model=self.model, max_tokens=16000, system=SYSTEM, tools=TOOLS,
                output_config={"effort": "low"}, messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
        if response is None or response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text")
        match = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(match.group(0)) if match else None
        except json.JSONDecodeError:
            return None

    def match(self, prompt):
        """This prompt's entry in today's sheet, if it's there: {"prompt", "krillion", "deepCut", ...}."""
        if not self.sheet:
            return None
        key = promptKey(prompt)
        best, bestScore = None, 0.0
        for entry in self.sheet.get("prompts", []):
            other = promptKey(entry.get("prompt", ""))
            score = SequenceMatcher(None, key, other).ratio()
            if other and (key.startswith(other[:25]) or other.startswith(key[:25])):
                score = max(score, 0.9)  # OCR often cuts long prompts off
            if score > bestScore:
                best, bestScore = entry, score
        return best if bestScore >= 0.7 else None


def tieredAnswers(entry):
    """[(answer, tier)] rarest first, from a sheet entry."""
    out = []
    for field, tier in TIERS:
        for answer in entry.get(field) or []:
            if answer and answer.strip():
                out.append((answer.strip(), tier))
    return out
