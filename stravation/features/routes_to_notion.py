# stravation/features/routes_to_notion.py
from __future__ import annotations

import os
import time
import json
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, Optional, Tuple, List

import httpx
from notion_client import Client as Notion

from ..places import (
    ensure_place_for_coord,                 # crée/maj les lieux + renvoie (page_id, commune)
    PROP_ACT_START_REL, PROP_ACT_END_REL,   # "Départ" / "Arrivée" (relations)
)
# on réutilise la même sanitisation des selects que pour les activités
from ..places import _select_value as _place_select_value  # type: ignore

# -------------------------
# Config
# -------------------------

NOTION_DB_GPX = os.environ["NOTION_DB_GPX"]  # id de la DB "🗺️ Projets GPX"
NOTION_TOKEN  = os.environ["NOTION_API_KEY"]

# mapping sport identique à strava_to_notion (avec l’extension demandée)
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

# surcharge optionnelle via .env
if os.getenv("STRAVA_SPORT_MAP"):
    try:
        SPORT_MAP.update(json.loads(os.environ["STRAVA_SPORT_MAP"]))
    except Exception:
        pass


# -------------------------
# Strava helpers (Routes)
# -------------------------

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


def _iter_strava_routes() -> Iterable[dict]:
    """
    Récupère tous les itinéraires “Mes itinéraires”.
    Strava: GET /api/v3/athlete/routes  (paginé)
    """
    token = _get_strava_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    page, per_page = 1, 200
    client = httpx.Client(timeout=60, headers=headers)
    try:
        while True:
            params = {"page": page, "per_page": per_page}
            r = client.get("https://www.strava.com/api/v3/athlete/routes", params=params)
            r.raise_for_status()
            routes = r.json()
            if not routes:
                break
            for route in routes:
                yield route
            page += 1
    finally:
        client.close()


def _route_type_to_key(rt: dict) -> str:
    """
    Convertit le type/sub_type Strava Route vers une clé de SPORT_MAP.
    - type: 1=Ride, 2=Run
    - sub_type (approx): pour Ride -> 1=Road, 2=MTB, 3=Gravel/CX
                         pour Run  -> 1=Road, 2=Trail, 3=Track
    """
    t = rt.get("type")
    sub = rt.get("sub_type")

    if t == 2:  # Run
        return "TrailRun" if sub == 2 else "Run"
    if t == 1:  # Ride
        if sub == 2:
            return "MountainBikeRide"
        if sub == 3:
            return "GravelRide"
        return "Ride"
    # fallback
    return "Run"


def _export_route_gpx(route_id: int, token: str) -> Optional[str]:
    """
    Télécharge le GPX de la route via l’API (auth requise). Renvoie le texte GPX (str).
    """
    url = f"https://www.strava.com/api/v3/routes/{route_id}/export_gpx"
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get(url, headers=headers, timeout=60)
    if r.status_code == 200 and r.text.strip():
        return r.text
    return None


def _first_last_latlng_from_gpx(gpx_text: str) -> Tuple[Optional[Tuple[float,float]], Optional[Tuple[float,float]]]:
    """
    Extrait (lat,lon) du premier et du dernier point GPX.
    Strava Routes utilisent souvent <rtept>, parfois <trkpt>.
    """
    try:
        root = ET.fromstring(gpx_text)
    except Exception:
        return None, None

    # namespace agnostique
    pts = list(root.findall(".//{*}rtept"))
    if not pts:
        pts = list(root.findall(".//{*}trkpt"))

    def _latlon(elem):
        try:
            return float(elem.attrib["lat"]), float(elem.attrib["lon"])
        except Exception:
            return None

    if not pts:
        return None, None

    start = _latlon(pts[0])
    end   = _latlon(pts[-1])
    return start, end


# -------------------------
# Notion helpers (DB Projets GPX)
# -------------------------

def _db_schema(notion: Notion, db_id: str) -> Dict[str, str]:
    info = notion.databases.retrieve(db_id)
    return {name: prop.get("type") for name, prop in info.get("properties", {}).items()}


def _first_existing(schema: Dict[str, str], candidates: List[str]) -> Optional[str]:
    for name in candidates:
        if name in schema:
            return name
    return None


def _filter_existing_props(props: dict, schema: Dict[str, str]) -> dict:
    return {k: v for k, v in props.items() if k in schema}


def _filter_by_route_id(prop_type: str, route_id: str) -> dict:
    if prop_type == "number":
        return {"number": {"equals": int(route_id)}}
    if prop_type == "title":
        return {"title": {"equals": route_id}}
    return {"rich_text": {"equals": route_id}}


def _find_page_id_by_route_id(notion: Notion, db_id: str, prop_type: str, route_id: str) -> Optional[str]:
    q = notion.databases.query(
        **{
            "database_id": db_id,
            "filter": {"property": "Strava Route ID", **_filter_by_route_id(prop_type, route_id)},
            "page_size": 1,
        }
    )
    res = q.get("results", [])
    return res[0]["id"] if res else None


# -------------------------
# Mapping vers Notion
# -------------------------

def _props_for_routes_db(rt: dict, schema: Dict[str, str], start_city: Optional[str], end_city: Optional[str]) -> dict:
    """
    Construit les propriétés pour la page Notion “Projets GPX”.
    Remplit uniquement ce qui existe dans ta DB (d’après schema).
    """
    props = {}

    # champs variables selon ta DB
    TITLE = _first_existing(schema, ["Nom", "Name", "Titre"])
    SPORT = _first_existing(schema, ["Type sport", "Type"])
    DIST  = _first_existing(schema, ["Distance (km)", "Distance"])
    GAIN  = _first_existing(schema, ["D+ (m)", "D+"])
    FILEU = _first_existing(schema, ["Fichier GPX", "Lien GPX", "GPX"])
    STAT  = _first_existing(schema, ["Statut", "Status"])
    LINK  = _first_existing(schema, ["Lien Strava", "Lien", "URL"])

    # titre
    if TITLE and schema.get(TITLE) == "title":
        props[TITLE] = {"title": [{"text": {"content": rt.get("name", f"Route {rt.get('id')}")}}]}

    # type de sport (select)
    if SPORT and schema.get(SPORT) == "select":
        key = _route_type_to_key(rt)
        props[SPORT] = {"select": {"name": SPORT_MAP.get(key, key)}}

    # distance (m -> km)
    if DIST and schema.get(DIST) == "number":
        dkm = round((rt.get("distance", 0.0) or 0.0) / 1000.0, 2)
        props[DIST] = {"number": dkm}

    # D+ (m)
    if GAIN and schema.get(GAIN) == "number":
        props[GAIN] = {"number": float(rt.get("elevation_gain", 0.0) or 0.0)}

    # lien GPX (URL) — clic direct pour télécharger depuis Strava
    if FILEU and schema.get(FILEU) == "url":
        props[FILEU] = {"url": f"https://www.strava.com/routes/{rt['id']}/export_gpx"}

    # lien Strava de la route (URL)
    if LINK and schema.get(LINK) == "url":
        props[LINK] = {"url": f"https://www.strava.com/routes/{rt['id']}"}

    # statut (status)
    if STAT and schema.get(STAT) == "status":
        props[STAT] = {"status": {"name": "Pas commencé"}}

    # identifiant de déduplication si la colonne existe (number / rich_text / title)
    if "Strava Route ID" in schema:
        t = schema["Strava Route ID"]
        rid = str(rt["id"])
        if t == "number":
            props["Strava Route ID"] = {"number": int(rid)}
        elif t == "title":
            props["Strava Route ID"] = {"title": [{"text": {"content": rid}}]}
        else:  # rich_text
            props["Strava Route ID"] = {"rich_text": [{"text": {"content": rid}}]}

    # éventuelles colonnes "Ville - départ / arrivée"
    if start_city and "Ville - départ" in schema and schema["Ville - départ"] == "select":
        sv = _place_select_value(start_city)
        if sv:
            props["Ville - départ"] = sv
    if end_city and "Ville - arrivée" in schema and schema["Ville - arrivée"] == "select":
        ev = _place_select_value(end_city)
        if ev:
            props["Ville - arrivée"] = ev

    return props


# -------------------------
# Sync principal
# -------------------------

def sync_strava_routes_to_notion() -> Tuple[int, int]:
    """
    Importe/Met à jour tous les itinéraires Strava dans la DB “🗺️ Projets GPX”.
    - upsert via la colonne optionnelle "Strava Route ID" si elle existe
    - crée les relations Départ/Arrivée vers Lieux si possible (GPX parsé)
    - renseigne Ville - départ / Ville - arrivée (selects) si présentes
    Retourne (créés_ou_mis_a_jour, déjà_existant_sans_maj).
    """
    notion = Notion(auth=NOTION_TOKEN)
    schema = _db_schema(notion, NOTION_DB_GPX)

    # champs relations (si présents dans ta DB GPX)
    has_start_rel = (PROP_ACT_START_REL in schema and schema[PROP_ACT_START_REL] == "relation")
    has_end_rel   = (PROP_ACT_END_REL   in schema and schema[PROP_ACT_END_REL]   == "relation")

    route_id_prop_type = schema.get("Strava Route ID", "rich_text")
    created_or_updated, skipped = 0, 0

    token = _get_strava_access_token()

    for rt in _iter_strava_routes():
        # start/end depuis le GPX Strava (on récupère aussi les villes via places.ensure_place_for_coord)
        start_city = end_city = None
        start_rel_id = end_rel_id = None
        try:
            gpx = _export_route_gpx(rt["id"], token)
            if gpx:
                start_ll, end_ll = _first_last_latlng_from_gpx(gpx)
                if start_ll:
                    start_rel_id, start_city = ensure_place_for_coord(list(start_ll))
                if end_ll:
                    end_rel_id, end_city = ensure_place_for_coord(list(end_ll))
        except Exception:
            # silencieux: si on ne peut pas extraire, on n’échoue pas l’import
            pass

        props = _props_for_routes_db(rt, schema, start_city, end_city)

        # relations vers Lieux si dispo
        if has_start_rel and start_rel_id:
            props[PROP_ACT_START_REL] = {"relation": [{"id": start_rel_id}]}
        if has_end_rel and end_rel_id:
            props[PROP_ACT_END_REL] = {"relation": [{"id": end_rel_id}]}

        # upsert par "Strava Route ID" si présent
        page_id = None
        if "Strava Route ID" in schema:
            page_id = _find_page_id_by_route_id(notion, NOTION_DB_GPX, route_id_prop_type, str(rt["id"]))

        if page_id:
            notion.pages.update(page_id=page_id, properties=props)
            created_or_updated += 1
        else:
            notion.pages.create(parent={"database_id": NOTION_DB_GPX}, properties=props)
            created_or_updated += 1

        time.sleep(0.15)  # ménage l’API

    return created_or_updated, skipped
