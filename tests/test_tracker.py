from krillionhelper.ocr.line import OcrLine as L
from krillionhelper.tracker import GameTracker, normalizeGuess, samePlayer

PROMPT = "Name a piece of bedding"
HUD = [
    L("CATEGORY 1", 9, 120, 145),
    L(PROMPT, 24, 148, 145),
    L("FLOATING 2/2", 10, 20, 640),
]


def frame(up=None, typed=None, extra=()):
    lines = list(HUD)
    if up:
        lines.append(L(f"{up} is up", 14, 600, 300))
    if typed:
        lines.append(L(typed, 16, 400, 300))
    return lines + list(extra)


def guesses(result):
    return result["guesses"]


def start(tmp_path):
    t = GameTracker(tmp_path / "g.sqlite3", playerName="puffermain")
    t.startRound(PROMPT)
    t.observe(frame(), 787, [PROMPT])
    t.observe(frame(), 787, [PROMPT])  # baseline
    return t


def test_guess_is_final_typing_when_turn_passes(tmp_path):
    t = start(tmp_path)
    assert guesses(t.observe(frame("kiwi"), 787, [PROMPT])) == []
    for partial in ["pil", "pillo", "pillow|"]:
        assert guesses(t.observe(frame("kiwi", partial), 787, [PROMPT])) == []  # typing in progress: nothing yet
    # Turn moves to you (the answer box placeholder): kiwi's guess was "pillow".
    found = guesses(t.observe(frame(extra=[L("your puffer, type an answer", 18, 650, 160)]), 787, [PROMPT]))
    assert found == [{"text": "pillow", "player": "kiwi"}]
    # Back to kiwi (OCR reads the name differently). You typed nothing and copied nothing: no guess for you.
    assert guesses(t.observe(frame("kiwii"), 787, [PROMPT])) == []
    t.observe(frame("kiwii", "duvet"), 787, [PROMPT])
    assert guesses(t.observe(frame("otter"), 787, [PROMPT])) == [{"text": "duvet", "player": "kiwii"}]


def test_noise_is_not_a_guess(tmp_path):
    t = start(tmp_path)
    t.knownPlayers.add("puffermain")
    t.observe(frame("kiwi"), 787, [PROMPT])
    for junk in ["pufferrnaim", "VW 1231", "1 Archive", "ER"]:
        t.observe(frame("kiwi", junk), 787, [PROMPT], ownAnswers=["Archive"])
    assert guesses(t.observe(frame("otter"), 787, [PROMPT])) == []


def test_your_turn_hides_what_you_played_without_guessing_a_verdict(tmp_path):
    t = start(tmp_path)
    t.observe(frame("puffermain"), 787, [PROMPT])  # your name in the turn line = your turn
    t.recordMyAnswer("Bolster")
    t.recordMyAnswer("Valance")
    result = t.observe(frame("kiwi"), 787, [PROMPT])
    assert result["guesses"] == [{"text": "Valance", "player": "you"}]
    assert result["labels"] == []  # verdicts only come from the game's own verdict line or your ✓/✗
    t.recordFeedback(PROMPT, "Valance", "accepted")
    assert t.goldAnswers(PROMPT) == ["Valance"]


def test_cut_off_guesses_are_completed_from_our_answers(tmp_path):
    t = start(tmp_path)
    t.observe(frame("kiwi"), 787, [PROMPT])
    t.observe(frame("kiwi", "coverl"), 787, [PROMPT], ownAnswers=["Coverlet", "Counterpane"])
    result = t.observe(frame("otter"), 787, [PROMPT], ownAnswers=["Coverlet", "Counterpane"])
    assert result["guesses"] == [{"text": "Coverlet", "player": "kiwi"}]


def test_abandoned_attempts_are_kept_as_likely_rejections(tmp_path):
    t = start(tmp_path)
    t.observe(frame("kiwi"), 787, [PROMPT])
    for typed in ["mattress", "pil", "pillow"]:
        t.observe(frame("kiwi", typed), 787, [PROMPT])
    t.observe(frame("otter"), 787, [PROMPT])
    _, attempts = t.distillExamples()
    assert (PROMPT, "mattress", "abandoned") in attempts and (PROMPT, "pillow", "final") in attempts


def test_feedback_and_learning(tmp_path):
    t = start(tmp_path)
    t.recordFeedback("Name a movie studio", "Annapurna Pictures", "rejected")
    t.recordFeedback("Name a movie studio", "A24", "accepted")
    assert t.feedbackFor("Name a movie studio") == {"accepted": ["a24"], "rejected": ["annapurnapicture"]}
    assert t.lessons()["rejected"] == [("Name a movie studio", "Annapurna Pictures")]
    t.observe(frame("kiwi", "quilt"), 787, [PROMPT])
    t.observe(frame("otter"), 787, [PROMPT])
    t.promptKey = None
    t.startRound(PROMPT)  # same prompt in a later game
    assert t.commonGuesses(PROMPT) == ["quilt"]
    assert t.stats()["accepted"] == 1 and t.stats()["acceptRate"] == 50
    assert t.guessCounts(PROMPT) == {"quilt": 1}


def test_helpers():
    assert normalizeGuess("Pumpkins!") == normalizeGuess("pumpkin")
    assert samePlayer("kiwi", "kiwii") and samePlayer("kiwi", "kiwl") and not samePlayer("kiwi", "otter")
    from krillionhelper.tracker import isContinuation

    assert isContinuation("pil", "pillow") and isContinuation("pillw", "pillow") and not isContinuation("mattress", "pil")


def test_game_verdict_lines_hide_and_label_your_answers(tmp_path):
    t = start(tmp_path)
    result = t.observe(frame(extra=[L('"turnstile": no echo. keep going', 14, 500, 300)]), 787, [PROMPT])
    assert result["guesses"] == [{"text": "turnstile", "player": "you"}]
    assert result["labels"] == [("turnstile", "rejected")]  # "no echo" = the game didn't recognize it
    # Same line still on screen next read: not counted twice.
    assert t.observe(frame(extra=[L('"turnstile": no echo: keep going', 14, 500, 300)]), 787, [PROMPT])["guesses"] == []
    # A message we don't know yet: hidden (it was played) and stored for the rules writer, but not labeled.
    result = t.observe(frame(extra=[L('"lozenge": something new', 14, 520, 300)]), 787, [PROMPT])
    assert result["guesses"] == [{"text": "lozenge", "player": "you"}] and result["labels"] == []
    _, attempts = t.distillExamples()
    assert (PROMPT, "lozenge", "game said: something new") in attempts


def test_hearts_prefix_is_stripped_from_the_answer_box():
    from krillionhelper.tracker import cleanGuessText

    assert cleanGuessText("·2 turnstile") == "turnstile" and cleanGuessText("÷2 tack") == "tack"
    assert cleanGuessText("1243") == "1243"


def test_last_rounds_verdict_still_on_screen_is_not_credited_to_the_new_prompt(tmp_path):
    t = start(tmp_path)
    t.observe(frame(extra=[L('"racing pigeon": no echo. keep going', 14, 500, 300)]), 787, [PROMPT])
    t.startRound("Name a country larger than one million square kilometres")
    stale = frame(extra=[L('"racing pigeon": no echo. keep going', 14, 500, 300)])
    for _ in range(4):
        assert t.observe(stale, 787, [])["labels"] == []
    assert t.feedbackFor("Name a country larger than one million square kilometres") == {"accepted": [], "rejected": []}



def test_fully_typed_answer_is_crossed_out_immediately(tmp_path):
    t = start(tmp_path)
    t.observe(frame("kiwi"), 787, [PROMPT])
    ours = ["Coverlet", "Counterpane", "Bolster"]
    # Partial typing doesn't count yet...
    assert guesses(t.observe(frame("kiwi", "Cov"), 787, [PROMPT], ownAnswers=ours)) == []
    # ...the full word does, before the turn even changes.
    assert guesses(t.observe(frame("kiwi", "coverlet|"), 787, [PROMPT], ownAnswers=ours)) == [{"text": "Coverlet", "player": "kiwi"}]
    # Your own typing in the answer box (bottom of the game window) counts as yours.
    assert guesses(t.observe(frame(extra=[L("·2 bolster", 18, 700, 160)]), 787, [PROMPT], ownAnswers=ours)) == [{"text": "Bolster", "player": "you"}]


def test_our_answers_page_inside_the_game_area_is_ignored(tmp_path):
    t = start(tmp_path)
    ours = ["Coverlet", "Counterpane", "Bolster", "Valance", "Duvet"]
    page = [L(a, 26, 100 + 40 * i, 780) for i, a in enumerate(ours)]
    assert guesses(t.observe(frame(extra=page), 787, [PROMPT], ownAnswers=ours)) == []


def test_tier_badge_marks_the_answer_accepted(tmp_path):
    t = start(tmp_path)
    t.observe(frame("puffermain"), 787, [PROMPT])  # your turn
    t.recordMyAnswer("Valance")
    t.observe(frame("puffermain", extra=[L("RARE", 12, 30, 330)]), 787, [PROMPT])
    result = t.observe(frame("kiwi"), 787, [PROMPT])
    assert ("Valance", "accepted") in result["labels"]
    tier = t.db.execute("SELECT tier, points FROM acceptances WHERE answer = 'Valance'").fetchone()
    assert tier == ("RARE", 60)


def test_tier_read_from_answer_color_and_colors_learned_from_tier_text(tmp_path):
    t = start(tmp_path)
    # Tier name written out once: its on-screen color is learned.
    t.observe(frame(extra=[L("SCHOOLER", 12, 30, 330, 80, (60, 130, 250))]), 787, [PROMPT])
    assert t.tierColors["SCHOOLER"] == (60, 130, 250)
    # Later an opponent's answer turns that blue with no tier text: accepted as SCHOOLER.
    t.observe(frame("kiwi"), 787, [PROMPT])
    t.observe(frame("kiwi", extra=[L("valance", 16, 400, 300, 90, (245, 245, 245))]), 787, [PROMPT])  # typing: white
    t.observe(frame("kiwi", extra=[L("valance", 16, 400, 300, 90, (62, 128, 248))]), 787, [PROMPT])   # accepted: blue
    t.observe(frame("otter"), 787, [PROMPT])
    assert t.db.execute("SELECT player, answer, tier, points FROM acceptances").fetchall() == [("kiwi", "valance", "SCHOOLER", 30)]


def test_color_helpers():
    from krillionhelper.colors import DEFAULT_TIER_COLORS, isPlainText, tierForColor

    assert isPlainText((245, 245, 245)) and not isPlainText((80, 150, 255))
    assert tierForColor((240, 190, 60), DEFAULT_TIER_COLORS) == "ONE IN A KRILLION"
    assert tierForColor((245, 245, 245), DEFAULT_TIER_COLORS) is None
