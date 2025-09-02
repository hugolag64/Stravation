from __future__ import annotations
import os
import sqlite3
from typing import Tuple, Optional

from ..config import DB_PATH

DDL = """
CREATE TABLE IF NOT EXISTS checkpoints(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_activities(
  strava_id TEXT PRIMARY KEY
);
"""

# ──────────────────────────────────────────────────────────────────────────────
# Connexion & schéma
# ──────────────────────────────────────────────────────────────────────────────
def connect() -> sqlite3.Connection:
    first = not os.path.exists(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    if first:
        con.executescript(DDL)
        con.commit()
    return con

def ensure_schema() -> None:
    with connect() as con:
        con.executescript(DDL)
        con.commit()

def db_path() -> str:
    return DB_PATH

# ──────────────────────────────────────────────────────────────────────────────
# Checkpoints
# ──────────────────────────────────────────────────────────────────────────────
def get_checkpoint(key: str) -> Optional[str]:
    with connect() as con:
        cur = con.execute("SELECT value FROM checkpoints WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

def set_checkpoint(key: str, value: str) -> None:
    with connect() as con:
        con.execute("REPLACE INTO checkpoints(key,value) VALUES(?,?)", (key, value))
        con.commit()

def clear_checkpoints() -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM checkpoints;")
        con.commit()
        return cur.rowcount

# ──────────────────────────────────────────────────────────────────────────────
# Seen activities
# ──────────────────────────────────────────────────────────────────────────────
def is_seen(strava_id: str) -> bool:
    with connect() as con:
        cur = con.execute("SELECT 1 FROM seen_activities WHERE strava_id=?", (strava_id,))
        return cur.fetchone() is not None

def mark_seen(strava_id: str) -> None:
    with connect() as con:
        con.execute("REPLACE INTO seen_activities(strava_id) VALUES(?)", (strava_id,))
        con.commit()

def clear_seen() -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM seen_activities;")
        con.commit()
        return cur.rowcount

def counts() -> Tuple[int, int]:
    """Retourne (nb_checkpoints, nb_seen)."""
    with connect() as con:
        c1 = con.execute("SELECT COUNT(*) FROM checkpoints;").fetchone()[0]
        c2 = con.execute("SELECT COUNT(*) FROM seen_activities;").fetchone()[0]
        return int(c1), int(c2)

def reset_all() -> Tuple[int, int]:
    """Vide checkpoints + seen_activities."""
    n1 = clear_checkpoints()
    n2 = clear_seen()
    return n1, n2
