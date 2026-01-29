from __future__ import annotations

import os

from dotenv import load_dotenv

from stravation.services.google_sheets import GoogleSheetsClient

# Charge ton .env à la racine
load_dotenv()

def main() -> None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_ID manquant dans le .env")

    client = GoogleSheetsClient(spreadsheet_id)

    # ⚠️ UTILISE LE NOM EXACT DE L’ONGLET : "BDD sport"
    client.append_row(
        sheet_name="BDD sport",
        values=[
            "TEST Stravation",                # Nom
            "2025-12-03",                     # Date
            5,                                # Intensité (RPE)
            "Endurance fondamentale",         # Type de séance
            "Trail",                           # Sport
            10.5,                              # Distance (km)
            "01:05:00",                        # Durée (hh:mm)
            "6:10",                            # Allure moy (min/km)
            450,                               # D+ (m)
            145,                               # FC moyenne (bpm)
            80,                                # Charge TRIMP
            "1234567890",                      # Strava ID
            "https://www.strava.com/activities/1234567890",  # Lien Strava
            3900,                              # Durée (s)
        ],
    )

    print("✅ Ligne test envoyée dans Google Sheets.")

if __name__ == "__main__":
    main()
