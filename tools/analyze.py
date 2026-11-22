"""What's working? Reads game.sqlite3 and prints rating progress, verdicts on your answers,
which kinds of answers survive vs get "already said", and the current learned rules.

Usage: .venv/bin/python tools/analyze.py
"""

import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from krillionhelper.tracker import normalizeGuess  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "game.sqlite3"


def main():
    from krillionhelper.tracker import GameTracker

    GameTracker(DB)  # creates any tables added since the database was made
    db = sqlite3.connect(DB)
    games = db.execute("SELECT result, rating, delta FROM games ORDER BY id").fetchall()
    print("== Games")
    if games:
        wins = sum(g[0] == "win" for g in games)
        print(f"{len(games)} games, {wins} won ({100 * wins // len(games)}%). Rating: "
              + " -> ".join(str(g[1]) for g in games[-12:]))
    else:
        print("no finished games recorded yet")

    verdicts = db.execute("SELECT roundId, prompt, answer, message FROM verdicts ORDER BY id").fetchall()
    print(f"\n== Your answers ({len(verdicts)} verdicts from the game)")
    print(Counter(m for _, _, _, m in verdicts).most_common(6))

    rows = []
    for roundId, prompt, answer, message in verdicts:
        c = db.execute(
            "SELECT finalRank, pValid, rarity, source FROM candidates WHERE roundId = ? AND normalized = ? ORDER BY id DESC LIMIT 1",
            (roundId, normalizeGuess(answer)),
        ).fetchone()
        repeated = "already" in message.lower()  # "no echo" = not recognized, "already said" = repeat
        rows.append((answer, repeated, c))
    scored = [r for r in rows if r[2] and r[2][2] is not None]
    if scored:
        print("\n== Which answers survive (answers we suggested, with Jev scores)")
        for label, group in (("no echo (unknown)", [r for r in scored if not r[1]]), ("already said", [r for r in scored if r[1]])):
            if group:
                print(f"{label:20s} n={len(group):3d}  rarity {statistics.mean(r[2][2] for r in group):.2f}  "
                      f"validity {statistics.mean(r[2][1] for r in group):.2f}  shown at #{statistics.mean(r[2][0] for r in group) + 1:.1f}")
    off = [r[0] for r in rows if not r[2]]
    if off:
        print(f"\n{len(off)} answers you played weren't on our list: {off[:15]}")

    repeated = [r[0] for r in rows if r[1]]
    if repeated:
        print(f"\nRepeats ('already said'): {repeated[-15:]}")

    print("\n== Tiers earned (accepted answers)")
    tiers = db.execute("SELECT player = 'you', tier, COUNT(*), SUM(points) FROM acceptances GROUP BY player = 'you', tier ORDER BY 1 DESC, 4 DESC").fetchall()
    for mine, tier, n, points in tiers:
        print(f"{'you' if mine else 'others':6s} {tier or '?':18s} x{n:<3d} {points or 0} pts")
    if not tiers:
        print("none recorded yet (tier badges and answer colors are tracked from now on)")
    tracker = GameTracker(DB)
    print("points by rarity band (learned):", tracker.pointsByRarityBand(minSamples=1) or "not enough data yet")
    print("tier colors learned from screen text:", db.execute("SELECT tier, round(r), round(g), round(b), samples FROM tierColors").fetchall())

    print("\n== Other players")
    top = db.execute("SELECT text, COUNT(*) n FROM guesses WHERE player != 'you' GROUP BY normalized ORDER BY n DESC LIMIT 12").fetchall()
    print("most frequent guesses:", top)

    insight = db.execute("SELECT text, basedOn FROM insights ORDER BY id DESC LIMIT 1").fetchone()
    print(f"\n== Learned checker rules (from {insight[1]} labels)\n{insight[0]}" if insight else "\n(no rules learned yet)")


if __name__ == "__main__":
    main()
