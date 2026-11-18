"""
database.py - SQLite persistence: players, game sessions, high scores,
statistics and the user's settings.

Schema (see docs/DATABASE.md for the diagram)

    players          one row per player name
    game_sessions    one row per finished game
    high_scores      best score of every player       (1 : 1 with players)
    game_statistics  running totals of every player   (1 : 1 with players)
    settings         key / value pairs from the Settings screen

If the database file cannot be opened (read-only folder, corrupt file ...)
the manager falls back to an in-memory database so the game still runs; the
``error`` attribute then explains what went wrong.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS game_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id      INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    score          INTEGER NOT NULL,
    fruits_sliced  INTEGER NOT NULL,
    fruits_missed  INTEGER NOT NULL,
    bombs_hit      INTEGER NOT NULL,
    max_combo      INTEGER NOT NULL,
    accuracy       REAL    NOT NULL,
    level_reached  INTEGER NOT NULL,
    duration_s     REAL    NOT NULL,
    control_mode   TEXT    NOT NULL,
    played_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS high_scores (
    player_id    INTEGER PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    best_score   INTEGER NOT NULL,
    session_id   INTEGER REFERENCES game_sessions(id),
    achieved_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS game_statistics (
    player_id            INTEGER PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    games_played         INTEGER NOT NULL DEFAULT 0,
    total_score          INTEGER NOT NULL DEFAULT 0,
    total_fruits_sliced  INTEGER NOT NULL DEFAULT 0,
    total_bombs_hit      INTEGER NOT NULL DEFAULT 0,
    best_combo           INTEGER NOT NULL DEFAULT 0,
    best_accuracy        REAL    NOT NULL DEFAULT 0,
    total_play_time_s    REAL    NOT NULL DEFAULT 0,
    last_played          TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_player ON game_sessions(player_id);
CREATE INDEX IF NOT EXISTS idx_high_scores_best ON high_scores(best_score DESC);
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


class DatabaseManager:
    """All SQL lives in this class; the rest of the game calls its methods."""

    def __init__(self, path=None):
        self.path = str(path or config.DB_PATH)
        self.error = None
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = self._connect(self.path)
        except (sqlite3.Error, OSError) as exc:
            # Keep playing with a temporary database instead of crashing.
            self.error = f"Database unavailable ({exc}). Scores will not be saved."
            self.path = ":memory:"
            self.conn = self._connect(self.path)

    @staticmethod
    def _connect(path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ players
    @staticmethod
    def clean_name(name):
        return (name or "").strip()[:20] or config.DEFAULT_SETTINGS["player_name"]

    def get_or_create_player(self, name, commit=True):
        name = self.clean_name(name)
        row = self.conn.execute("SELECT id FROM players WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO players (name, created_at) VALUES (?, ?)", (name, _now()))
        if commit:
            self.conn.commit()
        return cur.lastrowid

    # ----------------------------------------------------------------- sessions
    def record_session(self, player_name, stats, control_mode):
        """
        Persists a finished game session across multiple relational tables atomically.

        ACID Transaction Guarantee:
        - Wrapped in `with self.conn:` context manager.
        - If any insert fails, the entire transaction is rolled back, preventing
          corrupted partial state across players, game_sessions, and high_scores.

        SQLite UPSERT Mechanics:
        - `INSERT INTO high_scores ... ON CONFLICT(player_id) DO UPDATE ...`
          ensures high_scores is updated ONLY IF the new score is strictly greater
          (`WHERE excluded.best_score > high_scores.best_score`).
        - `game_statistics` table automatically aggregates running totals (games_played,
          total_score, max combo) using SQLite expressions.
        """
        try:
            with self.conn:  # Context manager: commits on completion, rolls back on exception
                player_name = self.clean_name(player_name)
                player_id = self.get_or_create_player(player_name, commit=False)
                previous_best = self.get_player_best(player_name)
                now = _now()

                # 1. Insert granular round history row
                cur = self.conn.execute(
                    """INSERT INTO game_sessions
                       (player_id, score, fruits_sliced, fruits_missed, bombs_hit,
                        max_combo, accuracy, level_reached, duration_s, control_mode, played_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (player_id, stats.score, stats.fruits_sliced, stats.fruits_missed,
                     stats.bombs_hit, stats.max_combo, stats.accuracy, stats.level,
                     round(stats.elapsed, 2), control_mode, now))
                session_id = cur.lastrowid

                # 2. Atomic UPSERT: update high_scores only when new score beats old record
                self.conn.execute(
                    """INSERT INTO high_scores (player_id, best_score, session_id, achieved_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(player_id) DO UPDATE SET
                           best_score  = excluded.best_score,
                           session_id  = excluded.session_id,
                           achieved_at = excluded.achieved_at
                       WHERE excluded.best_score > high_scores.best_score""",
                    (player_id, stats.score, session_id, now))

                # 3. Atomic UPSERT: aggregate lifetime statistics
                self.conn.execute(
                    """INSERT INTO game_statistics
                       (player_id, games_played, total_score, total_fruits_sliced,
                        total_bombs_hit, best_combo, best_accuracy, total_play_time_s, last_played)
                       VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(player_id) DO UPDATE SET
                           games_played        = games_played + 1,
                           total_score         = total_score + excluded.total_score,
                           total_fruits_sliced = total_fruits_sliced + excluded.total_fruits_sliced,
                           total_bombs_hit     = total_bombs_hit + excluded.total_bombs_hit,
                           best_combo          = MAX(best_combo, excluded.best_combo),
                           best_accuracy       = MAX(best_accuracy, excluded.best_accuracy),
                           total_play_time_s   = total_play_time_s + excluded.total_play_time_s,
                           last_played         = excluded.last_played""",
                    (player_id, stats.score, stats.fruits_sliced, stats.bombs_hit,
                     stats.max_combo, stats.accuracy, round(stats.elapsed, 2), now))
        except sqlite3.Error as exc:
            self.error = f"Could not save the game: {exc}"
            return {"player_best": max(stats.score, 0), "global_best": stats.score,
                    "new_record": False, "saved": False}

        player_best = self.get_player_best(player_name)
        return {
            "player_best": player_best,
            "global_best": self.get_global_best(),
            "new_record": stats.score > 0 and stats.score > previous_best,
            "saved": True,
        }

    # ------------------------------------------------------------------ queries
    def get_player_best(self, player_name):
        """Fetches personal best score using a JOIN across high_scores and players."""
        row = self.conn.execute(
            """SELECT h.best_score FROM high_scores h
               JOIN players p ON p.id = h.player_id WHERE p.name = ?""",
            (self.clean_name(player_name),)).fetchone()
        return row["best_score"] if row else 0

    def get_global_best(self):
        """Returns the highest score recorded across all players."""
        row = self.conn.execute("SELECT MAX(best_score) AS best FROM high_scores").fetchone()
        return row["best"] or 0

    def get_leaderboard(self, limit=10):
        """
        Generates the Top N Leaderboard via a 3-table relational JOIN.
        SQL Query:
            SELECT p.name, h.best_score, h.achieved_at, s.games_played, ...
            FROM high_scores h
            JOIN players p          ON p.id = h.player_id
            JOIN game_statistics s  ON s.player_id = h.player_id
            ORDER BY h.best_score DESC, h.achieved_at ASC
            LIMIT ?
        """
        rows = self.conn.execute(
            """SELECT p.name, h.best_score, h.achieved_at,
                      s.games_played, s.total_fruits_sliced, s.best_combo, s.best_accuracy
               FROM high_scores h
               JOIN players p          ON p.id = h.player_id
               JOIN game_statistics s  ON s.player_id = h.player_id
               ORDER BY h.best_score DESC, h.achieved_at ASC
               LIMIT ?""", (limit,)).fetchall()
        return [dict(row, rank=i + 1) for i, row in enumerate(rows)]

    def get_player_stats(self, player_name):
        row = self.conn.execute(
            """SELECT s.* FROM game_statistics s
               JOIN players p ON p.id = s.player_id WHERE p.name = ?""",
            (self.clean_name(player_name),)).fetchone()
        return dict(row) if row else None

    def recent_sessions(self, limit=5):
        rows = self.conn.execute(
            """SELECT p.name, g.score, g.accuracy, g.played_at FROM game_sessions g
               JOIN players p ON p.id = g.player_id
               ORDER BY g.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- settings
    def load_settings(self):
        settings = dict(config.DEFAULT_SETTINGS)
        try:
            for row in self.conn.execute("SELECT key, value FROM settings"):
                if row["key"] in settings:
                    settings[row["key"]] = row["value"]
        except sqlite3.Error as exc:
            self.error = f"Could not read settings: {exc}"
        return settings

    def save_setting(self, key, value):
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO settings (key, value) VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                    (key, str(value)))
        except sqlite3.Error as exc:
            self.error = f"Could not save setting: {exc}"
