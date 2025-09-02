# stravation/features/strava_to_notion.py
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Dict, Iterable, Optional, Tuple

import httpx
import pendulum as p
from notion_client import Client as Notion

from ..core.models import StravaActivity, NotionActivity
from ..places import build_activity_place_relations_from_strava  # relations Départ/Arrivée

# ==========================
# Config & état local
# ==========================

DEFAULT_YEARS = int(os.getenv("STRAVA_IMPORT_YEARS", "5"))

STATE_DIR = os.path.join(os.path.expanduser("~"), ".stravation")
STATE_DB = os.path.join(STATE_DIR, "state.sqlite")


def _state_conn() -> sqlite3.Connection:
    os.makedirs(STATE_DIR, exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS seen_activities (strava_id TEXT PRIMARY KEY)")
    return conn


def _get_meta(key: str) -> Optional[str]:
    with _state_conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def _set_meta(key: str, value: str) -> None:
    with _state_conn() as c:
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _is_seen(strava_id: str) -> bool:
    with _state_conn() as c:
        row = c.execute(
            "SELECT 1 FROM seen_activities WHERE strava_id=?", (strava_id,)
        ).fetchone()
        return bool(row)


def _mark_seen(strava_id: str) -> None:
    with _state_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO seen_activities(strava_id) VALUES(?)",
            (strava_id,),
        )


def _clear_seen() -> int:
    with _state_conn() as c:
        cur = c.execute("DELETE FROM seen_activities;")
        return cur.rowcount


# ==========================
# Mapping des sports
# ==========================
SPORT_MAP: Dict[str, str] = {
    # course à pied
    "Run": "🏃‍♂️Course à pied",
    "TrailRun": "🏃Trail",
    # vélo
    "Ride": "🚴Vélo de route",
    "GravelRide": "vélo gravel",
    "MountainBikeRide": "VTT",
    "EMountainBikeRide": "VTTAE",
    "VirtualRide": "home trainer",
    # autres
    "Walk": "marche",
    "Hike": "🏔️ Randonnée",
    "Swim": "natation",
    "Rowing": "rameur",
    "Yoga": "🧘Mobilité",
    "Workout": "🏋️Crossfit",
    "WeightTraining": "musculation",
    "Elliptical": "elliptique",
    "AlpineSki": "ski alpin",
    "NordicSki": "ski de fond",
    "Snowboard": "snowboard",
    "HIIT": "🔥Hyrox",
    "HighIntensityIntervalTraining": "🔥Hyrox",
}

# Surcharge optionnelle via .env
if os.getenv("STRAVA_SPORT_MAP"):
    try:
        SPORT_MAP.update(json.loads(os.environ["STRAVA_SPORT_MAP"]))
    except Exception:
        pass


# ==========================
# Strava
# ==========================

def _get_strava_access_token() -> str:
    url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
    }
    r = httpx.post(url, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _iter_strava_activities(after_epoch: int) -> Iterable[Tuple[StravaActivity, dict]]:
    """
    Itère sur (StravaActivity typé, dict JSON brut) pour pouvoir
    utiliser start_latlng/end_latlng dans places.py
    """
    token = _get_strava_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    page = 1
    per_page = 200
    while True:
        params = {"after": after_epoch, "page": page, "per_page": per_page}
        r = httpx.get(
            "https://www.strava.com/api/v3/athlete/activities",
            params=params,
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        for it in items:
            it.setdefault("start_date_local", None)
            it.setdefault("timezone", None)
            sa = StravaActivity(
                id=it["id"],
                name=it.get("name", ""),
                sport_type=it.get("sport_type") or it.get("type"),
                distance=it.get("distance", 0.0),
                moving_time=it.get("moving_time", 0),
                elapsed_time=it.get("elapsed_time", 0),
                total_elevation_gain=it.get("total_elevation_gain", 0.0) or 0.0,
                start_date=it["start_date"],
                start_date_local=it.get("start_date_local"),
                timezone=it.get("timezone"),
            )
            yield sa, it  # renvoie aussi le JSON brut
        page += 1


# ==========================
# Notion
# ==========================

def _db_schema(notion: Notion, db_id: str) -> Dict[str, str]:
    """Renvoie {nom_propriété: type} d'après la base Notion."""
    info = notion.databases.retrieve(db_id)
    out = {}
    for name, prop in info.get("properties", {}).items():
        out[name] = prop.get("type")
    return out


def _filter_by_strava_id(prop_type: str, strava_id: str) -> dict:
    if prop_type == "number":
        return {"number": {"equals": int(strava_id)}}
    if prop_type == "title":
        return {"title": {"equals": str(strava_id)}}
    # rich_text / text (API = rich_text)
    return {"rich_text": {"equals": str(strava_id)}}


def _find_page_id(notion: Notion, db_id: str, prop_type: str, strava_id: str) -> Optional[str]:
    q = notion.databases.query(
        **{
            "database_id": db_id,
            "filter": {"property": "Strava ID", **_filter_by_strava_id(prop_type, strava_id)},
            "page_size": 1,
        }
    )
    results = q.get("results", [])
    return results[0]["id"] if results else None


def _to_notion_activity(sa: StravaActivity) -> NotionActivity:
    dt = sa.start_dt_local  # déjà en Indian/Reunion via core.models
    week_year, week_num, _ = dt.isocalendar()
    week_iso = f"{week_year}-W{week_num:02d}"
    return NotionActivity(
        strava_id=str(sa.id),
        name=sa.name,
        sport_raw=sa.sport_type,
        sport=SPORT_MAP.get(sa.sport_type, sa.sport_type),
        date_local=dt,
        week_iso=week_iso,
        year=dt.year,
        distance_km=round((sa.distance or 0.0) / 1000.0, 2),
        moving_time_s=int(sa.moving_time or 0),
        elevation_gain_m=float(sa.total_elevation_gain or 0.0),
    )


def _props_for_db(na: NotionActivity, schema: Dict[str, str]) -> dict:
    """Construit seulement les propriétés EXISTANTES dans la DB Notion."""
    props = {}

    # Titre (Nom)
    if schema.get("Nom") == "title":
        props["Nom"] = {"title": [{"text": {"content": na.name}}]}

    # Date
    if schema.get("Date") == "date":
        props["Date"] = {"date": {"start": na.date_local.to_iso8601_string()}}

    # Sélecteur Sport
    if schema.get("Sport") == "select":
        props["Sport"] = {"select": {"name": na.sport}}

    # Statut / Réalisation -> toujours "Terminé" à l'import
    if schema.get("Réalisation") == "status":
        props["Réalisation"] = {"status": {"name": "Terminé"}}
    elif schema.get("Réalisation") == "select":
        props["Réalisation"] = {"select": {"name": "Terminé"}}

    # Chiffres de base
    if schema.get("Distance (km)") == "number":
        props["Distance (km)"] = {"number": na.distance_km}
    if schema.get("Durée (s)") == "number":
        props["Durée (s)"] = {"number": na.moving_time_s}
    if schema.get("D+ (m)") == "number":
        props["D+ (m)"] = {"number": na.elevation_gain_m}
    if schema.get("D- (m)") == "number":
        props["D- (m)"] = {"number": 0}  # Strava n'envoie pas le D- global

    # Strava ID : number / text / rich_text / title
    sid_type = schema.get("Strava ID")
    if sid_type == "number":
        props["Strava ID"] = {"number": int(na.strava_id)}
    elif sid_type == "title":
        props["Strava ID"] = {"title": [{"text": {"content": na.strava_id}}]}
    elif sid_type in ("rich_text", "text", None):
        if "Strava ID" in schema:
            props["Strava ID"] = {"rich_text": [{"text": {"content": na.strava_id}}]}

    # Semaine ISO / Année / Lien
    if schema.get("Semaine ISO") in ("rich_text", "text"):
        props["Semaine ISO"] = {"rich_text": [{"text": {"content": na.week_iso}}]}
    if schema.get("Année") == "number":
        props["Année"] = {"number": na.year}
    if schema.get("Lien Strava") == "url":
        props["Lien Strava"] = {"url": f"https://www.strava.com/activities/{na.strava_id}"}

    return props


def _filter_existing_props(props: dict, schema: Dict[str, str]) -> dict:
    """Retire les propriétés qui n’existent pas dans la DB (évite 400 Notion)."""
    return {k: v for k, v in props.items() if k in schema}


# ==========================
# Entrée principale
# ==========================

def sync_strava_to_notion(
    full: bool = False,
    since_iso: Optional[str] = None,
    *,
    places: bool = True,
) -> Tuple[int, int]:
    """
    Retourne (created_or_updated, already_skipped).

    **Important** :
    - Si full=True OU si l'ENV STRAVATION_FORCE=1, on **ignore le cache seen_activities**
      (utile pour un backfill complet après purge de Notion).
    - since_iso : YYYY-MM-DD (prioritaire si fourni)
    - sinon, horizon = STRAVA_IMPORT_YEARS (défaut: 5)
    - places : True pour créer/mettre à jour les relations Départ/Arrivée (peut être
      mis à False pour accélérer/éviter les erreurs réseau pendant un gros backfill).
    """
    notion = Notion(auth=os.environ["NOTION_API_KEY"])
    db_id = os.environ["NOTION_DB_ACTIVITIES"]

    schema = _db_schema(notion, db_id)
    strava_id_prop_type = schema.get("Strava ID", "rich_text")

    # Mode "force" (ignore seen)
    env_force = os.getenv("STRAVATION_FORCE", "").strip().lower() in {"1", "true", "yes", "on", "y"}
    force = bool(full or env_force)

    # Détermine "after" (epoch seconds)
    if since_iso:
        after_epoch = p.parse(since_iso).set(hour=0, minute=0, second=0, microsecond=0).int_timestamp
    elif full:
        after_epoch = 0  # tout l'historique
    else:
        last = _get_meta("last_sync_epoch")
        if last:
            after_epoch = int(last)
        else:
            after_epoch = p.now().subtract(years=DEFAULT_YEARS).int_timestamp

    created_or_updated = 0
    already = 0

    # Si force explicite, on vide le cache "seen" dès le départ (sécurité)
    if force:
        _clear_seen()

    for sa, raw in _iter_strava_activities(after_epoch=after_epoch):
        na = _to_notion_activity(sa)

        # Idempotence locale : on ne skippe que si **pas** en mode force
        if (not force) and _is_seen(na.strava_id):
            already += 1
            continue

        # propriétés de base (activité)
        props = _props_for_db(na, schema)

        # propriétés relationnelles Départ / Arrivée (si colonnes présentes)
        if places:
            try:
                place_props = build_activity_place_relations_from_strava(raw)
                props.update(_filter_existing_props(place_props, schema))
            except httpx.HTTPError:
                # on ignore les erreurs réseau ponctuelles sur les lieux
                pass

        # upsert Notion par "Strava ID"
        page_id = None
        if "Strava ID" in schema:
            page_id = _find_page_id(notion, db_id, strava_id_prop_type, na.strava_id)

        if page_id:
            notion.pages.update(page_id=page_id, properties=props)
        else:
            notion.pages.create(parent={"database_id": db_id}, properties=props)

        _mark_seen(na.strava_id)
        created_or_updated += 1

        # throttle doux pour Notion
        time.sleep(0.15)

    # checkpoint
    _set_meta("last_sync_epoch", str(int(p.now().int_timestamp)))

    return created_or_updated, already
