"""Watch loop, prompt lifecycle and shared display state."""

import json
import threading
import time
import traceback

import anthropic
import mss
import mss.tools

from .answers import AnswerError, AnswerGenerator, makeClient
from .constraints import failedRules, parseConstraints
from .jev import JevRanker, rankKey
from .rarity import frequencyRarity
from .cache import AnswerCache
from .capture import RegionWatcher
from .config import DEFAULT_CONFIG_PATH, PROJECT_DIR, saveConfig
from .ocr import createOcrEngine
from .textnorm import extractPrompt, isNearDuplicate, isSamePrompt, normalizeOcrText, promptKey, selectPromptLines
from .tracker import GameTracker, normalizeGuess

CACHE_PATH = PROJECT_DIR / "answers.sqlite3"
GAME_DB_PATH = PROJECT_DIR / "game.sqlite3"
TRACK_INTERVAL_SECONDS = 0.3
# Rewrite the learned checker rules after this many new labels (verdicts and attempts).
DISTILL_EVERY_LABELS = 5
LATENCY_LOG = PROJECT_DIR / "latency.log"
KEEP_WARM_SECONDS = 30


class TriviaApp:
    def __init__(self, cfg, publish, configPath=DEFAULT_CONFIG_PATH):
        self.cfg = cfg
        self.configPath = configPath
        self.regionVersion = 0  # bumped when the region changes; the watch loop rebuilds its capture
        self.screenSnapshot = None  # last full-screen capture for picking a region on the page
        self.publish = publish  # callable(jsonString) -> pushes to every open page
        self.lock = threading.Lock()
        self.stopEvent = threading.Event()
        self.generation = 0
        self.lastOcrPrompt = None
        self.lastApiUseAt = 0.0
        self.cache = AnswerCache(CACHE_PATH)
        self.tracker = GameTracker(GAME_DB_PATH, cfg["trackIgnoreBottom"], cfg.get("playerName"))
        if cfg.get("playerName"):
            self.tracker.knownPlayers.add(cfg["playerName"].lower())
        self.client = None
        self.jev = JevRanker(cfg["jevModel"]) if cfg.get("rerank") else None
        if self.jev and not self.jev.enabled:
            print("[jev] TYPESAFE_API_KEY not set: ranking by the model's own order", flush=True)
            self.jev = None
        # Ask for spares: answers other players take, or the checker rejects, get replaced from these.
        self.candidateCount = cfg["answerCount"] + cfg["spareAnswers"]
        client = self.client = makeClient()  # one connection pool for every model
        self.generator = AnswerGenerator(
            cfg["model"], self.candidateCount, cfg["thinking"], cfg["effort"], cfg["fastMode"], client=client
        )
        # On prompts with a spelling rule, Opus thinks first and is slow; a fast model streams
        # rule-checked answers right away and the main model fills the remaining slots.
        ruleModel = cfg.get("ruleModel")
        self.ruleGenerator = (
            AnswerGenerator(ruleModel, self.candidateCount, client=client) if ruleModel and ruleModel != cfg["model"] else None
        )
        self.state = {
            "id": 0,
            "prompt": "",
            "source": "",
            "answers": [],
            "answerCount": cfg["answerCount"],
            "status": "waiting",
            "cached": False,
            "error": None,
            "latency": None,
            "ocrEngine": None,
            "ocrText": "",
            "model": cfg["model"],
            "hasRegion": bool(cfg.get("region")),
            "hasGameRegion": bool(cfg.get("gameRegion")),
            "taken": [],  # guesses other players made this round: [{text, player}]
            "rejected": [],  # normalized answers the checker rejected for this prompt
            "accepted": [],
            "stats": self.tracker.stats(),
            "insights": self.tracker.latestInsights()[0],  # checker rules learned from past games
            "gold": [],  # answers proven for you on this prompt and unused by others
        }

    # ---------- state ----------

    def snapshot(self):
        with self.lock:
            return json.dumps(self.state)

    def _update(self, **changes):
        with self.lock:
            self.state.update(changes)
            data = json.dumps(self.state)
        self.publish(data)

    # ---------- lifecycle ----------

    def start(self, watch=True):
        threading.Thread(target=self._keepWarm, name="keepWarm", daemon=True).start()
        if watch:
            # Create OCR engines here, one at a time: macOS's Quartz/Vision bindings load lazily
            # and two threads loading them at once crash (KeyError: CGColorSpaceCreateDeviceRGB).
            try:
                self.promptOcr, self.trackOcr = createOcrEngine(), createOcrEngine()
            except RuntimeError as e:
                self._update(status="error", error=str(e))
                return
            threading.Thread(target=self._watchLoop, name="watch", daemon=True).start()
            threading.Thread(target=self._trackLoop, name="track", daemon=True).start()
        threading.Thread(target=self._distillLoop, name="distill", daemon=True).start()

    def stop(self):
        self.stopEvent.set()

    def _keepWarm(self):
        while not self.stopEvent.is_set():
            if time.time() - self.lastApiUseAt >= KEEP_WARM_SECONDS:
                try:
                    self.generator.warmUp()
                    self.lastApiUseAt = time.time()
                    if self.state["status"] == "error" and not self.state["prompt"]:
                        self._update(status="waiting", error=None)
                except anthropic.APIError as e:
                    self._update(status="error", error=f"API check failed: {describeApiError(e)}")
            self.stopEvent.wait(5)

    # ---------- prompts ----------

    def submitPrompt(self, prompt, source, timing=None, force=False):
        prompt = prompt.strip()
        if not prompt:
            return
        timing = dict(timing or {})
        timing.setdefault("changedAt", time.time())
        key = promptKey(prompt)
        cached = None if force else self.cache.get(key)
        self.tracker.startRound(prompt)
        verdicts = self.tracker.feedbackFor(prompt)
        gold = self.tracker.goldAnswers(prompt)
        with self.lock:
            self.generation += 1
            generation = self.generation
            self.state.update(
                id=generation,
                prompt=prompt,
                source=source,
                answers=[],
                status="loading",
                cached=False,
                error=None,
                latency=None,
                taken=[] if not isSamePrompt(self.state["prompt"], prompt) else self.state["taken"],
                rejected=verdicts["rejected"],
                accepted=verdicts["accepted"],
                gold=gold,
            )
            if cached:
                # Re-rank cached answers with what other players have said since (common ones sink).
                counts = self.tracker.guessCounts(prompt)
                cached = sorted(
                    cached, key=lambda a: cached.index(a) / max(len(cached), 1) + 0.15 * counts.get(normalizeGuess(a), 0)
                )
                timing["firstAnswerAt"] = timing["doneAt"] = time.time()
                cached = list(dict.fromkeys(gold + cached))  # proven answers first
                self.state.update(
                    answers=cached[: self.candidateCount],
                    status="done",
                    cached=True,
                    latency=latencySummary(timing),
                )
            data = json.dumps(self.state)
        self.publish(data)
        if cached:
            self._logLatency(prompt, source, timing, cached=True, answerCount=len(cached))
            return
        threading.Thread(
            target=self._generate, args=(generation, prompt, key, source, timing), daemon=True
        ).start()

    def regenerate(self):
        with self.lock:
            prompt, source = self.state["prompt"], self.state["source"]
        if prompt:
            self.submitPrompt(prompt, source, force=True)

    def _generate(self, generation, prompt, key, source, timing):
        count = self.candidateCount
        notes = self._learningNotes(prompt)
        # Each answer's rank: Jev's calibrated validity and rarity once scored, until then an estimate
        # from its position in its model's rarest-first list. Proven answers ("bank") stay on top.
        answers, seen, errors = [], set(), []
        info = {}
        positions = {}
        timesGuessed = self.tracker.guessCounts(prompt)  # learned from other players: common answers sink
        repeatRate = self.tracker.repeatRateByRarity()  # learned from the game: bands that keep getting "already said"
        rules = parseConstraints(prompt)
        learnedRules, _ = self.tracker.latestInsights()
        pendingJev = []
        dropped = []
        dropBelow = self.cfg["dropBelowValidity"]
        jevWake = threading.Event()
        streamsDone = threading.Event()

        hasRule = bool(rules)
        hedgeDelay = 0 if hasRule else (self.cfg.get("hedgeAfterMs") or 0) / 1000
        mainStarted = threading.Event()

        def cancelled():
            return self.generation != generation

        def sortKey(answer):
            item = info[answer]
            commonness = 0.15 * timesGuessed.get(normalizeGuess(answer), 0)
            # Rarity = average of Jev's judgment (or, until Jev answers, the model's own order) and
            # how often the word turns up in written text, which is how Krillion scores many prompts.
            if item["jev"]:
                pValid, rarity = item["jev"]
            else:
                pValid, rarity = 0.75, 4 * (1 - min(item["position"], 1))
            if item["freq"] is not None:
                rarity = (rarity + item["freq"]) / 2
            if item["source"] == "bank":
                # Proven for you here: validity is certain, but it still has to be rare to rank high.
                pValid = 1.0
                rarity = item["jev"][1] if item["jev"] else 2.5
            commonness += 0.5 * repeatRate.get(min(int(rarity), 3), 0)
            return (rankKey(pValid, rarity, commonness), item["position"])

        def show(newAnswers, model):
            with self.lock:
                if cancelled():
                    return
                added = False
                for answer in newAnswers:
                    position = positions.get(model, 0)
                    positions[model] = position + 1
                    if len(answers) < count and answer.lower() not in seen and not isNearDuplicate(answer, answers):
                        seen.add(answer.lower())
                        info[answer] = {"source": model, "position": position / count, "jev": None,
                                        "freq": frequencyRarity(answer)}
                        answers.append(answer)
                        added = True
                        pendingJev.append(answer)
                        if "firstAnswerAt" not in timing and model != "bank":
                            timing["firstAnswerAt"] = time.time()
                            timing["firstModel"] = model
                if not added:
                    return
                answers.sort(key=sortKey)
                self.state.update(answers=list(answers), latency=latencySummary(timing))
                data = json.dumps(self.state)
            jevWake.set()
            self.publish(data)

        def rerankWithJev():
            """Score answers in small batches as they stream in, then re-sort the list."""
            while not cancelled():
                jevWake.wait(0.1)
                jevWake.clear()
                with self.lock:
                    batch = pendingJev[:]
                    pendingJev.clear()
                if not batch:
                    if streamsDone.is_set():
                        return
                    continue
                try:
                    scores = self.jev.score(prompt, batch, learnedRules)
                except Exception as e:
                    print(f"[jev] rerank failed, keeping the model's order: {e}", flush=True)
                    return
                with self.lock:
                    if cancelled():
                        return
                    for answer, (pValid, rarity) in scores.items():
                        if rules and not failedRules(answer, rules):
                            pValid = max(pValid, 0.5)  # letter rules are checked locally; Jev misreads them
                        info[answer]["jev"] = (pValid, rarity)
                        if pValid < dropBelow and answer in answers:  # proven answers too: stored data can be wrong
                            answers.remove(answer)  # clearly wrong: better a shorter list than junk
                            dropped.append(answer)
                    answers.sort(key=sortKey)
                    timing.setdefault("rankedAt", time.time())
                    self.state.update(answers=list(answers))
                    data = json.dumps(self.state)
                self.publish(data)

        def firstToken():
            timing.setdefault("firstTokenAt", time.time())

        def consumeMain():
            try:
                for answer in self.generator.stream(prompt, cancelled, firstToken, notes):
                    mainStarted.set()
                    show([answer], self.generator.model)
            except Exception as e:  # reported below if nothing at all came back
                errors.append(e)
                if not isinstance(e, (AnswerError, anthropic.APIError)):
                    traceback.print_exc()
            finally:
                mainStarted.set()

        def consumeBackup():
            held = []
            deadline = timing["requestAt"] + hedgeDelay
            live = hedgeDelay == 0
            try:
                for answer in self.ruleGenerator.stream(prompt, lambda: cancelled() or (not live and mainHasAnswers()), firstToken, notes):
                    if live:
                        show([answer], self.ruleGenerator.model)
                    elif time.time() >= deadline:
                        live = True
                        show(held + [answer], self.ruleGenerator.model)
                        held = []
                    else:
                        held.append(answer)
                if held and not live:
                    # The backup finished before the deadline: show its answers then, unless the main model is on screen.
                    if not mainStarted.wait(max(0.0, deadline - time.time())) or not mainHasAnswers():
                        show(held, self.ruleGenerator.model)
            except Exception as e:
                errors.append(e)

        def mainHasAnswers():
            return timing.get("firstModel") == self.generator.model

        timing["requestAt"] = time.time()
        self.lastApiUseAt = timing["requestAt"]
        gold = self.tracker.goldAnswers(prompt)
        if gold:
            show(gold, "bank")  # answers that already worked for you here, unused by others
        workers = [consumeMain] + ([consumeBackup] if self.ruleGenerator else [])
        threads = [threading.Thread(target=work, daemon=True) for work in workers]
        ranker = threading.Thread(target=rerankWithJev, daemon=True) if self.jev else None
        if ranker:
            ranker.start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        streamsDone.set()
        jevWake.set()
        if ranker:
            ranker.join(timeout=3)  # so the cached list is in ranked order
        self.lastApiUseAt = time.time()

        if cancelled():
            return
        if not answers and errors:
            e = errors[0]
            message = describeApiError(e) if isinstance(e, anthropic.APIError) else str(e)
            return self._fail(generation, message)
        for e in errors:
            print(f"[answers] {type(e).__name__}: {e}", flush=True)
        timing["doneAt"] = time.time()
        with self.lock:
            if cancelled():
                return
            self.state.update(status="done", latency=latencySummary(timing))
            data = json.dumps(self.state)
        self.publish(data)
        if answers:  # narrow categories can legitimately have fewer than answerCount
            try:
                self.cache.put(key, prompt, answers, self.cfg["model"])
            except Exception as e:  # a cache problem must never stop tracking and logging
                print(f"[cache] write failed: {e}", flush=True)
            if dropped:
                print(f"[jev] dropped as clearly wrong: {dropped}", flush=True)
            self.tracker.recordCandidates(prompt, [
                {"answer": a, "rank": i, "source": info[a]["source"], "position": info[a]["position"],
                 "pValid": (info[a]["jev"] or (None, None))[0], "rarity": (info[a]["jev"] or (None, None))[1],
                 "freqRarity": info[a]["freq"]}
                for i, a in enumerate(answers)
            ])
        self._logLatency(prompt, source, timing, cached=False, answerCount=len(answers))

    def _fail(self, generation, message):
        with self.lock:
            if self.generation != generation:
                return
            self.state.update(status="error", error=message)
            data = json.dumps(self.state)
        self.publish(data)
        print(f"[answers] error: {message}", flush=True)

    def _logLatency(self, prompt, source, timing, cached, answerCount):
        summary = latencySummary(timing)
        entry = {"at": time.time(), "prompt": prompt, "source": source, "cached": cached, "answers": answerCount,
                 "firstModel": timing.get("firstModel")}
        entry.update(summary)
        with LATENCY_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        print(
            f"[latency] {prompt!r} ({source}{', cached' if cached else ''}): "
            + ", ".join(f"{k}={v}" for k, v in summary.items()),
            flush=True,
        )

    # ---------- learning from games ----------

    def _learningNotes(self, prompt):
        """Context for the model from earlier games: what's taken, what's common, what the checker refused."""
        with self.lock:
            taken = [t["text"] for t in self.state["taken"]]
        verdicts = self.tracker.feedbackFor(prompt)
        common = self.tracker.commonGuesses(prompt)
        lessons = self.tracker.lessons()
        others = self.tracker.otherPlayersRecent()
        rules, _ = self.tracker.latestInsights()
        sections = []
        if rules:
            sections.append("What this game's checker accepts and rejects, learned from earlier games:\n" + rules)
        if taken:
            sections.append("Already taken this round: " + ", ".join(taken))
        if common:
            sections.append("Players often say these for this prompt (too common, skip them): " + ", ".join(common))
        if verdicts["rejected"]:
            sections.append("The game rejected these for this prompt: " + ", ".join(verdicts["rejected"]))
        if lessons["rejected"]:
            sections.append(
                "Answers the game's checker rejected recently (prompt -> answer); avoid answers like these:\n"
                + "\n".join(f'- "{p}" -> {a}' for p, a in lessons["rejected"])
            )
        if others:
            sections.append(
                "Answers other players gave recently in this game (prompt -> answer); shows what the checker lets through:\n"
                + "\n".join(f'- "{p}" -> {a}' for p, a in others)
            )
        if lessons["accepted"]:
            sections.append(
                "Rare answers the checker accepted recently (prompt -> answer):\n"
                + "\n".join(f'- "{p}" -> {a}' for p, a in lessons["accepted"])
            )
        return "Notes from earlier games:\n" + "\n\n".join(sections) if sections else None

    def recordFeedback(self, answer, verdict):
        with self.lock:
            prompt = self.state["prompt"]
        if not prompt or verdict not in ("accepted", "rejected"):
            return
        self.tracker.recordFeedback(prompt, answer, verdict)
        verdicts = self.tracker.feedbackFor(prompt)
        self._update(rejected=verdicts["rejected"], accepted=verdicts["accepted"], stats=self.tracker.stats())
        print(f"[learn] {verdict}: {answer!r} for {prompt!r}", flush=True)

    def recordMyAnswer(self, answer):
        self.tracker.recordMyAnswer(answer)

    def _trackLoop(self):
        """Read the whole game window every half second and record guesses, turns and players left."""
        ocr, watcher, watcherVersion = self.trackOcr, None, -1
        starters, headers = self.cfg["promptStarters"], self.cfg["promptHeaders"]
        while not self.stopEvent.is_set():
            try:
                if watcherVersion != self.regionVersion:
                    if watcher:
                        watcher.close()
                        watcher = None
                    if self.cfg.get("gameRegion"):
                        watcher = RegionWatcher(self.cfg["gameRegion"], self.cfg["changeThreshold"], self.cfg["retinaCapture"])
                    watcherVersion = self.regionVersion
                if watcher is None:
                    self.stopEvent.wait(1)
                    continue
                frame = watcher.grab()
                if watcher.changed(frame):
                    lines = ocr.recognize(frame)
                    promptLines = selectPromptLines(lines, headers, starters, self.cfg["ignoreText"])
                    with self.lock:
                        ownAnswers = list(self.state["answers"])
                    result = self.tracker.observe(lines, frame.height, promptLines, ownAnswers)
                    if result["guesses"]:
                        self._onGuesses(result["guesses"])
                    if result["labels"]:
                        self._onLabels(result["labels"])
            except Exception:
                traceback.print_exc()
                self.stopEvent.wait(2)
            self.stopEvent.wait(TRACK_INTERVAL_SECONDS)
        if watcher:
            watcher.close()

    def _onGuesses(self, found):
        """Guesses that went through (yours or other players'): hide them from the list."""
        with self.lock:
            prompt = self.state["prompt"]
            self.state.update(taken=self.state["taken"] + found, stats=self.tracker.stats())
            data = json.dumps(self.state)
        self.publish(data)
        for guess in found:
            print(f"[track] guess {guess['text']!r} by {guess['player']} for {prompt!r}", flush=True)
        if self.jev:
            threading.Thread(target=self._hideSynonyms, args=(prompt, found), daemon=True).start()

    def _hideSynonyms(self, prompt, found):
        """The game treats synonyms and near-spellings as the same answer, so those are gone too."""
        for guess in found:
            with self.lock:
                if self.state["prompt"] != prompt:
                    return
                gone = {normalizeGuess(t["text"]) for t in self.state["taken"]}
                candidates = [a for a in self.state["answers"] if normalizeGuess(a) not in gone][:40]
            try:
                same = self.jev.sameAs(prompt, guess["text"], candidates)
            except Exception as e:
                print(f"[jev] synonym check failed: {e}", flush=True)
                return
            if not same:
                continue
            extra = [{"text": a, "player": guess["player"], "sameAs": guess["text"]} for a in same]
            with self.lock:
                if self.state["prompt"] != prompt:
                    return
                self.state.update(taken=self.state["taken"] + extra)
                data = json.dumps(self.state)
            self.publish(data)
            print(f"[jev] hid {same} (same answer as {guess['text']!r})", flush=True)

    def _onLabels(self, labels):
        with self.lock:
            prompt = self.state["prompt"]
        verdicts = self.tracker.feedbackFor(prompt) if prompt else {"accepted": [], "rejected": []}
        self._update(accepted=verdicts["accepted"], rejected=verdicts["rejected"], stats=self.tracker.stats())
        for answer, verdict in labels:
            print(f"[learn] auto {verdict}: {answer!r} for {prompt!r}", flush=True)

    def _distillLoop(self):
        """Every few new labels, have Claude rewrite the checker rules from all the evidence so far."""
        while not self.stopEvent.wait(20):
            try:
                count = self.tracker.labelCount()
                _, basedOn = self.tracker.latestInsights()
                if count - basedOn < DISTILL_EVERY_LABELS:
                    continue
                rules = distillRules(self.client, self.cfg["learnModel"], *self.tracker.distillExamples())
                if rules:
                    self.tracker.saveInsights(rules, count)
                    self._update(insights=rules)
                    print(f"[learn] rules updated from {count} labels:\n{rules}", flush=True)
            except anthropic.APIError as e:
                print(f"[learn] rules update failed: {describeApiError(e)}", flush=True)
            except Exception:
                traceback.print_exc()

    # ---------- region selection from the page ----------

    def takeSnapshot(self):
        """Capture every display so the page can show it for drag-selecting the region."""
        with mss.MSS() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            png = mss.tools.to_png(shot.rgb, shot.size)
        self.screenSnapshot = png
        return {key: monitor[key] for key in ("left", "top", "width", "height")}

    def setRegion(self, region, kind="prompt"):
        """kind "prompt": the prompt card (read fast). kind "game": the whole game window (tracking)."""
        region = {key: int(region[key]) for key in ("left", "top", "width", "height")}
        if region["width"] < 12 or region["height"] < 12:
            raise ValueError("region too small")
        self.cfg["region" if kind == "prompt" else "gameRegion"] = region
        saveConfig(self.cfg, self.configPath)
        self.screenSnapshot = None  # don't keep a screenshot around longer than needed
        with self.lock:
            self.regionVersion += 1
            self.lastOcrPrompt = None
        self._update(hasRegion=bool(self.cfg.get("region")), hasGameRegion=bool(self.cfg.get("gameRegion")), ocrText="")
        print(f"[watch] new {kind} region {region} saved to {self.configPath}", flush=True)

    # ---------- screen watching ----------

    def _watchLoop(self):
        ocr = self.promptOcr
        self._update(ocrEngine=ocr.name)
        print(f"[watch] OCR engine: {ocr.name}; region {self.cfg.get('region')}", flush=True)

        watcher, watcherVersion = None, -1
        interval = self.cfg["captureIntervalMs"] / 1000
        starters, headers = self.cfg["promptStarters"], self.cfg["promptHeaders"]
        lastError = None
        nextTick = time.monotonic()
        try:
            while not self.stopEvent.is_set():
                try:
                    if watcherVersion != self.regionVersion or watcher is None:
                        if watcher:
                            watcher.close()
                            watcher = None
                        if self.cfg.get("region"):
                            watcher = RegionWatcher(self.cfg["region"], self.cfg["changeThreshold"], self.cfg["retinaCapture"])
                        watcherVersion = self.regionVersion
                    if watcher is None:
                        self.stopEvent.wait(0.3)  # no region yet: pick one on the page
                        continue
                    frame = watcher.grab()
                    if watcher.changed(frame):
                        lines = ocr.recognize(frame)
                        ocrDoneAt = time.time()
                        text = normalizeOcrText(selectPromptLines(lines, headers, starters, self.cfg["ignoreText"]))
                        if text != self.state["ocrText"]:
                            self._update(ocrText=text)
                        prompt = extractPrompt(text, starters, headers, self.cfg["requireStarter"])
                        if prompt and self._isNewPrompt(prompt):
                            self.lastOcrPrompt = prompt
                            self.submitPrompt(
                                prompt, "ocr", {"changedAt": frame.capturedAt, "ocrDoneAt": ocrDoneAt}
                            )
                    lastError = None
                except Exception as e:  # one bad frame must not stop the loop
                    if str(e) != lastError:
                        traceback.print_exc()
                        lastError = str(e)
                nextTick += interval
                delay = nextTick - time.monotonic()
                if delay < 0:
                    nextTick = time.monotonic()
                    delay = 0
                self.stopEvent.wait(delay)
        finally:
            if watcher:
                watcher.close()

    def _isNewPrompt(self, prompt):
        if isSamePrompt(self.lastOcrPrompt, prompt):
            return False
        if isSamePrompt(self.state["prompt"], prompt):
            # Already showing it (typed manually, or same as last round).
            self.lastOcrPrompt = prompt
            return False
        return True


DISTILL_SYSTEM = """You study the answer checker of a category word game (Puffer Party). Players take turns typing an answer to a prompt like "Name a fruit"; the checker accepts or rejects each answer, and an answer can't be reused.

Write the rules that best predict what the checker accepts and rejects: spelling and variants, plurals, brand or trade names, multi-word or descriptive answers, abbreviations, how strictly it reads the category, and how obscure an answer can be before the checker stops recognizing it.

Rules:
- The checker auto-corrects small misspellings and maps synonyms and spelling variants to one answer; account for that.
- Describe the checker only. Give no strategy advice; the player's strategy is fixed (the rarest answers the checker still accepts).
- Other players' text was read off the screen mid-typing, so a "final" attempt may be cut off ("straw" for "strawberry"). Never infer a rule from a cut-off word.
- Only state a rule that the examples support, and give your confidence (high, medium or low) with the number of supporting examples.
- At most 8 short lines, one rule per line, no preamble."""


def distillRules(client, model, verdicts, attempts):
    if not verdicts and not attempts:
        return None
    lines = ["Verdicts on the player's answers (prompt | answer | verdict | how we know):"]
    lines += [f"{p} | {a} | {v} | {'player marked it' if s == 'me' else 'inferred from the turn'}" for p, a, v, s in verdicts]
    lines += ["", "Other players' attempts (final = the turn moved on after it, so it was most likely accepted; "
              "abandoned = typed in full and then replaced, so it was likely rejected):"]
    lines += [f"{p} | {t} | {o}" for p, t, o in attempts]
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        output_config={"effort": "medium"},
        system=DISTILL_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


def latencySummary(timing):
    def ms(end, start):
        if end in timing and start in timing:
            return round((timing[end] - timing[start]) * 1000)
        return None

    summary = {
        "ocrMs": ms("ocrDoneAt", "changedAt"),
        "firstTokenMs": ms("firstTokenAt", "requestAt"),
        "firstAnswerMs": ms("firstAnswerAt", "changedAt"),
        "allAnswersMs": ms("doneAt", "changedAt"),
    }
    return {k: v for k, v in summary.items() if v is not None}


def describeApiError(error):
    if isinstance(error, anthropic.AuthenticationError):
        return "ANTHROPIC_API_KEY was rejected (401)"
    if isinstance(error, anthropic.NotFoundError):
        return "Model not found (404). Check \"model\" in config.json"
    if isinstance(error, anthropic.RateLimitError):
        return "Rate limited (429). Try again in a moment"
    if isinstance(error, anthropic.APIStatusError):
        return f"API error {error.status_code}: {error.message}"
    if isinstance(error, anthropic.APIConnectionError):
        return "Can't reach the Anthropic API (network)"
    return str(error)
