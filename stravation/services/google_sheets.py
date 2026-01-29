from __future__ import annotations

import os
from pathlib import Path
from typing import List

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────
def _get_credentials() -> Credentials:
    """
    Authentification via compte de service (service_account.json).
    """
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    key_path = Path(service_account_file)

    if not key_path.exists():
        raise FileNotFoundError(
            f"Fichier de compte de service introuvable : {key_path.resolve()}\n"
            "Vérifie que 'service_account.json' est bien à la racine du projet "
            "et que GOOGLE_SERVICE_ACCOUNT_FILE est correct dans le .env."
        )

    return Credentials.from_service_account_file(
        str(key_path),
        scopes=SCOPES,
    )


# ─────────────────────────────────────────────────────────────
# CLIENT SHEETS
# ─────────────────────────────────────────────────────────────
class GoogleSheetsClient:
    def __init__(self, spreadsheet_id: str):
        creds = _get_credentials()
        self.service = build("sheets", "v4", credentials=creds)
        self.spreadsheet_id = spreadsheet_id

    # ─────────────────────────────────────────────────────────
    # APPEND
    # ─────────────────────────────────────────────────────────
    def append_row(self, sheet_name: str, values: List):
        """Ajoute une ligne en bas de la feuille."""
        body = {"values": [values]}
        sheet_range = f"'{sheet_name}'!A1"

        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=sheet_range,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()

    # ─────────────────────────────────────────────────────────
    # LECTURE
    # ─────────────────────────────────────────────────────────
    def get_values(self, sheet_name: str, range_a1: str):
        full_range = f"'{sheet_name}'!{range_a1}"
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=full_range,
        ).execute()
        return resp.get("values", [])

    # ─────────────────────────────────────────────────────────
    # WRITE EXACT ROW (NO INSERT)
    # ─────────────────────────────────────────────────────────
    def write_row(self, sheet_name: str, row_index: int, values: List):
        """
        Écrit exactement en A{row_index}, sans insertion.
        """
        range_a1 = f"'{sheet_name}'!A{row_index}"
        body = {"values": [values]}

        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=range_a1,
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()

    # ─────────────────────────────────────────────────────────
    # SORT (TRI)
    # ─────────────────────────────────────────────────────────
    def _get_sheet_id(self, sheet_name: str) -> int:
        metadata = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id
        ).execute()

        for sheet in metadata.get("sheets", []):
            if sheet["properties"]["title"] == sheet_name:
                return sheet["properties"]["sheetId"]

        raise ValueError(f"Onglet '{sheet_name}' introuvable dans le fichier Sheets.")

    def sort_by_date_desc(self, sheet_name: str, header_rows: int = 1):
        """
        Trie la feuille par date décroissante (colonne B).
        """
        sheet_id = self._get_sheet_id(sheet_name)

        body = {
            "requests": [
                {
                    "sortRange": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": header_rows,
                            "startColumnIndex": 0,
                            "endColumnIndex": 13,  # Colonnes A → M
                        },
                        "sortSpecs": [
                            {
                                "dimensionIndex": 1,  # colonne B
                                "sortOrder": "DESCENDING",
                            }
                        ],
                    }
                }
            ]
        }

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body=body,
        ).execute()
