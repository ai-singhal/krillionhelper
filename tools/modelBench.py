"""Compare answer models on speed and correctness.

Speed: time from sending the request to the first usable answer line (what the page shows first),
and to the last line. Add ~0.2s for capture + OCR to get screen-to-answer time.
Correctness: every shown answer is graded by a strong judge model.

Usage: .venv/bin/python tools/modelBench.py   (needs ANTHROPIC_API_KEY)
"""

import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from krillionhelper.answers import AnswerGenerator  # noqa: E402

CONFIGS = [
    {"name": "Haiku 5.5, thinking off", "model": "claude-haiku-5-5"},
    {"name": "Sonnet 5.5, thinking off", "model": "claude-sonnet-5-5"},
    {"name": "Sonnet 5.5, thinking off, low effort", "model": "claude-sonnet-5-5", "effort": "low"},
    {"name": "Opus 5.5, low effort", "model": "claude-opus-5-5", "effort": "low"},
    {"name": "Opus 5.5, low effort, fast mode", "model": "claude-opus-5-5", "effort": "low", "fastMode": True},
]
PROMPTS = [
    "Name a body part spelled with three letters",
    "Name a fruit",
    "Name an animal that starts with the letter B",
    "Name a country in Africa",
    "Name a word that rhymes with cat",
    "Name a US state with exactly four letters",
    "Name a breakfast food",
    "Name a color that ends with the letter N",
    "Name a dog breed",
    "Name a sport played with a ball",
]
# $ per million tokens (input, output), standard / fast.
PRICES = {
    ("claude-haiku-5-5", False): (0.10, 0.50),
    ("claude-sonnet-5-5", False): (2.0, 10.0),
    ("claude-opus-5-5", False): (4.0, 20.0),
    ("claude-opus-5-5", True): (8.0, 40.0),
}
JUDGE_MODEL = "claude-opus-5-5"
JUDGE_SYSTEM = """You are a strict referee for a turn-based category word game. Players type answers to a category prompt. An answer is accepted only if it is a real, specific thing (it has a dictionary or Wikipedia entry), it is spelled correctly (missing accents are fine), and it clearly and unambiguously fits the category, including every rule in the prompt such as letter counts or starting letters. Reject answers that are made up, misspelled, too generic to count as a member of the category, arguable, or that break a rule. List every rejected answer with a short reason."""
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"answer": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["answer", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rejected"],
    "additionalProperties": False,
}


def runOne(gen, prompt):
    marks = {}
    start = time.perf_counter()
    answers = []
    for answer in gen.stream(prompt, lambda: False, onFirstToken=lambda: marks.setdefault("text", time.perf_counter())):
        answers.append(answer)
        marks.setdefault("first", time.perf_counter())
    end = time.perf_counter()
    usage = gen.lastUsage
    return {
        "prompt": prompt,
        "answers": answers,
        "firstTextMs": round((marks["text"] - start) * 1000) if "text" in marks else None,
        "firstAnswerMs": round((marks["first"] - start) * 1000) if "first" in marks else None,
        "allMs": round((end - start) * 1000),
        "dropped": gen.droppedCount,
        "usage": {"in": usage.input_tokens, "out": usage.output_tokens} if usage else None,
    }


def judge(client, prompt, answers):
    if not answers:
        return []
    listing = "\n".join(answers)
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=16000,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": f"Category prompt: {prompt}\n\nAnswers:\n{listing}"}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["rejected"]


def main():
    generators = []
    for cfg in CONFIGS:
        gen = AnswerGenerator(cfg["model"], 20, effort=cfg.get("effort"), fastMode=cfg.get("fastMode", False), quiet=True)
        gen.warmUp()
        generators.append((cfg, gen))

    results = {cfg["name"]: [] for cfg in CONFIGS}
    for i, prompt in enumerate(PROMPTS):
        order = generators[i % len(generators):] + generators[: i % len(generators)]  # rotate to spread network noise
        for cfg, gen in order:
            try:
                r = runOne(gen, prompt)
            except anthropic.APIError as e:
                r = {"prompt": prompt, "answers": [], "error": f"{type(e).__name__}: {e}"}
            results[cfg["name"]].append(r)
            print(f"{cfg['name']:38s} {prompt[:40]:40s} first={r.get('firstAnswerMs')}ms all={r.get('allMs')}ms n={len(r['answers'])} {r.get('error', '')}", flush=True)

    judgeClient = anthropic.Anthropic(max_retries=3)
    jobs = [(name, r) for name, rows in results.items() for r in rows if r["answers"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        verdicts = pool.map(lambda job: judge(judgeClient, job[1]["prompt"], job[1]["answers"]), jobs)
        for (name, r), rejected in zip(jobs, verdicts):
            shown = {a.lower() for a in r["answers"]}
            r["rejected"] = [x for x in rejected if x["answer"].lower() in shown]

    Path(__file__).with_name("bench_results.json").write_text(json.dumps(results, indent=2))
    print(f"\n{'config':38s} {'first answer':>14s} {'all answers':>12s} {'shown':>6s} {'wrong':>6s} {'$/prompt':>9s}")
    for cfg in CONFIGS:
        rows = [r for r in results[cfg["name"]] if r.get("firstAnswerMs")]
        firsts = sorted(r["firstAnswerMs"] for r in rows)
        alls = [r["allMs"] for r in rows]
        shown = sum(len(r["answers"]) for r in rows)
        wrong = sum(len(r.get("rejected", [])) for r in rows)
        priceIn, priceOut = PRICES[(cfg["model"], cfg.get("fastMode", False))]
        costs = [(r["usage"]["in"] * priceIn + r["usage"]["out"] * priceOut) / 1e6 for r in rows if r.get("usage")]
        print(
            f"{cfg['name']:38s} {statistics.median(firsts):6.0f}ms (max {max(firsts)}) {statistics.median(alls):8.0f}ms "
            f"{shown:6d} {wrong:4d} ({100 * wrong / max(shown, 1):.0f}%) {statistics.mean(costs) if costs else float('nan'):9.4f}"
        )


if __name__ == "__main__":
    main()
