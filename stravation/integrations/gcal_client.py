from __future__ import annotations
import os, json, hashlib
from typing import Optional, Dict, Any
import pendulum as p
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from ..config import (
    TZ, GOOGLE_CREDENTIALS_PATH, GOOGLE_CREDENTIALS_JSON, TOKEN_PATH, SPORT_CALENDAR_NAME
)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

def _load_credentials() -> Credentials:
    creds: Optional[Credentials] = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if GOOGLE_CREDENTIALS_JSON:
                data = json.loads(GOOGLE_CREDENTIALS_JSON)
                flow = InstalledAppFlow.from_client_config(data, SCOPES)
            else:
                if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
                    raise FileNotFoundError("credentials.json introuvable. Défini GOOGLE_CREDENTIALS_JSON ou place le fichier.")
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(open_browser=True, port=0)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds

def _service():
    creds = _load_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def get_or_create_calendar_id(name: str = SPORT_CALENDAR_NAME) -> str:
    svc = _service()
    cals = svc.calendarList().list().execute()
    for item in cals.get("items", []):
        if item.get("summary") == name:
            return item["id"]
    created = svc.calendars().insert(body={"summary": name, "timeZone": TZ}).execute()
    svc.calendarList().insert(body={"id": created["id"]}).execute()
    return created["id"]

def _stable_event_id(*parts: str) -> str:
    raw = "::".join(parts).encode()
    return f"sn-{hashlib.sha1(raw).hexdigest()}"

def upsert_event(calendar_id: str, event_id: str, body: Dict[str, Any]) -> str:
    svc = _service()
    try:
        created = svc.events().insert(calendarId=calendar_id, body={**body, "id": event_id}).execute()
        return created["id"]
    except Exception:
        updated = svc.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
        return updated["id"]

def push_session(calendar_id: str, *, title: str, date: p.DateTime, start_hm: str,
                 duration_min: int, description: str, reminder_minutes_before: int = 30) -> str:
    start = date.replace(hour=int(start_hm[:2]), minute=int(start_hm[3:5]), second=0)
    end = start.add(minutes=duration_min)
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start.in_timezone("UTC").to_iso8601_string(), "timeZone": TZ},
        "end":   {"dateTime": end.in_timezone("UTC").to_iso8601_string(), "timeZone": TZ},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes_before}]},
    }
    eid = _stable_event_id("session", title, start.to_date_string())
    return upsert_event(calendar_id, eid, body)

def push_morning_reminder(calendar_id: str, *, title_line: str, date: p.DateTime, morning_hm: str) -> str:
    t = date.replace(hour=int(morning_hm[:2]), minute=int(morning_hm[3:5]), second=0)
    body = {
        "summary": "🟦 Séance du jour",
        "description": title_line,
        "start": {"dateTime": t.in_timezone("UTC").to_iso8601_string(), "timeZone": TZ},
        "end":   {"dateTime": t.add(minutes=5).in_timezone("UTC").to_iso8601_string(), "timeZone": TZ},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
    }
    eid = _stable_event_id("morning", title_line, date.to_date_string())
    return upsert_event(calendar_id, eid, body)
