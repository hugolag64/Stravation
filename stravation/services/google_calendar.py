# stravation/services/google_calendar.py
from __future__ import annotations

import os
import re
import json
import pathlib
from typing import List, Dict, Optional, Iterable, Tuple
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCreds
from google.oauth2.service_account import Credentials as ServiceCreds
from google_auth_oauthlib.flow import InstalledAppFlow

from stravation.utils.envtools import load_dotenv_if_exists

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/calendar"]
ENV_JSON  = "GOOGLE_CREDENTIALS_JSON"        # JSON inline (client OAuth Desktop ou Service Account)
ENV_PATH  = "GOOGLE_CREDENTIALS_PATH"        # chemin vers credentials.json
ENV_TOKEN = "GOOGLE_TOKEN_PATH"              # chemin token OAuth (défaut: token.json)

# Agenda par défaut pour le sport (ID OU NOM). Fallback sur WORK_CALENDAR_ID.
ENV_SPORT_CAL = "SPORT_CALENDAR_ID"

# Détection des shifts dans le titre (A/B/C/W). Adapte ici si besoin.
_SHIFT_RE = re.compile(r"\b(A|B|C|W)\b", re.I)

# Mapping couleur Google Calendar (string "1".."11"). Ajuste à ton goût.
SPORT_COLOR_MAP: Dict[str, str] = {
    "Course": "9",
    "Trail": "10",
    "Vélo": "2",
    "Cyclisme": "2",
    "Natation": "7",
    "CrossFit": "11",
    "Hyrox": "11",
    "CAP": "9",
}


def _sport_tz() -> str:
    # Par défaut: fuseau de La Réunion
    return os.getenv("SPORT_TZ", "Indian/Reunion")


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────
def _load_credentials():
    """
    Charge des credentials Google (Service Account OU OAuth utilisateur).
    Priorité:
      1) GOOGLE_CREDENTIALS_JSON (JSON compact)
      2) GOOGLE_CREDENTIALS_PATH (fichier credentials.json)
    Pour OAuth, le token est persistant dans GOOGLE_TOKEN_PATH (ou token.json).
    """
    load_dotenv_if_exists()

    token_path = pathlib.Path(os.getenv(ENV_TOKEN, "token.json"))

    # client config depuis JSON inline ou fichier
    json_inline = (os.getenv(ENV_JSON) or "").strip()
    path_str = (os.getenv(ENV_PATH) or "").strip()

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

    # OAuth utilisateur (client 'Desktop app')
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


# Alias public (plus clair côté appelants/CLI)
def get_service():
    return _service()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers Calendrier
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_calendar_id(service, hint: str) -> str:
    """
    Si 'hint' contient '@' → on considère que c'est déjà un ID.
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
    # pas trouvé: on renvoie tel quel; le check suivant lèvera une erreur lisible
    return hint


def _get_calendar_entry_from_list(service, cal_id: str) -> Optional[Dict]:
    """
    Cherche cal_id dans la calendarList (agendas visibles pour l'utilisateur OAuth).
    Retourne l'entrée (id, summary, accessRole, etc.) ou None si absent.
    """
    page = None
    while True:
        resp = service.calendarList().list(maxResults=250, pageToken=page).execute()
        for it in resp.get("items", []):
            if it.get("id") == cal_id:
                return it
        page = resp.get("nextPageToken")
        if not page:
            break
    return None


def _assert_visible_calendar(service, cal_id: str):
    """
    Vérifie que 'cal_id' est bien **visible** (présent dans calendarList).
    On évite calendars().get (peut renvoyer 404 même si l'agenda existe mais non abonné).
    """
    if cal_id.lower() == "primary":
        return
    entry = _get_calendar_entry_from_list(service, cal_id)
    if entry:
        return

    # message explicite pour debug
    # NB: on évite l'API oauth2; on reste neutre
    raise RuntimeError(
        f"Calendrier non visible pour l'utilisateur OAuth courant : '{cal_id}'. "
        "Abonne-toi à cet agenda (Google Agenda ▸ « + » ▸ S’abonner) "
        "ou partage-le avec ce compte. "
        "Astuce: tu peux mettre le NOM de l'agenda dans WORK_CALENDAR_ID/SPORT_CALENDAR_ID, il sera résolu."
    )


def assert_can_write_calendar(service, cal_id_or_name: str):
    """
    Vérifie visibilité + droits d'écriture (owner/writer) sur l'agenda donné (ID ou nom).
    Lève une RuntimeError lisible sinon.
    """
    cal_id = _resolve_calendar_id(service, cal_id_or_name)
    _assert_visible_calendar(service, cal_id)
    entry = _get_calendar_entry_from_list(service, cal_id)
    role = (entry or {}).get("accessRole")
    if role not in {"owner", "writer"}:
        raise RuntimeError(
            f"Accès insuffisant sur '{cal_id}' (accessRole={role}). "
            f"Demande « Apporter des modifications aux événements » au propriétaire."
        )
    return cal_id  # pratique si on a passé un nom


def _default_sport_calendar_hint() -> Optional[str]:
    return os.getenv(ENV_SPORT_CAL) or os.getenv("WORK_CALENDAR_ID")


def _as_localized_dt(dt_like: datetime | str) -> datetime:
    """
    Force un datetime timezone-aware en timezone SPORT_TZ.
    - Si str → fromisoformat (supporte 'YYYY-MM-DDTHH:MM:SS+04:00' ou sans TZ)
    - Si naïf → on applique SPORT_TZ.
    - Si aware → conservé tel quel.
    """
    if isinstance(dt_like, str):
        dt = datetime.fromisoformat(dt_like)
    else:
        dt = dt_like

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        # assigner la tz du sport
        # NB: datetime n'a pas de zone DB; on applique l'offset courant
        # Pour simplicité: on convertit via offset fixe à partir de now dans SPORT_TZ.
        # Ici on laisse le champ timeZone côté API expliciter la zone.
        return dt.replace(tzinfo=timezone.utc).astimezone(timezone.utc)
    return dt


def _choose_color_for_sport(sport: Optional[str], fallback: str = "9") -> str:
    if not sport:
        return fallback
    return SPORT_COLOR_MAP.get(sport, fallback)


# ─────────────────────────────────────────────────────────────────────────────
# API de plus haut niveau (utilisées par l'app)
# ─────────────────────────────────────────────────────────────────────────────
def ensure_calendar(summary: str) -> str:
    """
    Retourne l'ID d'un calendrier nommé `summary`, en le créant si besoin et en s'y abonnant.
    """
    svc = _service()

    # Cherche par NOM d'abord
    page = None
    while True:
        resp = svc.calendarList().list(maxResults=250, pageToken=page).execute()
        for c in resp.get("items", []):
            if (c.get("summary") or "") == summary:
                return c["id"]
        page = resp.get("nextPageToken")
        if not page:
            break

    # Crée puis s'abonne
    new_cal = svc.calendars().insert(
        body={"summary": summary, "timeZone": _sport_tz()}
    ).execute()
    # s'assurer qu'il apparaisse dans la calendarList (rarement nécessaire, mais safe)
    try:
        svc.calendarList().insert(body={"id": new_cal["id"]}).execute()
    except Exception:
        pass
    return new_cal["id"]


def list_events(calendar_hint: str, time_min_iso: str, time_max_iso: str) -> List[Dict]:
    """
    Liste les événements [timeMin; timeMax[ triés par startTime.
    `calendar_hint` peut être un ID (…@group.calendar.google.com) ou un NOM (ex. 'Travail').
    """
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
    calendar_id: str,           # ID direct (utilise assert_can_write_calendar si tu as un nom)
    start_iso: str,             # "YYYY-MM-DDTHH:MM:SS+04:00"
    duration_min: int,
    title: str,
    description: str = "",
    external_key: Optional[str] = None,  # ex. notion_page_id
    color_id: str = "9",                 # "Blueberry" (palette Google)
) -> str:
    """
    Crée/Met à jour un event (clé = extendedProperties.private.notion_page_id).
    Si external_key est None, on fait un CREATE simple (pas d’upsert).
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


# ─────────────────────────────────────────────────────────────────────────────
# Nouveau: création simple d'un event "Sport" + description enrichie + couleur
# ─────────────────────────────────────────────────────────────────────────────
def push_sport_event(
    summary: str,
    start_local: datetime | str,
    duration_min: int = 60,
    sport: Optional[str] = None,
    types: Optional[Iterable[str]] = None,
    calendar_hint: Optional[str] = None,     # ID OU NOM ; défaut ENV_SPORT_CAL → WORK_CALENDAR_ID → "primary"
    external_key: Optional[str] = None,      # pour upsert (ex: notion_page_id)
) -> Dict:
    """
    Crée (ou met à jour si external_key) un événement "Sport".
    - summary: titre (ex: 'Séance CAP – Endurance')
    - start_local: datetime (aware de préférence) ou ISO string ; si naïf → SPORT_TZ
    - duration_min: durée en minutes
    - sport: string (pour colorisation + description)
    - types: itérable de tags (affichés dans description)
    - calendar_hint: id ou nom d’agenda (sinon ENV_SPORT_CAL/work/primary)
    - external_key: si fournie → upsert via extendedProperties.private.notion_page_id
    Retourne l’event créé/mis à jour (dict API).
    """
    svc = _service()

    # Résolution de l'agenda cible
    hint = (calendar_hint or _default_sport_calendar_hint() or "primary").strip()
    cal_id = assert_can_write_calendar(svc, hint)

    # Prépare le corps
    start_dt = _as_localized_dt(start_local)
    end_dt = start_dt + timedelta(minutes=duration_min or 60)

    desc_lines: List[str] = []
    if sport:
        desc_lines.append(f"Sport : {sport}")
    if types:
        t = ", ".join([str(x) for x in types])
        desc_lines.append(f"Type : {t}")
    description = "\n".join(desc_lines) if desc_lines else ""

    body = {
        "summary": summary,
        "description": description or None,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": _sport_tz(),
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": _sport_tz(),
        },
        "colorId": _choose_color_for_sport(sport, fallback="9"),
    }
    if external_key:
        body["extendedProperties"] = {"private": {"notion_page_id": external_key}}

    # Upsert si external_key
    if external_key:
        existing = (
            svc.events()
            .list(
                calendarId=cal_id,
                privateExtendedProperty=f"notion_page_id={external_key}",
                timeMin=(start_dt - timedelta(days=2)).isoformat(),
                timeMax=(end_dt + timedelta(days=2)).isoformat(),
                singleEvents=True,
            )
            .execute()
            .get("items", [])
        )
        if existing:
            evt_id = existing[0]["id"]
            updated = svc.events().update(calendarId=cal_id, eventId=evt_id, body=body).execute()
            return updated

    created = svc.events().insert(calendarId=cal_id, body=body).execute()
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Comptage par jour (pour badges UI calendrier)
# ─────────────────────────────────────────────────────────────────────────────
def events_count_by_day(calendar_hint: str, time_min_iso: str, time_max_iso: str) -> Dict[str, int]:
    """
    Retourne un dict { 'YYYY-MM-DD': count } sur l’intervalle [timeMin; timeMax[.
    Idéal pour afficher des pastilles par case dans la grille calendrier.
    """
    items = list_events(calendar_hint, time_min_iso, time_max_iso)
    counts: Dict[str, int] = {}
    for e in items:
        start = e.get("start", {})
        day = start.get("date") or (start.get("dateTime") or "")[:10]
        if not day:
            continue
        counts[day] = counts.get(day, 0) + 1
    return counts


def month_shifts(calendar_hint: str, month_start_iso: str, month_end_iso: str) -> Dict[str, str]:
    """
    Retourne { 'YYYY-MM-DD': 'A'|'B'|'C'|'W' } pour l’intervalle demandé.
    `calendar_hint` peut être un ID (…@group.calendar.google.com) ou un NOM (ex. 'Travail').
    On cherche la lettre dans le titre via _SHIFT_RE.
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


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics & utilitaires (listing, whoami, etc.)
# ─────────────────────────────────────────────────────────────────────────────
def list_calendars(service) -> List[Dict]:
    """
    Retourne la liste des agendas accessibles pour l'utilisateur OAuth courant.
    Champs utiles: id, summary, accessRole, primary, selected.
    """
    items: List[Dict] = []
    page_token = None
    while True:
        res = service.calendarList().list(pageToken=page_token, maxResults=250).execute()
        items.extend(res.get("items", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    out = []
    for it in items:
        out.append({
            "id": it.get("id"),
            "summary": it.get("summary"),
            "accessRole": it.get("accessRole"),
            "primary": bool(it.get("primary")),
            "selected": it.get("selected"),
        })
    return out


def whoami_email(service) -> Optional[str]:
    """
    Récupère l'adresse 'id' du calendrier primaire (souvent ton email).
    """
    res = service.calendarList().list(maxResults=10).execute()
    for it in res.get("items", []):
        if it.get("primary"):
            return it.get("id")
    return None


# ───────── Petit test en ligne de commande ─────────
if __name__ == "__main__":
    svc = get_service()
    me = whoami_email(svc)
    print(f"[OAuth] connecté en tant que: {me}")

    print("\nAgendas visibles (id | accessRole | primary | selected | summary):")
    for cal in list_calendars(svc):
        print(f"- {cal['id']} | {cal['accessRole']} | primary={cal['primary']} | selected={cal['selected']} | {cal['summary']}")

    # Vérification de droits d'écriture sur SPORT_CALENDAR_ID / WORK_CALENDAR_ID (si défini)
    target_hint = _default_sport_calendar_hint()
    if target_hint:
        try:
            cal_id = assert_can_write_calendar(svc, target_hint)
            print(f"\n[OK] Tu peux écrire dans: {cal_id}")
        except Exception as e:
            print(f"\n[ERR] {e}")
