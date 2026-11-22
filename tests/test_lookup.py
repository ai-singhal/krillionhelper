import json

from krillionhelper.lookup import DailySheet, tieredAnswers

SHEET = {
    "date": "2026-11-22",
    "source": "https://example.com/krillion-answers",
    "prompts": [
        {"prompt": "Name a car brand", "krillion": ["Sears"], "deepCut": ["Greyhound", "Anchor"], "rare": ["Reliant"], "schooler": ["Bentley"]},
        {"prompt": "Name a part of the field or court in basketball, soccer, American football, cricket, or rugby",
         "krillion": ["Corridor of Uncertainty"], "deepCut": ["Silly Mid-Off"], "rare": ["Gully"], "schooler": ["Backboard"]},
    ],
}


def sheet(tmp_path):
    s = DailySheet(client=None, dbPath=tmp_path / "g.sqlite3")
    s.sheet = json.loads(json.dumps(SHEET))
    return s


def test_matches_prompts_including_ocr_cut_ones(tmp_path):
    s = sheet(tmp_path)
    assert s.match("Name a car brand!")["krillion"] == ["Sears"]
    assert s.match("Name a part of the field or court in basketball, soccer")["krillion"] == ["Corridor of Uncertainty"]
    assert s.match("Name a fruit") is None


def test_tiered_answers_rarest_first(tmp_path):
    entry = sheet(tmp_path).match("Name a car brand")
    assert tieredAnswers(entry) == [
        ("Sears", "ONE IN A KRILLION"), ("Greyhound", "DEEP CUT"), ("Anchor", "DEEP CUT"),
        ("Reliant", "RARE"), ("Bentley", "SCHOOLER"),
    ]
