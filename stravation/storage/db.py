# stravation/storage/db.py
from __future__ import annotations
import os, sqlite3, pendulum as p
from typing import Optional, Iterable, Dict, Tuple

DB_PATH = os.getenv("SPORT_DB_PATH", "stravation.sqlite3")

# ── connexion unique ───────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con

# ── init DB & migrations légères ──────────────────────────────────────────────
def init_db() -> None:
    con = _connect()
    cur = con.cursor()

    # Checkpoints génériques (si tu les utilises ailleurs)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS checkpoints (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # Activités déjà vues (existant probable) — on ne modifie pas
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_activities (
        strava_id INTEGER PRIMARY KEY,
        seen_at TEXT NOT NULL
    )
    """)

    # ✅ N O U V E A U : routes déjà vues (mémoire incrémentale)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_routes (
        route_id INTEGER PRIMARY KEY,
        updated_at TEXT,            -- si Strava fournit "updated_at" sur les routes
        checksum TEXT,              -- fallback (ex: hash sur nom+distance si besoin)
        synced_at TEXT NOT NULL     -- date locale du dernier sync réussi
    )
    """)

    con.commit()
    con.close()

# ── helpers checkpoints ───────────────────────────────────────────────────────
def get_checkpoint(key: str) -> Optional[str]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT value FROM checkpoints WHERE key=?", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None

def set_checkpoint(key: str, value: str) -> None:
    con = _connect()
    cur = con.cursor()
    cur.execute("INSERT INTO checkpoints(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()
    con.close()

# ── helpers seen_routes (N O U V E A U) ───────────────────────────────────────
def get_seen_routes() -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """
    Retourne {route_id: (updated_at, checksum)}
    """
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT route_id, updated_at, checksum FROM seen_routes")
    out = {int(rid): (upd, chk) for (rid, upd, chk) in cur.fetchall()}
    con.close()
    return out

def mark_route_seen(route_id: int, *, updated_at: Optional[str], checksum: Optional[str]) -> None:
    """
    Upsert une route comme 'vue' après sync OK.
    """
    con = _connect()
    cur = con.cursor()
    now_iso = p.now().to_datetime_string()  # "YYYY-MM-DD HH:mm:ss"
    cur.execute("""
        INSERT INTO seen_routes(route_id, updated_at, checksum, synced_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(route_id) DO UPDATE SET
            updated_at = excluded.updated_at,
            checksum = excluded.checksum,
            synced_at = excluded.synced_at
    """, (int(route_id), updated_at, checksum, now_iso))
    con.commit()
    con.close()

def forget_routes(route_ids: Iterable[int]) -> None:
    """
    Permet de 'oublier' des routes (pour re-synchroniser uniquement celles-ci).
    """
    con = _connect()
    cur = con.cursor()
    cur.executemany("DELETE FROM seen_routes WHERE route_id=?", [(int(r),) for r in route_ids])
    con.commit()
    con.close()
