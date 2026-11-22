"""Spot simple spelling rules in a prompt and check answers against them locally.

Covers letter counts ("spelled with three letters", "5-letter", "at least 6 letters"),
first/last letter ("starts with B", "ends with a vowel") and letter inclusion
("with the letter Z", "without the letter E"). When a rule is unclear, no check is made:
a wrong filter would hide valid answers.
"""

import re

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
VOWELS = set("aeiou")


def _number(token):
    return int(token) if token.isdigit() else NUMBER_WORDS[token]


def _letters(answer):
    return re.sub(r"[^a-z]", "", answer.lower())


COUNT_RULES = [
    (rf"\b(?:at least|minimum of|no fewer than)\s+{NUM}[- ]letters?", lambda n: lambda c: c >= n, ">= {}"),
    (rf"\b{NUM}\s+(?:or more|\+)\s*letters?|\b{NUM}[- ]letters?\s+or\s+more", lambda n: lambda c: c >= n, ">= {}"),
    (rf"\b(?:at most|no more than|maximum of|up to)\s+{NUM}[- ]letters?", lambda n: lambda c: c <= n, "<= {}"),
    (rf"\b{NUM}\s+or\s+(?:fewer|less)\s+letters?|\b{NUM}[- ]letters?\s+or\s+(?:fewer|less)", lambda n: lambda c: c <= n, "<= {}"),
    (rf"\b(?:more than|over|longer than)\s+{NUM}[- ]letters?", lambda n: lambda c: c > n, "> {}"),
    (rf"\b(?:fewer than|less than|under|shorter than)\s+{NUM}[- ]letters?", lambda n: lambda c: c < n, "< {}"),
    (rf"\b{NUM}[- ]letters?\b", lambda n: lambda c: c == n, "== {}"),
]
EDGE_RULE = r"\b(starts?|starting|begins?|beginning|ends?|ending)\s+with\s+(?:the\s+letter\s+|an?\s+(?!(?:and|or|but)\b)(?=\S{2})|the\s+)?[\"']?([a-z]+)[\"']?"
WITHOUT_RULE = r"\b(?:without|no|not containing|doesn'?t contain|does not contain)\s+(?:the\s+|an?\s+)?letter\s+[\"']?([a-z])\b"
WITH_RULE = r"\b(?:contains?|containing|with|has|having)\s+(?:the\s+|an?\s+)?letter\s+[\"']?([a-z])\b"


def parseConstraints(prompt):
    """Return a list of (description, check(answer) -> bool)."""
    text = prompt.lower().replace("’", "'")
    rules = []

    for pattern, makeCheck, label in COUNT_RULES:
        match = re.search(pattern, text)
        if match:
            n = _number(next(g for g in match.groups() if g))
            countCheck = makeCheck(n)
            rules.append((f"letters {label.format(n)}", lambda a, f=countCheck: f(len(_letters(a)))))
            break

    for verb, target in re.findall(EDGE_RULE, text):
        atStart = verb.startswith(("start", "begin"))
        if target in ("vowel", "consonant"):
            wantVowel = target == "vowel"
            rules.append(
                (f"{'starts' if atStart else 'ends'} with a {target}",
                 lambda a, s=atStart, v=wantVowel: bool(_letters(a)) and ((_letters(a)[0 if s else -1] in VOWELS) == v))
            )
        elif len(target) <= 2:
            rules.append(
                (f"{'starts' if atStart else 'ends'} with {target!r}",
                 lambda a, s=atStart, t=target: _letters(a).startswith(t) if s else _letters(a).endswith(t))
            )

    for letter in re.findall(WITHOUT_RULE, text):
        rules.append((f"no {letter!r}", lambda a, x=letter: x not in _letters(a)))
    withoutStripped = re.sub(EDGE_RULE, " ", re.sub(WITHOUT_RULE, " ", text))
    for letter in re.findall(WITH_RULE, withoutStripped):
        rules.append((f"has {letter!r}", lambda a, x=letter: x in _letters(a)))
    return rules


def failedRules(answer, rules):
    return [label for label, check in rules if not check(answer)]
