"""Rerank answers with TypeSafe's Jev decision model (calibrated typed decisions, ~100ms).

For every candidate answer Jev returns:
- pValid: probability the game's checker accepts it (true/false question),
- rarity: how few players would think of it (score 0 = almost everyone ... 4 = almost nobody).
It also finds which of our answers mean the same thing as a guess that was just played,
because the game auto-corrects small typos and treats synonyms as the same answer.
"""

import os
import threading

try:
    import httpx2 as httpx
except ImportError:
    import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
RARITY_LEVELS = ["Almost every player says it", "Many players", "Some players", "Few players", "Almost nobody"]
GAME = (
    "Puffer Party, a turn-based category word game. Players take turns typing an answer to the prompt; "
    "an answer can't be used twice. The game's checker auto-corrects small misspellings and treats "
    "synonyms and spelling variants as the same answer."
)
NONE_OPTION = "none of these"


class JevRanker:
    def __init__(self, model="jev-latest", apiKey=None):
        self.model = model
        self.apiKey = apiKey or os.environ.get("TYPESAFE_API_KEY")
        self.client = httpx.Client(timeout=5.0, limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120))
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.apiKey)

    def _ask(self, state, questions):
        response = self.client.post(
            API_URL,
            headers={"Authorization": f"Bearer {self.apiKey}", "Content-Type": "application/json"},
            json={"model": self.model, "state": state, "questions": questions},
        )
        response.raise_for_status()
        return response.json()["answers"]

    def score(self, prompt, answers, rules=None):
        """{answer: (pValid, rarity 0-4)} for each answer, in one request."""
        if not answers:
            return {}
        state = {"prompt": prompt, "game": GAME}
        if rules:
            state["learned checker rules"] = rules
        questions = {}
        for i, answer in enumerate(answers):
            questions[f"v{i}"] = {
                "type": "noul",
                "instructions": f"Would the game's answer checker accept `{answer}` as an answer to the `prompt`, "
                "including every rule in the prompt?",
            }
            questions[f"r{i}"] = {
                "type": "score",
                "instructions": f"How many players would think of `{answer}` for this `prompt`?",
                "criteria": RARITY_LEVELS,
            }
        result = self._ask(state, questions)
        return {answer: (result[f"v{i}"]["noul"], result[f"r{i}"]["score"]) for i, answer in enumerate(answers)}

    def sameAs(self, prompt, guess, answers, threshold=0.35):
        """Answers the game would treat as the same answer as `guess` (synonym, variant, near-spelling)."""
        if not answers:
            return []
        criteria = {answer: None for answer in answers}
        criteria[NONE_OPTION] = "No listed answer means the same thing"
        result = self._ask(
            {"prompt": prompt, "game": GAME, "answer just played": guess},
            {
                "same": {
                    "type": "choice",
                    "instructions": "Which listed answer would the game treat as the same answer as `answer just played` "
                    "(same thing, synonym, spelling variant or near-misspelling)?",
                    "criteria": criteria,
                }
            },
        )
        probabilities = result["same"]["probabilities"]
        return [a for a in answers if probabilities.get(a, 0) >= threshold]


def rankKey(pValid, rarity, commonness=0.0):
    """Lower sorts first. Rarity leads; validity scales it. Likely-rejected answers and answers
    almost everyone says (rarity < 1 of 4) sink below everything else."""
    value = (rarity / 4) * (0.4 + 0.6 * pValid) - commonness
    if pValid < 0.35:
        value -= 2
    if rarity < 1:
        value -= 1
    return -value
