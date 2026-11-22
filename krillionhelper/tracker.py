"""Watch the whole game window and record what happens: rounds, whose turn it is,
the guesses other players make, and how many players are left.

Everything goes into SQLite (game.sqlite3) so it can be used later:
- guesses seen this round are removed from the answer list,
- guesses seen for the same prompt in earlier games are sent to Claude as "too common".

Guess detection is a heuristic built from what the game shows: a guess is short text that
newly appears during a round, away from the answer box, the prompt card and the HUD.
Raw OCR snapshots are stored too (table observations) so the parser can be improved later.
"""

import json
import re
import sqlite3
import threading
import time
from difflib import SequenceMatcher

from .textnorm import promptKey

IS_UP = re.compile(r"([A-Za-z0-9_.]{2,24})\s+is\s+up\b", re.IGNORECASE)
YOUR_TURN = re.compile(r"your\s*puffer|type\s*an?\s*answer", re.IGNORECASE)  # the answer box when it's your turn
YOU = "you"
# The game's verdict on your answer, e.g. '"turnstile": no echo. keep going'.
VERDICT = re.compile(r'^\W*["“”\'](?P<answer>[^"“”\',\[\]]{1,40})["“”\']\s*[:;.,\-]\s*(?P<message>[A-Za-z][A-Za-z .,:;!\-]{1,60})$')
RESULT = re.compile(r"\bYOU\s+(WIN|WON|LOSE|LOST)\b", re.IGNORECASE)
RATING = re.compile(r"\bRATING\s*(\d{3,4})\s*([+\-−]\s?\d{1,3})?", re.IGNORECASE)
ACCEPTED_MESSAGE = re.compile(r"\bno\s+echo\b|keep\s+going|nice|good|great|correct", re.IGNORECASE)
REJECTED_MESSAGE = re.compile(r"echo|already|taken|used|repeat|invalid|not\s+(a|an|valid|accepted|in)|unknown|try\s+again|nope|wrong", re.IGNORECASE)
# OCR often garbles the turn line ("madz is UD", "ma dz 1s. Up"); never treat those as guesses.
IS_UP_FUZZY = re.compile(r"[il1|][s5]\.?\s*u\s*[pdo]\b|\bup\.{2,}|\.\.\.$", re.IGNORECASE)
FLOATING = re.compile(r"f\s?l\s?o\s?a\s?t\s?i\s?n\s?g\D{0,4}(\d{1,2})\s*/\s*(\d{1,2})", re.IGNORECASE)
UI_TEXT = re.compile(
    r"^(send|chat|players?|floating|unrated|rated|category|round|skip|pass|ready|waiting|lobby|start|leave|exit|settings|menu|"
    r"you|your turn|your puffer|time'?s up|timeout|eliminated|winner|wins?|out|ok|go|from screen)\b",
    re.IGNORECASE,
)
MAX_GUESS_WORDS = 4
MAX_GUESS_CHARS = 32
BURST_LIMIT = 4  # more new lines than this at once is a screen change, not a guess
BASELINE_OBSERVATIONS = 2  # text already on screen in the first frames of a round is UI, not guesses


def samePlayer(a, b):
    """OCR reads one name several ways ("madz", "madza", "madam")."""
    if not a or not b:
        return False
    if a == YOU or b == YOU:
        return a == b
    return SequenceMatcher(None, normalizeGuess(a), normalizeGuess(b)).ratio() >= 0.6


def normalizeGuess(text):
    """Lowercase letters/digits only, with a plain trailing plural "s" dropped, for matching."""
    key = re.sub(r"[^a-z0-9]+", "", text.lower())
    if len(key) > 3 and key.endswith("s") and not key.endswith("ss"):
        key = key[:-1]
    return key


class GameTracker:
    def __init__(self, path, ignoreBottom=0.22, playerName=None):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY, prompt TEXT NOT NULL, promptKey TEXT NOT NULL,
                startedAt REAL NOT NULL, playersLeft INTEGER, playersTotal INTEGER);
            CREATE TABLE IF NOT EXISTS guesses (
                id INTEGER PRIMARY KEY, roundId INTEGER NOT NULL, promptKey TEXT NOT NULL,
                player TEXT, text TEXT NOT NULL, normalized TEXT NOT NULL, seenAt REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS myAnswers (
                id INTEGER PRIMARY KEY, roundId INTEGER, promptKey TEXT, text TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, roundId INTEGER, at REAL NOT NULL, lines TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY, prompt TEXT NOT NULL, promptKey TEXT NOT NULL, answer TEXT NOT NULL,
                normalized TEXT NOT NULL, verdict TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, roundId INTEGER, prompt TEXT, promptKey TEXT, player TEXT,
                text TEXT NOT NULL, normalized TEXT NOT NULL, outcome TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS insights (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, text TEXT NOT NULL, basedOn INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS verdicts (
                id INTEGER PRIMARY KEY, roundId INTEGER, prompt TEXT, answer TEXT NOT NULL,
                message TEXT NOT NULL, label TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY, roundId INTEGER, prompt TEXT NOT NULL, answer TEXT NOT NULL,
                normalized TEXT NOT NULL, finalRank INTEGER, source TEXT, modelPosition REAL,
                pValid REAL, rarity REAL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, roundId INTEGER, result TEXT,
                rating INTEGER, delta INTEGER);
            CREATE INDEX IF NOT EXISTS guessesByPrompt ON guesses (promptKey);
            CREATE INDEX IF NOT EXISTS feedbackByPrompt ON feedback (promptKey);
            """
        )
        self.db.commit()
        self.ignoreBottom = ignoreBottom
        self.playerName = playerName
        self.roundId = None
        self.promptKey = None
        self.prompt = None
        # Player names show up all over the screen (and garbled by OCR); never count them as guesses.
        self.knownPlayers = {p.lower() for (p,) in self.db.execute("SELECT DISTINCT player FROM guesses WHERE player IS NOT NULL AND player != 'you'")}
        if playerName:
            self.knownPlayers.add(playerName.lower())
        self.currentUp = None
        self._resetRoundMemory()

    def _resetRoundMemory(self):
        self.baseline = set()
        self.observationCount = 0
        self.previousTexts = set()
        self.guessedKeys = set()
        self.turnTyped = []  # what the active player has typed so far this turn, oldest first
        self.turnAbandoned = []  # full words typed and then replaced this turn (likely rejected)
        self.myTurnCopies = []  # answers copied from the page during your current turn
        self.verdictsSeen = set()
        self.lastLinesJson = None

    # ---------- writes ----------

    def startRound(self, prompt):
        with self.lock:
            key = promptKey(prompt)
            if key == self.promptKey:
                return self.roundId
            cursor = self.db.execute(
                "INSERT INTO rounds (prompt, promptKey, startedAt) VALUES (?, ?, ?)", (prompt, key, time.time())
            )
            self.db.commit()
            self.roundId, self.promptKey, self.prompt = cursor.lastrowid, key, prompt
            self._resetRoundMemory()
            return self.roundId

    def recordMyAnswer(self, text):
        with self.lock:
            if self.currentUp == YOU:
                self.myTurnCopies.append(text)
            self.db.execute(
                "INSERT INTO myAnswers (roundId, promptKey, text, at) VALUES (?, ?, ?, ?)",
                (self.roundId, self.promptKey, text, time.time()),
            )
            self.db.commit()

    def observe(self, lines, frameHeight, promptTexts=(), ownAnswers=()):
        """Feed one OCR read of the game window. Returns newly seen guesses [{text, player}].

        ownAnswers: what this app's page is showing; if the page sits inside the game area,
        its list must not be mistaken for other players' guesses."""
        now = time.time()
        own = {normalizeGuess(a) for a in ownAnswers}
        ownAnswers = list(ownAnswers)
        lines = [line for line in lines if normalizeGuess(re.sub(r"^\d{1,2}\s+", "", line.text)) not in own]
        promptWords = {w for t in promptTexts for w in re.findall(r"[a-z]+", t.lower())}
        with self.lock:
            linesJson = json.dumps([[line.text, line.height, line.top, line.left] for line in lines])
            if linesJson != self.lastLinesJson:
                self.lastLinesJson = linesJson
                self.db.execute(
                    "INSERT INTO observations (roundId, at, lines) VALUES (?, ?, ?)", (self.roundId, now, linesJson)
                )

            for line in lines:
                floating = FLOATING.search(line.text)
                if floating and self.roundId:
                    self.db.execute(
                        "UPDATE rounds SET playersLeft = ?, playersTotal = ? WHERE id = ?",
                        (int(floating.group(1)), int(floating.group(2)), self.roundId),
                    )

            found, labels = [], []
            for line in lines:
                self._readVerdict(line.text, now, found, labels)
            self._readResult(lines, now)

            allTexts = {cleanGuessText(line.text).lower() for line in lines}
            texts = {}
            for line in lines:
                text = cleanGuessText(line.text)
                # On your turn your typing is in the answer box at the bottom; otherwise ignore that area.
                bottom = 0 if self.currentUp == YOU else self.ignoreBottom
                if not text or not looksLikeGuess(text, line, frameHeight, bottom):
                    continue
                if self._isPlayerName(text) or set(re.findall(r"[a-z]+", text.lower())) <= promptWords:
                    continue
                texts[text.lower()] = text

            self.observationCount += 1
            if self.roundId is None or self.observationCount <= BASELINE_OBSERVATIONS:
                self.baseline |= allTexts
                self.db.commit()
                return {"guesses": [g for g in found if g], "labels": labels}

            # Puffer Party shows the active player's answer live as they type ("pil", "pillo", "pillow").
            # Their guess is whatever they had typed when the turn moved to someone else.
            upNow = self._whoIsUp(lines)
            if upNow and not samePlayer(upNow, self.currentUp):
                if self.currentUp:
                    self._finishTurn(self.currentUp, now, found, labels, ownAnswers)
                self.turnTyped, self.turnAbandoned, self.myTurnCopies = [], [], []
                self.currentUp = upNow
            for text in texts.values():
                if text.lower() in self.baseline or len(re.sub(r"[^A-Za-z]", "", text)) < 2:
                    continue
                if not self.turnTyped or self.turnTyped[-1].lower() != text.lower():
                    previous = self.turnTyped[-1] if self.turnTyped else None
                    if previous and len(normalizeGuess(previous)) >= 4 and not isContinuation(previous, text):
                        self.turnAbandoned.append(previous)
                    self.turnTyped = (self.turnTyped + [text])[-30:]
            self.db.commit()
            return {"guesses": [g for g in found if g], "labels": labels}

    def _readResult(self, lines, now):
        """End-of-game screen: "YOU WIN ... RATING 1247 +16". One row per game."""
        text = " | ".join(line.text for line in lines)
        result, rating = RESULT.search(text), RATING.search(text)
        if not result or not rating:
            return
        value = int(rating.group(1))
        delta = int(re.sub(r"[^\d\-+]", "", rating.group(2).replace("−", "-"))) if rating.group(2) else None
        last = self.db.execute("SELECT at, rating, delta FROM games ORDER BY id DESC LIMIT 1").fetchone()
        if last and now - last[0] < 90 and last[1] == value:
            return  # the same results screen, read again
        outcome = "win" if result.group(1).upper() in ("WIN", "WON") else "loss"
        self.db.execute(
            "INSERT INTO games (at, roundId, result, rating, delta) VALUES (?, ?, ?, ?, ?)",
            (now, self.roundId, outcome, value, delta),
        )

    def _readVerdict(self, text, now, found, labels):
        """'"turnstile": no echo. keep going' = the game's verdict on an answer you played."""
        match = VERDICT.match(text.strip())
        if not match or self.roundId is None:
            return
        answer = cleanGuessText(match.group("answer"))
        message = match.group("message").strip()
        key = normalizeGuess(answer)
        if not key or key in self.verdictsSeen:
            return
        self.verdictsSeen.add(key)
        if ACCEPTED_MESSAGE.search(message):
            label = "accepted"
        elif REJECTED_MESSAGE.search(message):
            label = "rejected"
        else:
            label = "unknown"
        self.db.execute(
            "INSERT INTO verdicts (roundId, prompt, answer, message, label, at) VALUES (?, ?, ?, ?, ?, ?)",
            (self.roundId, self.prompt, answer, message, label, now),
        )
        found.append(self._recordGuess(answer, YOU, now))  # played, so it's gone either way
        if label != "unknown":
            self._insertFeedback(self.prompt, answer, label, "game")
            labels.append((answer, label))

    def _finishTurn(self, player, now, found, labels, ownAnswers=()):
        """The turn moved on: hide what the player played. Verdicts come only from the game's own
        verdict lines (or your ✓/✗), never from reading half-typed text."""
        typed = self._complete(self._finalTyped(), ownAnswers)
        if player == YOU:
            final = self.myTurnCopies[-1] if self.myTurnCopies else typed
            if final:
                found.append(self._recordGuess(final, YOU, now))
            return
        if typed:
            found.append(self._recordGuess(typed, player, now))
            self._insertAttempt(player, typed, "final", now)
        for text in self.turnAbandoned:
            if not typed or normalizeGuess(text) != normalizeGuess(typed):
                self._insertAttempt(player, self._complete(text, ownAnswers), "abandoned", now)

    def _complete(self, text, ownAnswers):
        """OCR often catches a word before the last keystrokes ("appl"); use the full answer when
        exactly one of our answers starts with it."""
        if not text:
            return text
        key = normalizeGuess(text)
        matches = {a for a in ownAnswers if normalizeGuess(a).startswith(key) and normalizeGuess(a) != key}
        return matches.pop() if len(matches) == 1 and len(key) >= 3 else text

    def _insertAttempt(self, player, text, outcome, now):
        self.db.execute(
            "INSERT INTO attempts (roundId, prompt, promptKey, player, text, normalized, outcome, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.roundId, self.prompt, self.promptKey, player, text, normalizeGuess(text), outcome, now),
        )

    def _whoIsUp(self, lines):
        for line in lines:
            if YOUR_TURN.search(line.text):
                return YOU
            up = IS_UP.search(line.text)
            if up:
                if self.playerName and samePlayer(up.group(1), self.playerName):
                    return YOU
                self.knownPlayers.add(up.group(1).lower())
                return up.group(1)
        return None

    def _finalTyped(self):
        """The last thing typed this turn that looks like a word (partial typing is ignored)."""
        for text in reversed(self.turnTyped):
            if len(normalizeGuess(text)) >= 3 and not self._isPlayerName(text):
                return text
        return None

    def _recordGuess(self, text, player, now):
        key = normalizeGuess(text)
        if not key or key in self.guessedKeys:
            return None
        self.guessedKeys.add(key)
        self.db.execute(
            "INSERT INTO guesses (roundId, promptKey, player, text, normalized, seenAt) VALUES (?, ?, ?, ?, ?, ?)",
            (self.roundId, self.promptKey, player, text, key, now),
        )
        return {"text": text, "player": player}

    def recordFeedback(self, prompt, answer, verdict, source="me"):
        """verdict: "accepted" or "rejected" by the game's checker. source "me" (your ✓/✗) beats "auto"."""
        with self.lock:
            self._insertFeedback(prompt, answer, verdict, source)
            self.db.commit()

    def _insertFeedback(self, prompt, answer, verdict, source):
        if not prompt or not answer:
            return
        self.db.execute(
            "INSERT INTO feedback (prompt, promptKey, answer, normalized, verdict, source, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (prompt, promptKey(prompt), answer, normalizeGuess(answer), verdict, source, time.time()),
        )

    def _isPlayerName(self, text):
        key = normalizeGuess(text)
        return any(SequenceMatcher(None, key, normalizeGuess(p)).ratio() >= 0.7 for p in self.knownPlayers)

    # ---------- reads ----------

    def feedbackFor(self, prompt):
        """Latest verdict per answer for this prompt: {"accepted": [...], "rejected": [...]} (normalized)."""
        with self.lock:
            # Your own ✓/✗ (source "me") outranks the automatic guess, whatever came later.
            rows = self.db.execute(
                "SELECT normalized, verdict FROM feedback WHERE promptKey = ? ORDER BY CASE source WHEN 'me' THEN 2 WHEN 'game' THEN 1 ELSE 0 END, at", (promptKey(prompt),)
            ).fetchall()
        latest = dict(rows)
        return {v: sorted(k for k, verdict in latest.items() if verdict == v) for v in ("accepted", "rejected")}

    def lessons(self, limit=12):
        """Recent checker verdicts across all prompts, newest first: {"rejected": [(prompt, answer)], "accepted": [...]}"""
        with self.lock:
            out = {}
            for verdict in ("rejected", "accepted"):
                out[verdict] = self.db.execute(
                    "SELECT prompt, answer FROM feedback WHERE verdict = ? ORDER BY at DESC LIMIT ?", (verdict, limit)
                ).fetchall()
        return out

    def myAnswersThisRound(self):
        with self.lock:
            rows = self.db.execute("SELECT text FROM myAnswers WHERE roundId = ?", (self.roundId,)).fetchall()
        return {normalizeGuess(text): text for (text,) in rows}

    def guessCounts(self, prompt):
        """How often each answer was guessed for this prompt in earlier rounds: {normalized: count}."""
        with self.lock:
            rows = self.db.execute(
                "SELECT normalized, COUNT(*) FROM guesses WHERE promptKey = ? AND roundId != ? GROUP BY normalized",
                (promptKey(prompt), self.roundId or -1),
            ).fetchall()
        return dict(rows)

    def otherPlayersRecent(self, limit=15):
        """Recent guesses other players made on other prompts: (prompt, guess). Mostly accepted answers."""
        with self.lock:
            return self.db.execute(
                "SELECT r.prompt, g.text FROM guesses g JOIN rounds r ON r.id = g.roundId "
                "WHERE g.promptKey != ? ORDER BY g.seenAt DESC LIMIT ?",
                (self.promptKey or "", limit),
            ).fetchall()

    def commonGuesses(self, prompt, limit=25):
        """Guesses other players gave for this prompt in earlier rounds, most frequent first."""
        with self.lock:
            rows = self.db.execute(
                "SELECT MIN(text), COUNT(*) AS n FROM guesses WHERE promptKey = ? AND roundId != ? "
                "GROUP BY normalized ORDER BY n DESC LIMIT ?",
                (promptKey(prompt), self.roundId or -1, limit),
            ).fetchall()
        return [text for text, _ in rows]

    def goldAnswers(self, prompt):
        """Answers the checker accepted from you for this prompt that no other player has used: rare and proven."""
        key = promptKey(prompt)
        with self.lock:
            rows = self.db.execute(
                "SELECT answer, normalized, verdict FROM feedback WHERE promptKey = ? ORDER BY CASE source WHEN 'me' THEN 2 WHEN 'game' THEN 1 ELSE 0 END, at", (key,)
            ).fetchall()
            others = {n for (n,) in self.db.execute("SELECT normalized FROM guesses WHERE promptKey = ? AND player != ?", (key, YOU))}
        latest = {}
        for answer, normalized, verdict in rows:
            latest[normalized] = (answer, verdict)
        return [answer for normalized, (answer, verdict) in latest.items() if verdict == "accepted" and normalized not in others]

    def labelCount(self):
        with self.lock:
            feedback = self.db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            attempts = self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        return feedback + attempts

    def distillExamples(self):
        """Everything the rules writer learns from: verdicts on your answers and other players' attempts."""
        with self.lock:
            verdicts = self.db.execute(
                "SELECT prompt, answer, verdict, source FROM feedback ORDER BY at DESC LIMIT 200"
            ).fetchall()
            attempts = self.db.execute(
                "SELECT prompt, text, outcome FROM attempts WHERE prompt IS NOT NULL ORDER BY at DESC LIMIT 150"
            ).fetchall()
            attempts += [
                (p, a, f"game said: {m}")
                for p, a, m in self.db.execute("SELECT prompt, answer, message FROM verdicts ORDER BY at DESC LIMIT 100")
            ]
        return verdicts, attempts

    def recordCandidates(self, prompt, rows):
        """Every answer we offered this round, with its scores and final rank (rows: dicts)."""
        with self.lock:
            now = time.time()
            self.db.executemany(
                "INSERT INTO candidates (roundId, prompt, answer, normalized, finalRank, source, modelPosition, pValid, rarity, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (self.roundId, prompt, r["answer"], normalizeGuess(r["answer"]), r["rank"], r.get("source"),
                     r.get("position"), r.get("pValid"), r.get("rarity"), now)
                    for r in rows
                ],
            )
            self.db.commit()

    def repeatRateByRarity(self, minSamples=8):
        """Learned from the game's verdicts: how often an answer of each rarity band (Jev 0-4)
        came back "already said". {band: rate}, only for bands with enough evidence."""
        with self.lock:
            rows = self.db.execute("SELECT answer, message, roundId FROM verdicts").fetchall()
            bands = {}
            for answer, message, roundId in rows:
                row = self.db.execute(
                    "SELECT rarity FROM candidates WHERE roundId = ? AND normalized = ? AND rarity IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (roundId, normalizeGuess(answer)),
                ).fetchone()
                if not row:
                    continue
                band = min(int(row[0]), 3)
                total, repeats = bands.get(band, (0, 0))
                lowered = message.lower()
                repeated = "already" in lowered or ("echo" in lowered and "no echo" not in lowered)
                bands[band] = (total + 1, repeats + int(repeated))
        return {band: repeats / total for band, (total, repeats) in bands.items() if total >= minSamples}

    def gameStats(self):
        with self.lock:
            rows = self.db.execute("SELECT result, rating, delta FROM games ORDER BY id").fetchall()
        wins = sum(r[0] == "win" for r in rows)
        return {"games": len(rows), "wins": wins, "rating": rows[-1][1] if rows else None,
                "lastDelta": rows[-1][2] if rows else None}

    def saveInsights(self, text, basedOn):
        with self.lock:
            self.db.execute("INSERT INTO insights (at, text, basedOn) VALUES (?, ?, ?)", (time.time(), text, basedOn))
            self.db.commit()

    def latestInsights(self):
        with self.lock:
            row = self.db.execute("SELECT text, basedOn FROM insights ORDER BY id DESC LIMIT 1").fetchone()
        return row or (None, 0)

    def stats(self):
        with self.lock:
            rounds = self.db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
            guesses = self.db.execute("SELECT COUNT(*) FROM guesses WHERE player != ?", (YOU,)).fetchone()[0]
            mine = self.db.execute("SELECT COUNT(*) FROM myAnswers").fetchone()[0]
            latest = {}
            for key, normalized, verdict in self.db.execute(
                "SELECT promptKey, normalized, verdict FROM feedback ORDER BY CASE source WHEN 'me' THEN 2 WHEN 'game' THEN 1 ELSE 0 END, at"
            ):
                latest[(key, normalized)] = verdict
            attempts = self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            attempts += self.db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
        accepted = sum(v == "accepted" for v in latest.values())
        rejected = sum(v == "rejected" for v in latest.values())
        return {
            "rounds": rounds, "guesses": guesses, "myAnswers": mine, "accepted": accepted, "rejected": rejected,
            "acceptRate": round(100 * accepted / (accepted + rejected)) if accepted + rejected else None,
            "attempts": attempts, **self.gameStats(),
        }


def isContinuation(previous, current):
    """Is `current` the same answer still being typed or corrected ("pil" -> "pillow", "pillw" -> "pillow")?"""
    a, b = normalizeGuess(previous), normalizeGuess(current)
    return b.startswith(a[: max(1, len(a) - 2)]) or a.startswith(b)


def cleanGuessText(text):
    text = re.sub(r"^\W{0,2}\d{1,2}\s+(?=[A-Za-z])", "", text.strip())  # your hearts count before the answer box ("·2 turnstile")
    text = text.strip().strip("\"'`“”‘’").rstrip("!.?…,|").strip()  # "|" is the typing cursor
    text = re.sub(r"^[^A-Za-z0-9]+", "", text)
    return text


def looksLikeGuess(text, line, frameHeight, ignoreBottom):
    if not re.search(r"[A-Za-z]{2}", text):
        return False
    if len(text) > MAX_GUESS_CHARS or len(text.split()) > MAX_GUESS_WORDS:
        return False
    if IS_UP.search(text) or IS_UP_FUZZY.search(text) or FLOATING.search(text) or UI_TEXT.match(text):
        return False
    if re.match(r"^\d{1,2}\s+\S", text):
        return False  # a numbered list line (this app's own page)
    if re.search(r"[\[\]{}<>=]|', '|\bgone:", text):
        return False  # code or log text from another window
    if len(re.findall(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", text)) > 1:
        return False  # letters and digits jumbled together ("1U50") is OCR noise
    if sum(ch.isdigit() for ch in text) >= 0.4 * len(text.replace(" ", "")):
        return False  # mostly digits: scores, timers, HUD ("VW 1231")
    if re.fullmatch(r"[\d\s/:\-x#]+", text):
        return False
    if line.top is not None and frameHeight and line.top > frameHeight * (1 - ignoreBottom):
        return False  # the answer box (your own typing) and buttons
    return True
