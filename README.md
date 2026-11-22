# krillionhelper

Watches Puffer Party (krillion.io) on screen, reads each category prompt and shows the 30 rarest answers the game should still accept at http://localhost:8765. It learns from every game you play. Display only: it never types or clicks in the game.

Unofficial fan tool, not affiliated with or endorsed by Krillion. MIT licensed.

## Setup (once)

```sh
./setup.sh                                  # makes .venv with a native Python (needs uv)
export ANTHROPIC_API_KEY=sk-ant-...         # or put ANTHROPIC_API_KEY=... in .env here
export TYPESAFE_API_KEY=...                 # Jev reranking (optional; also read from .env)
```

macOS asks for **Screen Recording** permission for your terminal the first time. Grant it, then restart the terminal.

## Run

```sh
.venv/bin/python -m krillionhelper
```

On the page, click **Areas** and pick both areas (3-second countdown so you can switch to the game):

1. **Prompt card**: a tight box around the `CATEGORY N` line and the prompt. Read every 0.3s for answers.
2. **Whole game window**: used to follow turns and see what other players type.

Keep the answers page and your terminal outside both boxes.

## Daily Krillion mode

```sh
.venv/bin/python -m krillionhelper --daily      # http://localhost:8766, its own screen area
```

Goal: the single **One in a Krillion** answer. Answer sites publish each day's 7 prompts with answers by rarity tier, so daily mode has Claude find today's sheet with web search once (about 30s, then cached for the day). Every prompt it reads from the screen is matched against that sheet: the Krillion answer is pinned to the top in gold, then Deep Cut (purple), Rare (red), and Schooler (blue). Claude's own ranked list follows as the fallback.

## Using the page

- Answers run from rarest (1) to most common. ★ = worked for you before on this prompt and no one else has used it.
- Click an answer to copy it. Copied answers get ✓ / ✗ in case the automatic check gets it wrong.
- Answers that are gone disappear by themselves: other players' guesses, your accepted answers, and rejected ones. Spare answers move up to fill the gaps.
- Type a prompt in the box if OCR misses it. ↻ asks again without the cache.

## How it gets better as you play

Everything is stored in `game.sqlite3`:

| What | How it's learned | How it's used |
|---|---|---|
| Your answers' verdicts | the game's own line, e.g. `"turnstile": no echo. keep going` / `already said` | exact hiding and labels; joined to each answer's rarity to learn which kinds survive |
| Game results | the results screen: `YOU WIN … RATING 1247 +16` | rating and wins on the page, `tools/analyze.py` |
| Other players' guesses | the last thing a player typed before the turn moved on | removed this round; counted so common answers sink next time |
| Your other plays | the answer you copied or typed when your turn ends (hidden only, never used as a verdict) | removed this round; your ✓/✗ adds a verdict by hand; accepted rare answers become ★ |
| Abandoned attempts | a player types a full word, then replaces it | likely rejections, evidence for the rules |
| Checker rules | after every 5 new labels, Claude Opus rewrites short rules from all the evidence | sent with every prompt; shown under the prompt on the page |
| Raw snapshots | every changed read of the game window (`observations` table) | for improving the parsers later |

## Ranking

Claude lists 40 candidates rarest-first. TypeSafe's Jev decision model then scores each one, about 70ms per batch: how likely the checker accepts it (validity AUC 0.90 against judged answers) and how rare it is (0–4). Rarity also blends in word frequency (`wordfreq`): how often the answer turns up in written text, which is how Krillion's own familiarity database scores many prompts. Answers are sorted rarest-first among those likely valid, and near-spellings of the same answer are merged. Clear junk (validity under 0.2) is dropped, unsure answers go to the bottom, and very common ones sink below the visible 30. Answers that other players often say, and rarity bands that keep coming back "already said", get pushed down. Synonyms of a played guess are hidden too, because the game auto-corrects typos and synonyms.

## Speed

Claude Opus 5.5 in fast mode, with Claude Haiku 5.5 as a backup: if Opus has no answer after 0.6s, Haiku's answers show first. Prompts with spelling rules ("three letters", "starts with B") go to Haiku first, with local letter checks. First answer is typically 0.6–0.9s after the prompt appears. Timings go to `latency.log`; answers are cached in `answers.sqlite3`.

## Tuning (config.json)

| key | default | what it does |
|---|---|---|
| `model` / `effort` / `fastMode` | `claude-opus-5-5` / `low` / `true` | main answer model |
| `ruleModel` / `hedgeAfterMs` | `claude-haiku-5-5` / `600` | backup model and when its answers show |
| `answerCount` / `spareAnswers` | `30` / `10` | answers shown / extra answers requested |
| `learnModel` | `claude-opus-5-5` | writes the checker rules |
| `playerName` | none | your in-game name, so your turns are recognized |
| `captureIntervalMs` | `300` | prompt card read interval |

## Tests

```sh
.venv/bin/python -m pytest -q tests
.venv/bin/python tools/modelBench.py        # compare answer models on speed and judged correctness
.venv/bin/python tools/analyze.py           # progress: rating, verdicts, which answers survive, learned rules
```
