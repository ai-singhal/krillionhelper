"""SQLite cache of answers keyed by normalized prompt."""

import json
import sqlite3
import threading
import time


class AnswerCache:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS answers ("
            " key TEXT PRIMARY KEY, prompt TEXT NOT NULL, answers TEXT NOT NULL,"
            " model TEXT NOT NULL, createdAt REAL NOT NULL)"
        )
        self.db.commit()

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT answers FROM answers WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, prompt, answers, model):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO answers (key, prompt, answers, model, createdAt) VALUES (?, ?, ?, ?, ?)",
                (key, prompt, json.dumps(answers), model, time.time()),
            )
            self.db.commit()
