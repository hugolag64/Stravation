# stravation/services/gcal_service.py
from __future__ import annotations

import os
import re
import json
import pathlib
from typing import List, Dict, Optional
from datetime import datetime, timedelta

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCreds
from google.oauth2.service_account import Credentials as ServiceCreds
from google_auth_oauthlib.flow import InstalledAppFlow

from stravation.utils.envtools import load_dotenv_if_exists

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/calendar"]  # lecture/écriture
ENV_JSON  = "GOOGLE_CREDENTIALS_JSON"                  # JSON inline
ENV_PATH  = "GOOGLE_CREDENTIALS_PATH"                  # chemin credentials.json
ENV_TOKEN = "GOOGLE_TOKEN_PATH"                        # chemin token OAuth (défaut: token.json)

# Détection des shifts dans le titre (A/B/C/W). Adapte ici si besoin.
_SHIFT_RE = re.compile(r"\b(A|B|C|W)\b", re.I)


def _sport_tz() -> str:
    return os.getenv("SPORT_TZ", "Indian/Reunion")


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────
def _load_credentials():
    """Charge des credentials Google (Service Account OU OAuth utilisateur)."""
    load_dotenv_if_exists()

    token_path = pathlib.Path(os.getenv(ENV_TOKEN, "token.json"))

    # client config depuis JSON inline ou fichier
    json_inline = os.getenv(ENV_JSON, "").strip()
    path_str = os.getenv(ENV_PATH, "").strip()

    if json_inline:
        try:
            client_cfg = json.loads(json_inline)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{ENV_JSON} invalide (JSON non décodable) : {e}")
    elif path_str:
        p = pathlib.Path(path_str).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"credentials.json introuvable : {p}")
        client_cfg = json.loads(p.read_text(encoding="utf-8"))
    else:
        raise RuntimeError(
            f"Aucun credentials Google détecté. Renseigne {ENV_JSON} (JSON compact) "
            f"ou {ENV_PATH} (chemin vers credentials.json)."
        )

    # Service Account
    if client_cfg.get("type") == "service_account":
        return ServiceCreds.from_service_account_info(client_cfg, scopes=SCOPES)

    # OAuth utilisateur
    creds = None
    if token_path.exists():
        creds = UserCreds.from_authorized_user_file(str(token_path), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_config(client_cfg, SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _service():
    """Client Google Calendar authentifié (discovery cache désactivé)."""
    return build("calendar", "v3", credentials=_load_credentials(), cache_discovery=False)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers Calendrier
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_calendar_id(service, hint: str) -> str:
    """
    Si 'hint' contient '@' → ID direct.
    Sinon, on résout par NOM (summary), insensible à la casse.
    """
    if not hint or hint.lower() == "primary" or "@" in hint:
        return hint or "primary"

    page = None
    while True:
        resp = service.calendarList().list(maxResults=250, pageToken=page).execute()
        for it in resp.get("items", []):
            if (it.get("summary") or "").strip().lower() == hint.strip().lower():
                return it["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    # pas trouvé, on renverra tel quel (mais on vérifiera l'abonnement plus bas)
    return hint


def _assert_visible_calendar(service, cal_id: str):
    """
    Sécurise l'accès : on vérifie que 'cal_id' est bien dans la calendarList
    (donc visible par l'utilisateur OAuth). On ÉVITE calendars().get (source de 404).
    """
    if cal_id.lower() == "primary":
        return

    page = None
    while True:
        resp = service.calendarList().list(maxResults=250, pageToken=page).execute()
        if any((it.get("id") == cal_id) for it in resp.get("items", [])):
            return
        page = resp.get("nextPageToken")
        if not page:
            break

    # message explicite pour debug
    try:
        me = build("oauth2", "v2", credentials=service._http.credentials).userinfo().get().execute().get("email")
    except Exception:
        me = "utilisateur OAuth courant"
    raise RuntimeError(
        f"Calendrier non visible pour {me} : '{cal_id}'. "
        "Abonne-toi à cet agenda (Other calendars ▸ From URL) ou partage-le avec ce compte. "
        "Tu peux aussi mettre le NOM au lieu de l'ID dans WORK_CALENDAR_ID."
    )


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────
def ensure_calendar(summary: str) -> str:
    """
    Retourne l'ID d'un calendrier nommé `summary`,
    en le créant si besoin et en s'y abonnant.
    """
    svc = _service()

    # cherche par NOM
    page = None
    while True:
        resp = svc.calendarList().list(maxResults=250, pageToken=page).execute()
        for c in resp.get("items", []):
            if c.get("summary") == summary:
                return c["id"]
        page = resp.get("nextPageToken")
        if not page:
            break

    # crée puis s'abonne
    new_cal = svc.calendars().insert(
        body={"summary": summary, "timeZone": _sport_tz()}
    ).execute()
    svc.calendarList().insert(body={"id": new_cal["id"]}).execute()
    return new_cal["id"]


def list_events(calendar_hint: str, time_min_iso: str, time_max_iso: str) -> List[Dict]:
    """Liste les événements [timeMin; timeMax[ triés par startTime (ID ou Nom accepté)."""
    svc = _service()
    cal_id = _resolve_calendar_id(svc, calendar_hint)
    _assert_visible_calendar(svc, cal_id)

    res = (
        svc.events()
        .list(
            calendarId=cal_id,
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
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


def month_shifts(calendar_hint: str, month_start_iso: str, month_end_iso: str) -> Dict[str, str]:
    """
    Retourne { 'YYYY-MM-DD': 'A'|'B'|'C'|'W' } pour l’intervalle demandé.
    `calendar_hint` peut être un ID (…@group.calendar.google.com) ou un NOM (ex. 'Travail').
    """
    events = list_events(calendar_hint, month_start_iso, month_end_iso)
    out: Dict[str, str] = {}

    for e in events:
        title = (e.get("summary") or "").strip()
        m = _SHIFT_RE.search(title)
        if not m:
            continue
        code = m.group(1).upper()

        start = e.get("start", {})
        day = start.get("date") or (start.get("dateTime") or "")[:10]
        if day:
            out[day] = code

    return out
