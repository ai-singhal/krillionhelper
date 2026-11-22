from krillionhelper.answers import cleanAnswer
from krillionhelper.config import DEFAULTS
from krillionhelper.textnorm import extractPrompt, isSamePrompt, normalizeOcrText, promptKey, selectPromptLines

STARTERS = DEFAULTS["promptStarters"]


def prompt(lines):
    return extractPrompt(normalizeOcrText(lines), STARTERS)


def test_extracts_prompt_and_drops_ui_noise():
    assert prompt(["ROUND 3", "Name a fruit", "0:12"]) == "Name a fruit"
    assert prompt(["Name a country", "in Africa!", "350 pts"]) == "Name a country in Africa!"
    assert prompt(["Narne a  type of cheese"]) == "Name a type of cheese"


def test_rejects_non_prompts():
    assert prompt(["Get ready..."]) is None
    assert prompt(["Score: 1200"]) is None
    assert prompt(["Name"]) is None
    assert prompt([]) is None


def test_fixes_obvious_ocr_noise():
    assert normalizeOcrText(["Name a fru1t  ,", "|"]) == "Name a frult, I"
    assert normalizeOcrText(["“Name”  a  c0lor"]) == '"Name" a color'
    assert normalizeOcrText(["Name a year 1990"]) == "Name a year 1990"


def test_same_prompt_tolerates_ocr_jitter():
    assert isSamePrompt("Name a fruit", "name a fruit.")
    assert isSamePrompt("Name a dog breed", "Name a dog bread")
    assert isSamePrompt("Name a fruit", "Name a fru")  # fading out


def test_different_prompts_are_new():
    assert not isSamePrompt("Name a fruit", "Name a vegetable")
    assert not isSamePrompt("Name a dog breed", "Name a cat breed")
    assert not isSamePrompt("Name a car", "Name a cat")
    assert not isSamePrompt("Name a fru", "Name a fruit")  # still being revealed
    assert not isSamePrompt(None, "Name a fruit")


def test_prompt_key():
    assert promptKey("Name a  Fruit!") == "name a fruit"


def test_clean_answer():
    assert cleanAnswer("1. Kiwano") == "Kiwano"
    assert cleanAnswer("- \"Rambutan\".") == "Rambutan"
    assert cleanAnswer("Here are 20 answers:") is None
    assert cleanAnswer("   ") is None


def test_prompt_block_from_whole_game_window():
    from krillionhelper.ocr.line import OcrLine as L

    # Real Apple Vision output for the full Puffer Party window at 1x (your screenshot).
    lines = [
        L("515", 23.0, 36.0, 678.1),
        L('I" LEHL', 20.0, 38.0, 20.0),
        L("C A T E G OR ¥", 9.0, 120.8, 144.8),
        L("Name a body part spelled with three", 24.0, 148.8, 144.8),
        L("letters", 19.0, 181.8, 146.8),
        L("otter is UP_", 19.0, 655.2, 159.8),
        L("SEND", 12.0, 656.2, 611.2),
    ]
    want = "Name a body part spelled with three letters"
    assert extractPrompt(normalizeOcrText(selectPromptLines(lines, ["category"], STARTERS)), STARTERS, ["category"]) == want
    # Typing an answer into the game's box must not change the prompt.
    typed = lines[:5] + [L("ode", 19.0, 655.2, 159.8), L("SEND", 12.0, 656.2, 611.2)]
    assert extractPrompt(normalizeOcrText(selectPromptLines(typed, ["category"], STARTERS)), STARTERS, ["category"]) == want
    # "CATEGORY" seen while the card is empty: no prompt, not the chat box.
    empty = [lines[2], L("amoha is up...", 19.0, 655.2, 159.8), L("SEND", 12.0, 656.2, 611.2)]
    assert extractPrompt(normalizeOcrText(selectPromptLines(empty, ["category"], STARTERS)), STARTERS, ["category"]) is None


def test_button_level_with_prompt_stays_separate():
    from krillionhelper.ocr.appleVision import groupIntoLines

    pieces = [
        (148.0, 24.0, 144.0, 420.0, "Name a disease that has a"),
        (150.0, 14.0, 1180.0, 40.0, "CHAT"),
        (181.0, 22.0, 145.0, 90.0, "vaccine"),
    ]
    assert [line.text for line in groupIntoLines(pieces)] == ["Name a disease that has a", "CHAT", "vaccine"]
    lines = groupIntoLines(pieces)
    assert selectPromptLines(lines, ["category"], STARTERS) == ["Name a disease that has a", "vaccine"]


def test_header_prompt_without_starter_word():
    assert extractPrompt("CATEGORY 3 Things that are yellow", STARTERS, ["category"], requireStarter=False) == "Things that are yellow"
    assert extractPrompt("CATEGORY 3 Your f", STARTERS, ["category"]) is None  # Puffer Party prompts start with "Name"
    assert extractPrompt("C A T E G O R Y 2", STARTERS, ["category"]) is None  # card header only


def test_constraints():
    from krillionhelper.constraints import failedRules, parseConstraints

    three = parseConstraints("Name a body part spelled with three letters")
    assert not failedRules("Arm", three) and failedRules("Shin", three)
    b = parseConstraints("Name an animal that starts with the letter B")
    assert [label for label, _ in b] == ["starts with 'b'"]
    assert not failedRules("Bison", b) and failedRules("Cat", b)
    vowel = parseConstraints("Name a fruit that ends with a vowel")
    assert not failedRules("Banana", vowel) and failedRules("Kiwifruit", vowel)
    assert parseConstraints("Name a famous painter") == []
    assert [l for l, _ in parseConstraints("Name a country with at least 8 letters")] == ["letters >= 8"]


def test_clean_answer_rejects_model_commentary():
    for line in ["Rib - yes", "Shin - that's 4 letters, skip", "Tit? not appropriate", "Let me list three-letter body parts",
                 "Pit (armpit)", "Note: these are rare"]:
        assert cleanAnswer(line) is None, line
    assert cleanAnswer("Guinea-Bissau") == "Guinea-Bissau"
    assert cleanAnswer("DR Congo") == "DR Congo"


def test_ignores_chat_text_from_other_windows():
    # Seen live when the region showed a chat window instead of the game.
    assert prompt(["Give me a real example, bc this is e"]) is None
    assert prompt(["Say ougnt tat unis is no transaction across tools. digitalocean Free libraries do simple dedupe."]) is None
    assert prompt(["Name a body part spelled with three letters"]) == "Name a body part spelled with three letters"


def test_game_instructions_and_own_page_are_not_prompts():
    from krillionhelper.ocr.line import OcrLine as L

    ignore = DEFAULTS["ignoreText"]
    assert selectPromptLines([L("Game. Name an answer and pass the puffer", 20, 100, 50)], ["category"], STARTERS, ignore) == []
    assert selectPromptLines([L("Name an answer and pass the puffer", 20, 100, 50)], ["category"], STARTERS, ignore) == []
    assert selectPromptLines([L("OCR missed it? Type the prompt", 14, 700, 900)], ["category"], STARTERS, ignore) == []
    assert selectPromptLines([L("Name a fruit", 24, 100, 50)], ["category"], STARTERS, ignore) == ["Name a fruit"]


def test_turn_lines_are_never_prompts():
    from krillionhelper.ocr.line import OcrLine as L

    for junk in ["Your puffer. type an answer your puffer - type!", "E kiwi* Is up", "A kiwi is up", "kiwi is up..."]:
        lines = [L("CATEGORY 2", 9, 120, 145), L(junk, 24, 148, 145)]
        text = normalizeOcrText(selectPromptLines(lines, ["category"], STARTERS, DEFAULTS["ignoreText"]))
        assert extractPrompt(text, STARTERS, ["category"]) is None, junk


def test_frequency_rarity_orders_common_to_rare():
    from krillionhelper.rarity import frequencyRarity

    values = [frequencyRarity(w) for w in ["Apple", "Grape", "Kumquat", "Acerola"]]
    assert values == sorted(values) and values[0] < 1 and values[-1] > 3
    assert frequencyRarity("Welsh poppy") > frequencyRarity("poppy")  # phrases are rarer than their words


def test_near_duplicates_merge():
    from krillionhelper.textnorm import isNearDuplicate

    assert isNearDuplicate("hotstenia", ["hofstenia"]) and isNearDuplicate("Nemertean", ["nemertean worm"])
    assert not isNearDuplicate("Cat", ["Bat"]) and not isNearDuplicate("Feijoa", ["Tamarillo"])


def test_unknown_words_get_no_rarity_signal():
    from krillionhelper.rarity import frequencyRarity

    assert frequencyRarity("Hotstenia") is None  # never seen: maybe a typo, not "rarest"
