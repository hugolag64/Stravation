from __future__ import annotations

import os
import sys
import pendulum as p

# ─────────────────────────────────────────────
# Sécurise les imports depuis la racine du projet
# ─────────────────────────────────────────────
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from dotenv import load_dotenv
load_dotenv()

from stravation.services.strava_service import StravaService
from stravation.services.google_sheets import GoogleSheetsClient


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SHEET_NAME = os.getenv("SPORT_SHEET_NAME", "BDD sport")
HEADER_ROWS = 1

# Index colonnes (0-based)
STRAVA_ID_COL = 12   # M
DURATION_COL = 13    # N


def refresh_duration_last_days(days: int):
    print(f"🔄 Refresh Google Sheets – Durée (s) sur {days} derniers jours")

    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise RuntimeError("❌ GOOGLE_SHEETS_ID manquant dans le .env")

    strava = StravaService(per_page=50)
    sheets = GoogleSheetsClient(spreadsheet_id=spreadsheet_id)

    since = p.now("UTC").subtract(days=days)

    # ─────────────────────────────────────────
    # 1️⃣ Lecture des Strava ID existants
    # ─────────────────────────────────────────
    rows = sheets.get_values(SHEET_NAME, "A2:N")
    row_index_by_strava_id: dict[int, int] = {}

    for i, row in enumerate(rows, start=HEADER_ROWS + 1):
        if len(row) <= STRAVA_ID_COL:
            continue
        try:
            strava_id = int(row[STRAVA_ID_COL])
            row_index_by_strava_id[strava_id] = i
        except (ValueError, TypeError):
            continue

    # ─────────────────────────────────────────
    # 2️⃣ Parcours Strava
    # ─────────────────────────────────────────
    page = 1
    updated = 0

    while True:
        activities = strava.list_recent(page=page)
        if not activities:
            break

        for a in activities:
            start = p.parse(a["start_dt_utc"])
            if start < since:
                print("⏹️ Fenêtre temporelle dépassée")
                print(f"✅ {updated} durée(s) mise(s) à jour")
                return

            row_index = row_index_by_strava_id.get(a["id"])
            if row_index is None:
                continue

            duration_s = a["moving_time_s"]

            # ─────────────────────────────────
            # 3️⃣ Écriture ciblée colonne N
            # ─────────────────────────────────
            range_a1 = f"'{SHEET_NAME}'!N{row_index}"

            sheets.service.spreadsheets().values().update(
                spreadsheetId=sheets.spreadsheet_id,
                range=range_a1,
                valueInputOption="USER_ENTERED",
                body={"values": [[duration_s]]},
            ).execute()

            updated += 1
            print(f"✔️ {a['name']} ({a['start_local']}) → {duration_s}s")

        page += 1

    print(f"✅ {updated} durée(s) mise(s) à jour")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage : python scripts/refresh_sheets_duration_last_days.py <nb_jours>")
        sys.exit(1)

    refresh_duration_last_days(int(sys.argv[1]))
