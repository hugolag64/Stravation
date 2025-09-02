# services/google_calendar.py
from __future__ import annotations
import os
import json
import pathlib
from typing import List, Dict, Optional
from datetime import datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from stravation.utils.envtools import load_dotenv_if_exists

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
# Scope large: création/lecture/màj de calendriers + événements.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

CACHE_DIR = pathlib.Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
TOKEN_PATH = CACHE_DIR / "gcal_token.json"

# Priorité à GOOGLE_CREDENTIALS_JSON (JSON minifié sur une seule ligne),
# sinon GOOGLE_CREDENTIALS_PATH (chemin vers credentials.json).
ENV_JSON = "GOOGLE_CREDENTIALS_JSON"
ENV_PATH = "GOOGLE_CREDENTIALS_PATH"


def _sport_tz() -> str:
    return os.getenv("SPORT_TZ", "Indian/Reunion")


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────
def _load_credentials() -> Credentials:
    """
    Charge/rafraîchit les credentials OAuth2.
    1) Tente depuis le token cache
    2) Sinon, ouvre un flow basé sur GOOGLE_CREDENTIALS_JSON ou GOOGLE_CREDENTIALS_PATH
    """
    load_dotenv_if_exists()

    creds: Optional[Credentials] = None

    # 1) Token cache
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    # 2) Refresh ou consent initial
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Refresh silencieux
            creds.refresh(Request())
        else:
            json_str = os.getenv(ENV_JSON, "").strip()
            path_str = os.getenv(ENV_PATH, "").strip()

            if json_str:
                # JSON compact en .env → flow depuis dict
                try:
                    client_config = json.loads(json_str)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"{ENV_JSON} invalide (JSON non décodable) : {e}"
                    )
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            elif path_str:
                p = pathlib.Path(path_str).expanduser()
                if not p.exists():
                    raise FileNotFoundError(f"credentials.json introuvable : {p}")
                flow = InstalledAppFlow.from_client_secrets_file(str(p), SCOPES)
            else:
                raise RuntimeError(
                    f"Aucun credentials Google détecté. Renseigne {ENV_JSON} (JSON compact) "
                    f"ou {ENV_PATH} (chemin vers credentials.json)."
                )

            # Ouvre le navigateur pour consentir (une seule fois)
            creds = flow.run_local_server(port=0, prompt="consent")

        # Persist token
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _service():
    """Client Google Calendar authentifié (discovery cache désactivé)."""
    return build("calendar", "v3", credentials=_load_credentials(), cache_discovery=False)


# ─────────────────────────────────────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────────────────────────────────────
def ensure_calendar(summary: str) -> str:
    """Retourne l'ID d'un calendrier nommé `summary`, en le créant si besoin."""
    svc = _service()
    cals = svc.calendarList().list().execute().get("items", [])
    for c in cals:
        if c.get("summary") == summary:
            return c["id"]

    # create
    new_cal = svc.calendars().insert(
        body={"summary": summary, "timeZone": _sport_tz()}
    ).execute()

    # s’abonner pour qu’il apparaisse dans la liste
    svc.calendarList().insert(body={"id": new_cal["id"]}).execute()
    return new_cal["id"]


def list_events(calendar_id: str, time_min_iso: str, time_max_iso: str) -> List[Dict]:
    """Liste les événements [timeMin; timeMax[ triés par startTime."""
    svc = _service()
    res = (
        svc.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return res.get("items", [])


def upsert_sport_event(
    *,
    calendar_id: str,
    start_iso: str,            # "YYYY-MM-DDTHH:MM:SS+04:00"
    duration_min: int,
    title: str,
    description: str = "",
    external_key: Optional[str] = None,  # ex. notion_page_id
    color_id: str = "9",                 # Blueberry
) -> str:
    """
    Crée/met à jour un event (clé = extendedProperties.private.notion_page_id).
    Si external_key est None, on ne fait que CREATE (pas d’upsert).
    Retourne l'eventId.
    """
    svc = _service()
    start = datetime.fromisoformat(start_iso)
    end = start + timedelta(minutes=duration_min or 60)

    body = {
        "summary": title,
        "description": description,
        "colorId": color_id,
        "start": {"dateTime": start.isoformat(), "timeZone": _sport_tz()},
        "end": {"dateTime": end.isoformat(), "timeZone": _sport_tz()},
    }
    if external_key:
        body["extendedProperties"] = {"private": {"notion_page_id": external_key}}

    # Upsert si external_key présent
    if external_key:
        existing = (
            svc.events()
            .list(
                calendarId=calendar_id,
                privateExtendedProperty=f"notion_page_id={external_key}",
                timeMin=(start - timedelta(days=2)).isoformat(),
                timeMax=(end + timedelta(days=2)).isoformat(),
                singleEvents=True,
            )
            .execute()
            .get("items", [])
        )
        if existing:
            evt_id = existing[0]["id"]
            svc.events().update(calendarId=calendar_id, eventId=evt_id, body=body).execute()
            return evt_id

    created = svc.events().insert(calendarId=calendar_id, body=body).execute()
    return created["id"]


def month_shifts(calendar_id: str, month_start_iso: str, month_end_iso: str) -> Dict[str, str]:
    """
    Retourne { 'YYYY-MM-DD': 'A'|'B'|'C'|'W'|<titre court> } pour les events trouvés.
    """
    events = list_events(calendar_id, month_start_iso, month_end_iso)
    out: Dict[str, str] = {}
    for e in events:
        start = e.get("start", {})
        dt = start.get("dateTime") or (start.get("date") + "T00:00:00+00:00")
        d = datetime.fromisoformat(dt).date().isoformat()
        summ = (e.get("summary") or "").strip()
        # Heuristique: on garde 1er mot (A/B/C/W, etc.)
        short = summ.split()[0][:3] if summ else ""
        if short:
            out[d] = short
    return out
