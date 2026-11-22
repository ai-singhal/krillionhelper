"""Stream ranked answers for a category prompt from Claude."""

import re

import anthropic

from .constraints import failedRules, parseConstraints

try:
    import httpx2 as httpx  # anthropic >= 1.0
except ImportError:
    import httpx

# Keep the HTTPS connection to the API open between prompts (httpx default is 5s),
# so a new prompt doesn't pay for a fresh TLS handshake.
KEEPALIVE_SECONDS = 120

SYSTEM_PROMPT = """You supply answers for Puffer Party, a fast turn-based category game. Players take turns typing an answer to a category prompt such as "Name a fruit" or "Name a body part spelled with three letters". An answer someone already gave can't be used again, so the common answers are gone within a few turns. The player wants answers nobody else in the lobby would think of.

Reply with {count} different answers that fit the category.

Goal: rare, niche answers. Rank them strictly from rarest to most common: the first line is the answer the fewest people would ever think of, and each line after it is a little more familiar. Leave out the handful of answers everyone says first unless the category is so narrow that nothing else is left.

Every answer must still be accepted by the game's checker:
- a real, specific thing with its own dictionary or Wikipedia entry
- spelled correctly in its standard English form, in plain ASCII letters without accents
- clearly and unambiguously in the category, including every rule in the prompt (letter counts, starting or ending letters, and so on). Obscure is good; doubtful is not. If you are not sure an answer fits, leave it out.
- as short as possible to type: the shortest common name, usually one or two words, with no articles, qualifiers or notes in parentheses

The game auto-corrects small misspellings and treats synonyms and spelling variants as the same answer. So give at most one answer per meaning, and skip answers that are just another name or spelling for a common answer: the game counts them as that common answer, which is usually taken already.

The user may add notes from earlier games: answers already taken this round, answers players usually give for this prompt, and answers the game's checker rejected or accepted before. Never repeat a taken, common or rejected answer, and learn from the rejections what kind of answer the checker refuses.

Aim for {count}, but quality beats count: stop early rather than add answers you doubt. Never pad the list with answers that break a rule or are a stretch.

The prompt text was read from the screen with OCR, so it may contain a small typo. Answer the category it obviously means.

Output one answer per line and nothing else. Every line you write is shown to the player as an answer, so write no reasoning, checks, comments, numbering, bullets, headings or blank lines."""

LIST_MARKER = re.compile(r"^\s*(?:\d{1,2}\s*[.):\-]|[-*•·]|\(\d{1,2}\))\s*")
# Signs that a line is the model commenting ("Rib - yes", "Pit? not standard") rather than an answer.
COMMENTARY = re.compile(r"\s[-–—]\s|[?:(\[]|^(let me|here|note|okay|ok)\b", re.IGNORECASE)
MAX_ANSWER_WORDS = 6


def cleanAnswer(line):
    """Strip list markers, quotes and trailing punctuation; reject preamble-looking lines."""
    text = LIST_MARKER.sub("", line).strip()
    previous = None
    while text != previous:
        previous = text
        text = text.strip().strip("\"'`*_").rstrip(".,;")
    if not text or len(text) > 60 or len(text.split()) > MAX_ANSWER_WORDS or COMMENTARY.search(text):
        return None
    return text


class AnswerError(Exception):
    pass


def thinkingOff(model):
    """The request field that turns thinking off, which differs by model. None = can't be turned off."""
    if model.startswith(("claude-opus-5-5", "claude-fable", "claude-mythos")):
        return None  # always thinks; low effort keeps it short
    if model.startswith("claude-sonnet-5-5"):
        return {"type": "between_tools"}
    return {"type": "disabled"}


# Models that take the server-side refusal fallback (a refused request is retried on another model).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def makeClient():
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=KEEPALIVE_SECONDS)
    # One retry is enough: a stale answer is worse than a fast error here.
    return anthropic.Anthropic(
        max_retries=1,
        timeout=20.0,
        http_client=anthropic.DefaultHttpxClient(limits=limits, timeout=20.0),
    )


class AnswerGenerator:
    def __init__(self, model, count=20, thinking=False, effort=None, fastMode=False, quiet=False, client=None):
        self.model = model
        self.count = count
        self.thinking = thinking
        self.effort = effort
        self.fastMode = fastMode
        self.quiet = quiet
        self.droppedCount = 0  # lines dropped by the local rule check in the last stream()
        self.client = client or makeClient()

    def warmUp(self):
        """Check the key and model ID, and keep the HTTPS connection open. Cheap (no tokens)."""
        return self.client.models.retrieve(self.model)

    def stream(self, prompt, isCancelled, onFirstToken=None, notes=None):
        """Yield answers one at a time as each line completes. `notes`: learned context from earlier games."""
        rules = parseConstraints(prompt)
        # With a spelling rule we check answers locally, so ask for a few spares.
        requested = self.count + 6 if rules else self.count
        params = {
            "model": self.model,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT.format(count=requested),
            "messages": [{"role": "user", "content": f"{prompt}\n\n{notes}" if notes else prompt}],
        }
        off = None if self.thinking else thinkingOff(self.model)
        if off:
            params["thinking"] = off
        else:
            params["max_tokens"] = 8000  # room for thinking before the answers
        effort = self.effort or ("low" if self.thinking or not off else None)
        if effort:
            params["output_config"] = {"effort": effort}
        if self.fastMode:
            params["speed"] = "fast"

        self.droppedCount = 0
        self.lastUsage = None
        seen = set()
        buffer = ""
        firstToken = True
        if self.model.startswith(FALLBACK_MODELS):
            stream = self.client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            stream = self.client.messages.stream(**params)
        with stream as stream:
            for text in stream.text_stream:
                if isCancelled():
                    return
                if firstToken and onFirstToken:
                    onFirstToken()
                firstToken = False
                buffer += text
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    answer = self._accept(line, seen, rules)
                    if answer:
                        yield answer
                        if len(seen) >= self.count:
                            return
            message = stream.get_final_message()
        self.lastUsage = message.usage
        answer = self._accept(buffer, seen, rules)
        if answer and len(seen) <= self.count:
            yield answer
        if message.stop_reason == "refusal":
            raise AnswerError("Claude declined this prompt")

    def _accept(self, line, seen, rules):
        answer = cleanAnswer(line)
        if not answer or answer.lower() in seen:
            return None
        failed = failedRules(answer, rules)
        if failed:
            self.droppedCount += 1
            if not self.quiet:
                print(f"[answers] dropped {answer!r}: breaks {', '.join(failed)}", flush=True)
            return None
        seen.add(answer.lower())
        return answer
