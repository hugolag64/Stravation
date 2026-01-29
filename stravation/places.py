# stravation/places.py
from __future__ import annotations

import os
import time
import functools
import httpx

# ── Notion config ─────────────────────────────────────────────────────────────
NOTION_TOKEN = os.environ["NOTION_API_KEY"]
DB_PLACES_ID = os.environ["NOTION_DB_PLACES"]  # id de la DB "Lieux"
NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ---- Noms exacts des propriétés dans Notion (Lieux) ----
PROP_PLACES_TITLE   = "Commune"                 # <- c'est bien le Titre (Aa)
PROP_PLACES_LAT     = "Latitude"
PROP_PLACES_LON     = "Longitude"
PROP_PLACES_COUNTRY = "Pays"
PROP_PLACES_REGION  = "Région/Département"

# ---- Noms des propriétés dans la DB "BDD sport" ----
PROP_ACT_START_REL   = "Départ"                 # relation vers Lieux
PROP_ACT_END_REL     = "Arrivée"                # relation vers Lieux
PROP_ACT_CITY_START  = "Ville - départ"         # select
PROP_ACT_CITY_END    = "Ville - arrivée"        # select

# ── HTTP helpers avec retry ───────────────────────────────────────────────────
def _notion_request(method: str, path: str, *, json: dict | None = None,
                    max_retries: int = 5):
    """
    Appel Notion avec retry exponentiel.
    Gère ReadTimeout, connect errors, 429 (Retry-After), et 5xx.
    """
    backoff = 1.0
    url = f"{NOTION_API}{path}"
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=httpx.Timeout(90.0)) as c:
                r = c.request(method, url, headers=NOTION_HEADERS, json=json)
                if r.status_code == 429:
                    wait = float(r.headers.get("Retry-After", "3"))
                    time.sleep(wait)
                    # relance après pause
                    raise httpx.HTTPStatusError("Rate limited",
                                                request=r.request, response=r)
                r.raise_for_status()
                return r.json() if r.content else {}
        except (httpx.ReadTimeout, httpx.ConnectError,
                httpx.RemoteProtocolError, httpx.HTTPStatusError) as e:
            if attempt == max_retries:
                # remonte l'erreur après le dernier essai
                raise
            time.sleep(backoff)
            backoff = min(backoff * 1.8, 12.0)

def _notion_post(path, json):  return _notion_request("POST",  path, json=json)
def _notion_patch(path, json): return _notion_request("PATCH", path, json=json)
def _notion_get(path):         return _notion_request("GET",   path)

# ── Schéma DB Lieux ───────────────────────────────────────────────────────────
_PLACES_SCHEMA_CACHE: dict[str, str] | None = None

def _places_schema() -> dict[str, str]:
    """Retourne {nom_propriété: type} pour la DB Lieux (mise en cache)."""
    global _PLACES_SCHEMA_CACHE
    if _PLACES_SCHEMA_CACHE is None:
        info = _notion_get(f"/databases/{DB_PLACES_ID}")
        props = info.get("properties", {})
        _PLACES_SCHEMA_CACHE = {name: p.get("type") for name, p in props.items()}
    return _PLACES_SCHEMA_CACHE

def _as_prop(name: str, value):
    """
    Construit la valeur Notion correcte en fonction du type réel de la propriété.
    Gère: number / select / multi_select / title / rich_text.
    """
    if value is None:
        return None
    ptype = _places_schema().get(name)
    if ptype == "number":
        try:
            v = float(value)
        except Exception:
            v = None
        return {"number": v} if v is not None else None
    if ptype == "select":
        return {"select": {"name": str(value)}}
    if ptype == "multi_select":
        return {"multi_select": [{"name": str(value)}]}
    if ptype == "title":
        return {"title": [{"text": {"content": str(value)}}]}
    # fallback: rich_text (ou quand la prop n'existe pas)
    return {"rich_text": [{"text": {"content": str(value)}}]}

def _clean_props(props: dict) -> dict:
    # enlève les clés None pour éviter les 400 Notion
    return {k: v for k, v in props.items() if v is not None}

def _select_value(name: str | None):
    """Nettoie un libellé pour un select Notion (Notion n'accepte pas les virgules)."""
    if not name:
        return None
    cleaned = str(name).replace(",", "·").strip()
    if not cleaned:
        return None
    if len(cleaned) > 100:
        cleaned = cleaned[:100]
    return {"select": {"name": cleaned}}

# ── Requêtes DB Lieux ─────────────────────────────────────────────────────────
def _query_place_by_commune(commune: str):
    payload = {
        "filter": {"property": PROP_PLACES_TITLE, "title": {"equals": commune}},
        "page_size": 1,
    }
    data = _notion_post(f"/databases/{DB_PLACES_ID}/query", payload)
    results = data.get("results", [])
    return results[0] if results else None

def _create_place(commune, lat, lon, country=None, region=None):
    props = _clean_props({
        PROP_PLACES_TITLE:   _as_prop(PROP_PLACES_TITLE, commune),
        PROP_PLACES_LAT:     _as_prop(PROP_PLACES_LAT, lat),
        PROP_PLACES_LON:     _as_prop(PROP_PLACES_LON, lon),
        PROP_PLACES_COUNTRY: _as_prop(PROP_PLACES_COUNTRY, country),
        PROP_PLACES_REGION:  _as_prop(PROP_PLACES_REGION, region),
    })
    data = _notion_post("/pages", {
        "parent": {"database_id": DB_PLACES_ID},
        "properties": props
    })
    return data["id"]

def _update_place_if_needed(page_id, lat, lon, country=None, region=None):
    props = _clean_props({
        PROP_PLACES_LAT:     _as_prop(PROP_PLACES_LAT, lat),
        PROP_PLACES_LON:     _as_prop(PROP_PLACES_LON, lon),
        PROP_PLACES_COUNTRY: _as_prop(PROP_PLACES_COUNTRY, country),
        PROP_PLACES_REGION:  _as_prop(PROP_PLACES_REGION, region),
    })
    if props:
        _notion_patch(f"/pages/{page_id}", {"properties": props})

# ── Reverse geocoding (Nominatim) ─────────────────────────────────────────────
# Politesse Nominatim : 1 req/s et User-Agent parlant
UA = f"stravation/1.0 ({os.getenv('REVERSE_GEO_EMAIL','contact@example.com')})"

@functools.lru_cache(maxsize=5000)
def reverse_geocode(lat_rounded: float, lon_rounded: float):
    # 1 req/s (sémaphore simple)
    time.sleep(1.0)
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat_rounded, "lon": lon_rounded, "format": "jsonv2",
        "zoom": 10, "accept-language": "fr"
    }
    with httpx.Client(timeout=20, headers={"User-Agent": UA}) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        a = r.json().get("address", {})
        commune = a.get("city") or a.get("town") or a.get("village") or \
                  a.get("municipality") or a.get("county")
        region = a.get("state") or a.get("region") or a.get("county")
        country = a.get("country")
        return {"commune": commune, "region": region, "country": country}

def _round_coord(x):
    # arrondi raisonnable pour dédoublonner (≈100 m)
    return None if x is None else round(float(x), 3)

def ensure_place_for_coord(latlng):
    """
    latlng: [lat, lon] strava (peut être None)
    Retourne (page_id Notion du lieu, nom_commune) ou (None, None)
    """
    if not latlng or len(latlng) != 2 or latlng[0] is None or latlng[1] is None:
        return None, None

    lat = _round_coord(latlng[0])
    lon = _round_coord(latlng[1])
    try:
        geo = reverse_geocode(lat, lon)
    except Exception:
        geo = {"commune": None, "region": None, "country": None}

    # Pour le Titre (Commune), la virgule est OK.
    commune = geo.get("commune") or f"{lat},{lon}"
    page = _query_place_by_commune(commune)
    if page:
        page_id = page["id"]
        _update_place_if_needed(page_id, lat, lon, geo.get("country"), geo.get("region"))
        return page_id, commune

    pid = _create_place(commune, lat, lon, geo.get("country"), geo.get("region"))
    return pid, commune

def build_activity_place_relations_from_strava(activity: dict):
    """
    activity: objet Strava (summary/detailed) avec start_latlng/end_latlng
    Retourne un dict de propriétés Notion à fusionner lors du create/update de l’activité :
    - Relations: Départ / Arrivée
    - Selects:   Ville - départ / Ville - arrivée (sanitisés)
    """
    props = {}
    start_rel, start_city = ensure_place_for_coord(activity.get("start_latlng"))
    end_rel,   end_city   = ensure_place_for_coord(activity.get("end_latlng"))

    if start_rel:
        props[PROP_ACT_START_REL] = {"relation": [{"id": start_rel}]}
    if end_rel:
        props[PROP_ACT_END_REL] = {"relation": [{"id": end_rel}]}

    if start_city:
        sv = _select_value(start_city)
        if sv:
            props[PROP_ACT_CITY_START] = sv
    if end_city:
        ev = _select_value(end_city)
        if ev:
            props[PROP_ACT_CITY_END] = ev

    return props
