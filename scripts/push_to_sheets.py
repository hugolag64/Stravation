from __future__ import annotations

from notion_client import Client as Notion
from datetime import datetime, timedelta
from typing import Set, List, Any
import os

from stravation.services.google_sheets import GoogleSheetsClient
from stravation.utils.envtools import load_dotenv_if_exists

load_dotenv_if_exists()

# ──────────────────────────────────────────────────────────────
# ENV
# ──────────────────────────────────────────────────────────────
NOTION_TOKEN = os.getenv("NOTION_API_KEY")
NOTION_DB    = os.getenv("NOTION_DB_ACTIVITIES") or os.getenv("NOTION_DB_SPORT")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
SPORT_SHEET_NAME = os.getenv("SPORT_SHEET_NAME", "BDD sport")

notion = Notion(auth=NOTION_TOKEN)


# ──────────────────────────────────────────────────────────────
# Google Sheets client
# ──────────────────────────────────────────────────────────────
def get_sheets_client() -> GoogleSheetsClient:
    if not GOOGLE_SHEETS_ID:
        raise RuntimeError("GOOGLE_SHEETS_ID manquant dans .env")
    return GoogleSheetsClient(GOOGLE_SHEETS_ID)


# ──────────────────────────────────────────────────────────────
# Helpers Notion
# ──────────────────────────────────────────────────────────────
def read_prop(props, name):
    p = props[name]
    t = p.get("type")

    if t == "number":
        return p.get("number")

    if t == "date":
        d = p.get("date") or {}
        return d.get("start")

    if t in ("title", "rich_text"):
        arr = p.get(t) or []
        if arr:
            return arr[0]["plain_text"]

    return None


def build_row_from_notion(props) -> List[Any]:
    """Construit la ligne envoyée dans Google Sheets."""
    return [
        read_prop(props, "Nom") or "",
        str(read_prop(props, "Date") or "")[:10],
        "",
        read_prop(props, "Distance (km)") or "",
        "", "",  # Temps, Allure → calculés dans Sheets
        read_prop(props, "D+ (m)") or "",
        read_prop(props, "FC moy (bpm)") or "",
        read_prop(props, "FC max (bpm)") or "",
        read_prop(props, "Calories") or "",
        read_prop(props, "Charge TRIMP") or "",
        read_prop(props, "Strava ID") or "",
        read_prop(props, "Durée (s)") or "",
    ]


# ──────────────────────────────────────────────────────────────
# Lecture Strava ID existants — VERSION CORRIGÉE
# ──────────────────────────────────────────────────────────────
def get_sheet_ids(sheets: GoogleSheetsClient) -> Set[str]:
    """
    Récupère tous les Strava ID déjà présents dans Google Sheets.
    Lecture A2:Z pour éviter les erreurs de colonnes.
    Colonne L = index 11.
    Nettoyage complet pour fiabilité.
    """
    try:
        rows = sheets.get_values(SPORT_SHEET_NAME, "A2:Z2000")
    except Exception:
        return set()

    ids = set()

    for row in rows:
        if len(row) < 12:  # Pas de colonne L
            continue

        raw = str(row[11]).strip()

        # Filtrer les lignes vides / "None" / "0"
        if not raw:
            continue

        # Nettoyage espaces invisibles
        cleaned = raw.replace(" ", "").strip()

        # Si l'ID ressemble à un ID Strava → on l'ajoute
        if cleaned.isdigit():
            ids.add(cleaned)

    return ids


# ──────────────────────────────────────────────────────────────
# Trouver la première ligne vide
# ──────────────────────────────────────────────────────────────
def get_first_empty_row(sheets: GoogleSheetsClient) -> int:
    """Retourne la première ligne où la colonne A est vide."""
    try:
        rows = sheets.get_values(SPORT_SHEET_NAME, "A2:A2000")
    except Exception:
        return 2

    row_index = 2

    for r in rows:
        if not r or not str(r[0]).strip():
            return row_index
        row_index += 1

    return row_index


# ──────────────────────────────────────────────────────────────
# PUSH ACTIVITÉS MANQUANTES
# ──────────────────────────────────────────────────────────────
def push_missing(days: int = 30) -> int:
    sheets = get_sheets_client()

    since = (datetime.now() - timedelta(days=days)).date().isoformat()

    # Récupération depuis Notion
    notion_pages = notion.databases.query(
        database_id=NOTION_DB,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after": since}},
                {"property": "Strava ID", "rich_text": {"is_not_empty": True}},
            ]
        }
    )

    existing_ids = get_sheet_ids(sheets)

    added = 0

    for page in notion_pages.get("results", []):
        props = page["properties"]

        sid = read_prop(props, "Strava ID")

        if not sid:
            continue

        sid = str(sid).strip().replace(" ", "")

        # ─── Garde-fou Strava ID ────────────────────────────────
        if sid in existing_ids:
            print(f"⛔ ID {sid} déjà présent → SKIP")
            continue

        # Construction ligne
        row = build_row_from_notion(props)

        # Première ligne vide
        row_index = get_first_empty_row(sheets)

        # Push dans Sheets
        sheets.write_row(SPORT_SHEET_NAME, row_index, row)
        print(f"➕ Ajout activité ID {sid} en ligne {row_index}")

        added += 1
        existing_ids.add(sid)  # Sécurise si plusieurs activités pushées

    print(f"✔ Ajouté {added} nouvelles activités dans Google Sheets.")
    return added


# ──────────────────────────────────────────────────────────────
# Script direct
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    push_missing(30)
