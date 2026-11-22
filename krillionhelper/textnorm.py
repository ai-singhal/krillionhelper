"""Clean OCR text, pull out a category prompt, and compare prompts."""

import re
import unicodedata
from difflib import SequenceMatcher

PUNCT_MAP = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"',
        "–": "-", "—": "-", "−": "-",
        " ": " ", "…": "...",
    }
)
EDGE_JUNK = " \t_~*•·|=+#^`-:;,"
# Common OCR misreads of the first word of a prompt.
STARTER_FIXES = {"narne": "name", "nane": "name", "narme": "name", "nme": "name", "namne": "name", "nam": "name"}
DIGIT_IN_WORD = {"0": "o", "1": "l", "5": "s"}
TRAILING_NOISE = re.compile(
    r"(\s+(\d{1,2}:\d{2}|\d+\s*(s|sec|secs|pts|points)?|x\d+|\d+/\d+))+$", re.IGNORECASE
)
# Game UI that sits right under the prompt card and must never be read as a prompt:
# the turn line ("madz is up", OCR'd as "E madz* Is up") and the answer box placeholder.
UI_LINE = re.compile(r"^\W*\S{1,3}?\W*\s*\S+\*?\s+[il1|][s5]\.?\s*u[pd]\b|your\s*puffer|type\s*an?\s*answer|\bis\s+up\b", re.IGNORECASE)
MAX_PROMPT_CHARS = 120
MAX_PROMPT_WORDS = 14  # real category prompts are short; long text is something else on screen


def selectPromptLines(lines, headers=(), starters=(), ignore=()):
    """Pick the prompt out of everything OCR saw in the region. Returns [text].

    The prompt is one block of lines: right under the "CATEGORY N" header (or starting at a
    line with a starter word such as "Name"), left-aligned with each other, about the same
    size, with no big gap between them. Chat, the answer box, buttons and player names sit
    elsewhere and are dropped, so typing an answer doesn't look like a new prompt.

    lines: [OcrLine] top to bottom. Engines without positions (Tesseract) return everything.
    """
    ignore = [text.lower() for text in ignore]
    lines = [line for line in lines if not any(text in line.text.lower() for text in ignore) and not UI_LINE.search(line.text)]
    if not lines:
        return []
    if any(line.top is None or not line.height for line in lines):
        return [line.text for line in lines]

    headerIdx = max((i for i, line in enumerate(lines) if _lastHeader(line.text, headers)), default=-1)
    if headerIdx >= 0:
        header = lines[headerIdx]
        first = next(
            (
                line
                for line in lines[headerIdx + 1 :]
                if line.top > header.top
                and line.top - (header.top + header.height) <= 2 * max(header.height, line.height)
                and abs(line.left - header.left) <= 3 * line.height
            ),
            None,
        )
        anchor = [header.text]
    else:
        starterPattern = "|".join(re.escape(s) for s in sorted(starters, key=len, reverse=True))
        first = next(
            # The starter must open the line: "Game. Name an answer..." is instructions, not a prompt.
            (line for line in lines if starterPattern and re.match(rf"\W*(?:{starterPattern})\b", _fixStarter(line.text), re.IGNORECASE)),
            None,
        )
        anchor = []
    if first is None:
        return anchor

    block = [first]
    for line in lines[lines.index(first) + 1 :]:
        previous = block[-1]
        gap = line.top - (previous.top + previous.height)
        if line.top <= previous.top or gap > 0.9 * first.height:
            if line.top <= previous.top:
                continue  # something to the side on the same row
            break
        if not 0.6 <= line.height / first.height <= 1.6 or abs(line.left - first.left) > 3 * first.height:
            continue  # different text style or column; keep looking for the next prompt line
        block.append(line)
    return anchor + [line.text for line in block]


def normalizeOcrText(lines):
    """Join OCR lines and fix obvious noise. Returns a single clean line."""
    text = " ".join(line.strip() for line in lines if line and line.strip())
    text = unicodedata.normalize("NFKC", text).translate(PUNCT_MAP)
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"(?<![A-Za-z])\|(?![A-Za-z])", "I", text)  # lone pipe -> I
    text = re.sub(r"(?<=[A-Za-z])\||\|(?=[A-Za-z])", "l", text)  # pipe inside word -> l
    text = re.sub(r"\b[A-Za-z]*[015][A-Za-z015]*\b", _fixDigitsInWord, text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([!?,;:])\1+", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(EDGE_JUNK + ".")


def _fixDigitsInWord(match):
    word = match.group(0)
    letters = sum(ch.isalpha() for ch in word)
    digits = len(word) - letters
    if letters < 2 or digits == 0 or digits >= letters:
        return word  # a real number, a year, or too ambiguous
    return "".join(DIGIT_IN_WORD.get(ch, ch) for ch in word)


def extractPrompt(text, starters, headers=(), requireStarter=True):
    """Return the category prompt inside `text`, or None if it doesn't look like one.

    If a header such as "CATEGORY 1" is on screen, everything after it is the prompt.
    Otherwise the prompt must start with one of `starters` ("Name", "List", ...).
    """
    if not text:
        return None
    header = _lastHeader(text, headers)
    if header:
        prompt = _fixStarter(text[header.end():].strip(EDGE_JUNK + "."))
        minWords = 2
        if requireStarter:
            starterPattern = "|".join(re.escape(s) for s in sorted(starters, key=len, reverse=True))
            if not re.match(rf"\W*(?:{starterPattern})\b", prompt, re.IGNORECASE):
                return None
    else:
        text = _fixStarter(text)
        starterPattern = "|".join(re.escape(s) for s in sorted(starters, key=len, reverse=True))
        match = re.search(rf"\b(?:{starterPattern})\b", text, re.IGNORECASE)
        if not match:
            return None
        prompt = text[match.start():]
        minWords = 3
    # A "?" or "!" ends the prompt; anything after it is other UI text.
    end = re.search(r"[?!]", prompt)
    if end:
        prompt = prompt[: end.end()]
    prompt = TRAILING_NOISE.sub("", prompt).strip(EDGE_JUNK)
    if not minWords <= len(prompt.split()) <= MAX_PROMPT_WORDS or len(prompt) > MAX_PROMPT_CHARS:
        return None
    return prompt[0].upper() + prompt[1:]


def _lastHeader(text, headers):
    """Find the last header like "CATEGORY 1" (OCR often reads letter-spaced text as "C A T E ...")."""
    last = None
    for word in headers:
        # Pixel fonts often get the last letter wrong ("CATEGOR ¥"), so it is optional.
        spaced = r"\s?".join(re.escape(ch) for ch in word[:-1]) + rf"(?:\s?\S)?"
        pattern = rf"\b{spaced}(?=\s|$)\s*[#:]?\s*(?:[0-9]{{1,2}}|[Il|](?=\s|$))?"
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if not last or match.start() > last.start():
                last = match
    return last


def _fixStarter(text):
    first, sep, rest = text.partition(" ")
    fixed = STARTER_FIXES.get(first.lower().strip(EDGE_JUNK))
    return fixed.capitalize() + sep + rest if fixed else text


def promptKey(prompt):
    """Cache key: lowercase words only."""
    text = unicodedata.normalize("NFKC", prompt).translate(PUNCT_MAP).lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def isSamePrompt(a, b):
    """True when two prompts differ only by OCR noise, not by meaning."""
    if not a or not b:
        return False
    keyA, keyB = promptKey(a), promptKey(b)
    if keyA == keyB:
        return True
    if keyB.startswith(keyA):
        return False  # text is still being revealed: "Name a fru" -> "Name a fruit"
    if keyA.startswith(keyB):
        return True  # text is fading out or got clipped
    if SequenceMatcher(None, keyA, keyB).ratio() >= 0.92:
        return True
    wordsA, wordsB = keyA.split(), keyB.split()
    if len(wordsA) != len(wordsB):
        return False
    return all(wa == wb or SequenceMatcher(None, wa, wb).ratio() >= 0.75 for wa, wb in zip(wordsA, wordsB))


def isNearDuplicate(answer, existing):
    """The game auto-corrects near-spellings, so "hotstenia" is "hofstenia" and "Nemertean" is
    "nemertean worm" for our purposes. existing: iterable of answers already listed."""
    key = re.sub(r"[^a-z0-9]", "", answer.lower())
    for other in existing:
        otherKey = re.sub(r"[^a-z0-9]", "", other.lower())
        if key == otherKey:
            return True
        shorter, longer = sorted((key, otherKey), key=len)
        if len(shorter) >= 5 and longer.startswith(shorter):
            return True
        if min(len(key), len(otherKey)) >= 5 and SequenceMatcher(None, key, otherKey).ratio() >= 0.85:
            return True
    return False
