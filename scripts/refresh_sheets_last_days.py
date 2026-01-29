from __future__ import annotations

import os
import sys
import time
import httpx
import pendulum as p
from pathlib import Path
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Setup environnement
# ─────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))
load_dotenv(ROOT / ".env")

from stravation.services.google_sheets import GoogleSheetsClient

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SHEET_NAME = "Strava"
STRAVA_ID_COL = 12   # M
DURATION_COL = 13    # N

STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")
GOOGLE_SHEETS_ID     = os.getenv("GOOGLE_SHEETS_ID")

if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN]):
    raise RuntimeError("❌ Variables Strava manquantes dans le .env")

if not GOOGLE_SHEETS_ID:
    raise RuntimeError("❌ GOOGLE_SHEETS_ID manquant dans le .env")


# ─────────────────────────────────────────────
# STRAVA
# ─────────────────────────────────────────────
def get_strava_token() -> str:
    r = httpx.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": STRAVA_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def list_strava_activities(after_ts: int):
    token = get_strava_token()
    page = 1

    while True:
        r = httpx.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={"page": page, "per_page": 100, "after": after_ts},
            timeout=30,
        )
        r.raise_for_status()
        acts = r.json()
        if not acts:
            break
        for a in acts:
            yield a
        page += 1
        time.sleep(0.2)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def refresh_last_days(days: int):
    print(f"🔄 Refresh Google Sheets – {days} derniers jours")

    since = p.now("UTC").subtract(days=days).int_timestamp

    sheets = GoogleSheetsClient(GOOGLE_SHEETS_ID)

    # Lecture des Strava ID existants
    rows = sheets.get_values(SHEET_NAME, "A2:N")
    row_by_strava_id: dict[int, int] = {}

    for i, row in enumerate(rows, start=2):
        try:
            sid = int(row[STRAVA_ID_COL])
            row_by_strava_id[sid] = i
        except Exception:
            continue

    updated = 0

    for a in list_strava_activities(after_ts=since):
        sid = a["id"]
        if sid not in row_by_strava_id:
            continue

        duration_s = int(a.get("moving_time", 0))
        row_index = row_by_strava_id[sid]

        # écriture UNIQUEMENT colonne N
        range_a1 = f"'{SHEET_NAME}'!N{row_index}"
        sheets.service.spreadsheets().values().update(
            spreadsheetId=sheets.spreadsheet_id,
            range=range_a1,
            valueInputOption="USER_ENTERED",
            body={"values": [[duration_s]]},
        ).execute()

        print(f"✔️ {a.get('name','')} → {duration_s}s")
        updated += 1

    print(f"✅ {updated} lignes mises à jour")


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage : python scripts/refresh_sheets_last_days.py <nb_jours>")
        sys.exit(1)

    refresh_last_days(int(sys.argv[1]))
