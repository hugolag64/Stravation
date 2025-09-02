from __future__ import annotations
import os, pathlib, shutil
from typing import List, Dict, Optional
from datetime import datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CACHE_DIR = pathlib.Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
TOKEN_PATH = CACHE_DIR / "gcal_token.json"


def _load_credentials() -> Credentials:
    """
    Charge les creds OAuth 2.0 :
    - d'abord depuis le token cache (.cache/gcal_token.json),
    - sinon via GOOGLE_CREDENTIALS_JSON (contenu du JSON en une ligne),
    - ou via GOOGLE_CREDENTIALS_JSON_FILE (chemin vers le fichier client_secret*.json).

    Au premier run, ouvre le flow local (navigateur).
    """
    creds: Optional[Credentials] = None

    # 1) Token cache
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    # 2) Refresh ou nouveau flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
            file_path = os.getenv("GOOGLE_CREDENTIALS_JSON_FILE", "")

            if not raw and not file_path:
                raise RuntimeError(
                    "Fournis GOOGLE_CREDENTIALS_JSON (contenu minifié en une seule ligne) "
                    "ou GOOGLE_CREDENTIALS_JSON_FILE (chemin du fichier credentials.json)."
                )

            client_path = CACHE_DIR / "client_secret.json"
            if raw:
                client_path.write_text(raw, encoding="utf-8")
            else:
                src = pathlib.Path(file_path).expanduser()
                if not src.exists():
                    raise FileNotFoundError(f"Fichier credentials introuvable: {src}")
                shutil.copyfile(src, client_path)

            flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
            creds = flow.run_local_server(port=0)
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _service():
    return build("calendar", "v3", credentials=_load_credentials(), cache_discovery=False)


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
        body={"summary": summary, "timeZone": os.getenv("SPORT_TZ", "Indian/Reunion")}
    ).execute()
    # s’abonner pour qu’il apparaisse dans la liste
    svc.calendarList().insert(body={"id": new_cal["id"]}).execute()
    return new_cal["id"]


def list_events(calendar_id: str, time_min_iso: str, time_max_iso: str) -> List[Dict]:
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
    start_iso: str,  # "YYYY-MM-DDTHH:MM:SS+04:00"
    duration_min: int,
    title: str,
    description: str = "",
    external_key: Optional[str] = None,  # ex. notion_page_id
    color_id: str = "9",  # Blueberry
):
    """
    Crée/met à jour un event (clé = extendedProperties.private.notion_page_id).
    Si external_key est None, on ne fait que CREATE (pas d’upsert).
    """
    svc = _service()
    start = datetime.fromisoformat(start_iso)
    end = start + timedelta(minutes=duration_min or 60)

    body = {
        "summary": title,
        "description": description,
        "colorId": color_id,
        "start": {"dateTime": start.isoformat(), "timeZone": os.getenv("SPORT_TZ", "Indian/Reunion")},
        "end": {"dateTime": end.isoformat(), "timeZone": os.getenv("SPORT_TZ", "Indian/Reunion")},
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
