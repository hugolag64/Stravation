# stravation/features/routes_to_notion.py
from __future__ import annotations

import os
import time
import json
import hashlib
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, Optional, Tuple, List
import re
import httpx
import pendulum as p
from notion_client import Client as Notion

from ..places import (
    ensure_place_for_coord,                 # crée/maj les lieux + renvoie (page_id, commune)
    PROP_ACT_START_REL, PROP_ACT_END_REL,   # "Départ" / "Arrivée" (relations)
)
from ..places import _select_value as _place_select_value  # sanitise un select

# Mémoire incrémentale (SQLite)
from ..storage.db import init_db, get_seen_routes, mark_route_seen


# -------------------------
# Config
# -------------------------

NOTION_DB_GPX = os.environ["NOTION_DB_GPX"]            # id de la DB "🗺️ Projets GPX"
NOTION_TOKEN  = os.environ["NOTION_API_KEY"]

# Géocodage (Nominatim)
GEO_ENABLE = (os.getenv("GEO_ENABLE", "1").strip().lower() not in {"0", "false", "no"})
NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "stravation/1.0 (https://github.com/hugolag64/Stravation)")
NOMINATIM_EMAIL = os.getenv("NOMINATIM_EMAIL")

# Détection de massifs via Overpass (OSM)
OVERPASS_ENABLE = (os.getenv("OVERPASS_ENABLE", "1").strip().lower() not in {"0", "false", "no"})
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OVERPASS_TIMEOUT = int(os.getenv("OVERPASS_TIMEOUT", "25"))  # secondes
MAX_ZONES = int(os.getenv("ROUTE_ZONES_MAX", "5"))  # limite de tags pour rester lisible

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
                if "updated_at" in route and route["updated_at"]:
                    try:
                        route["updated_at"] = p.parse(route["updated_at"]).to_iso8601_string()
                    except Exception:
                        pass
                # ➜ normalisation de created_at (AJOUT)
                if "created_at" in route and route["created_at"]:
                    try:
                        route["created_at"] = p.parse(route["created_at"]).to_iso8601_string()
                    except Exception:
                        pass
                yield route
            page += 1
    finally:
        client.close()


def _route_type_to_key(rt: dict) -> str:
    """
    Convertit type/sub_type Strava Route vers une clé SPORT_MAP.
    - type: 1=Ride, 2=Run
    - sub_type pour Ride -> 1=Road, 2=MTB, 3=Gravel/CX
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
    return "Run"


def _export_route_gpx(route_id: int, token: str) -> Optional[str]:
    """
    Télécharge le GPX de la route via l’API (auth requise). Renvoie le texte GPX.
    """
    url = f"https://www.strava.com/api/v3/routes/{route_id}/export_gpx"
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get(url, headers=headers, timeout=60)
    if r.status_code == 200 and r.text.strip():
        return r.text
    return None


def _first_last_latlng_from_gpx(gpx_text: str) -> Tuple[Optional[Tuple[float,float]], Optional[Tuple[float,float]]]:
    """
    Extrait (lat,lon) du premier et du dernier point GPX (rtept | trkpt).
    """
    try:
        root = ET.fromstring(gpx_text)
    except Exception:
        return None, None

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

    return _latlon(pts[0]), _latlon(pts[-1])


def _gpx_points(gpx_text: str, step: int = 50) -> List[Tuple[float, float]]:
    """
    Extrait une liste échantillonnée de points (lat, lon) depuis le GPX.
    step: on garde 1 point sur 'step' pour limiter la taille.
    """
    try:
        root = ET.fromstring(gpx_text)
    except Exception:
        return []
    pts = list(root.findall(".//{*}rtept"))
    if not pts:
        pts = list(root.findall(".//{*}trkpt"))
    out: List[Tuple[float, float]] = []
    for i, el in enumerate(pts):
        if i % step != 0:
            continue
        try:
            out.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
        except Exception:
            pass
    return out


def _bbox_from_points(points: List[Tuple[float, float]], expand_deg: float = 0.08) -> Optional[Tuple[float, float, float, float]]:
    """
    Calcule la bbox (lat_min, lon_min, lat_max, lon_max) avec un léger buffer (deg).
    """
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    return (lat_min - expand_deg, lon_min - expand_deg, lat_max + expand_deg, lon_max + expand_deg)


# -------------------------
# Reverse-geocode (Nominatim)
# -------------------------

def _nominatim_client() -> httpx.Client:
    headers = {"User-Agent": NOMINATIM_USER_AGENT}
    if NOMINATIM_EMAIL:
        headers["From"] = NOMINATIM_EMAIL
    return httpx.Client(timeout=20, headers=headers)

def reverse_geocode(lat: float, lon: float) -> Dict[str, Optional[str]]:
    """
    Retourne un dict minimal: country, admin1 (région/état), admin2 (département/county),
    city (ville/commune la plus pertinente).
    """
    if not GEO_ENABLE:
        return {"country": None, "admin1": None, "admin2": None, "city": None}

    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "zoom": 10,
        "addressdetails": 1,
    }
    try:
        with _nominatim_client() as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            addr = data.get("address", {}) if isinstance(data, dict) else {}
    except Exception:
        return {"country": None, "admin1": None, "admin2": None, "city": None}

    # Normalisation des champs fréquents OSM
    country = addr.get("country")
    admin1 = addr.get("state") or addr.get("region")
    admin2 = addr.get("county") or addr.get("province") or addr.get("state_district") or addr.get("department")

    # Ville/commune : on tente city > town > village > municipality > hamlet
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or addr.get("hamlet")

    return {"country": country, "admin1": admin1, "admin2": admin2, "city": city}


# -------------------------
# Overpass (massifs / parcs) & Zones auto
# -------------------------

def _overpass_query(bbox: Tuple[float, float, float, float]) -> List[str]:
    """
    Interroge Overpass pour extraire des noms pertinents autour de la trace:
    - natural=mountain_range / ridge
    - boundary=protected_area / national_park
    - leisure=nature_reserve
    Retourne une liste de noms uniques (max MAX_ZONES).
    """
    if not OVERPASS_ENABLE:
        return []

    lat_min, lon_min, lat_max, lon_max = bbox
    q = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}];
    (
      node["natural"="mountain_range"]({lat_min},{lon_min},{lat_max},{lon_max});
      way ["natural"="mountain_range"]({lat_min},{lon_min},{lat_max},{lon_max});
      rel ["natural"="mountain_range"]({lat_min},{lon_min},{lat_max},{lon_max});
      node["natural"="ridge"]({lat_min},{lon_min},{lat_max},{lon_max});
      way ["natural"="ridge"]({lat_min},{lon_min},{lat_max},{lon_max});
      rel ["natural"="ridge"]({lat_min},{lon_min},{lat_max},{lon_max});
      node["boundary"="protected_area"]({lat_min},{lon_min},{lat_max},{lon_max});
      way ["boundary"="protected_area"]({lat_min},{lon_min},{lat_max},{lon_max});
      rel ["boundary"="protected_area"]({lat_min},{lon_min},{lat_max},{lon_max});
      node["boundary"="national_park"]({lat_min},{lon_min},{lat_max},{lon_max});
      way ["boundary"="national_park"]({lat_min},{lon_min},{lat_max},{lon_max});
      rel ["boundary"="national_park"]({lat_min},{lon_min},{lat_max},{lon_max});
      node["leisure"="nature_reserve"]({lat_min},{lon_min},{lat_max},{lon_max});
      way ["leisure"="nature_reserve"]({lat_min},{lon_min},{lat_max},{lon_max});
      rel ["leisure"="nature_reserve"]({lat_min},{lon_min},{lat_max},{lon_max});
    );
    out tags;
    """
    try:
        r = httpx.post(OVERPASS_URL, data={"data": q}, timeout=OVERPASS_TIMEOUT + 5)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    names: List[str] = []
    for el in (data.get("elements") or []):
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("official_name")
        if not name:
            continue
        bad = {"Parc naturel", "Protected Area", "Nature Reserve", "Massif"}
        if name.strip() in bad:
            continue
        names.append(name.strip())

    seen, uniq = set(), []
    for n in names:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(n)
    uniq.sort(key=lambda s: (len(s), s.lower()))
    return uniq[:MAX_ZONES]


def compute_auto_zones_global(
    start_ll: Optional[Tuple[float, float]],
    end_ll: Optional[Tuple[float, float]],
    gpx_points: List[Tuple[float, float]],
    geo_start: Dict[str, Optional[str]],
    geo_end: Dict[str, Optional[str]],
) -> List[str]:
    """
    Déduit automatiquement des Zones globales:
    - Massifs / ranges / parcs via Overpass dans la bbox de la trace
    - Complété par des niveaux admin utiles (admin2, admin1)
    - Cirques de La Réunion (Mafate/Cilaos/Salazie) via toponymes OSM
    """
    zones: List[str] = []

    # 1) Overpass dans la bbox de la trace
    bbox = _bbox_from_points(gpx_points) if gpx_points else None
    if bbox:
        zones.extend(_overpass_query(bbox))

    # 2) Cirques de La Réunion (détection simple par toponymes)
    def _norm(s: Optional[str]) -> str:
        return (s or "").strip().lower()

    REUNION_MAFATE = {
        "marla", "la nouvelle", "roche plate", "aurère", "ilet à bourse", "ilet à bourses",
        "grand place", "grand-place", "lataniers", "orangers", "grand-place les hauts", "grand place les hauts"
    }
    REUNION_CILAOS = {"cilaos", "bras sec", "bras-sec", "ilet à cordes", "îlet à cordes"}
    REUNION_SALAZIE = {"salazie", "hell-bourg", "hell bourg", "grand ilet", "grand-ilet", "mare à poule d’eau"}

    def _maybe_cirque(geo: dict) -> Optional[str]:
        if _norm(geo.get("admin1")) not in {"la réunion", "la reunion", "réunion", "reunion"}:
            return None
        cand = [
            _norm(geo.get("city")), _norm(geo.get("town")), _norm(geo.get("village")),
            _norm(geo.get("suburb")), _norm(geo.get("neighbourhood")), _norm(geo.get("hamlet")),
            _norm(geo.get("municipality")), _norm(geo.get("admin2")), _norm(geo.get("state_district")),
        ]
        for n in cand:
            if not n:
                continue
            if n in REUNION_MAFATE:  return "Mafate"
            if n in REUNION_CILAOS:  return "Cilaos"
            if n in REUNION_SALAZIE: return "Salazie"
        s = " | ".join(c for c in cand if c)
        if "mafate" in s:  return "Mafate"
        if "cilaos" in s:  return "Cilaos"
        if "salazie" in s: return "Salazie"
        return None

    for g in (geo_start, geo_end):
        c = _maybe_cirque(g)
        if c:
            zones.append(c)

    # 3) Fallback générique: admin2 puis admin1 (utile pour trier par département/région)
    for g in (geo_start, geo_end):
        for key in ("admin2", "admin1"):
            val = g.get(key)
            if val:
                zones.append(val)

    # Nettoyage final (unicité + limite)
    out, seen = [], set()
    for z in zones:
        z = (z or "").strip()
        if not z or len(z) < 3:
            continue
        k = z.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(z)
    return out[:MAX_ZONES]


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

def _title_from_page_properties(props: dict) -> str:
    # récupère le 1er champ de type "title"
    for k, v in props.items():
        if isinstance(v, dict) and v.get("type") == "title":
            blocks = v.get("title") or []
            return "".join([b.get("plain_text", "") for b in blocks]).strip()
    return ""

_ROUTE_ID_RE = re.compile(r"/routes/(\d+)")

def _route_id_from_page_properties(props: dict) -> str | None:
    # 1) propriété explicite “Strava Route ID” si présente (number/rich_text/title)
    pr = props.get("Strava Route ID")
    if isinstance(pr, dict):
        t = pr.get("type")
        if t == "number":
            n = pr.get("number")
            return str(int(n)) if n is not None else None
        if t == "rich_text":
            arr = pr.get("rich_text") or []
            txt = "".join([a.get("plain_text", "") for a in arr]).strip()
            return txt or None
        if t == "title":
            arr = pr.get("title") or []
            txt = "".join([a.get("plain_text", "") for a in arr]).strip()
            return txt or None

    # 2) sinon, tenter d’extraire depuis des URLs (Fichier GPX / Lien Strava / Lien)
    for key in ("Fichier GPX", "Lien Strava", "Lien", "URL"):
        pv = props.get(key)
        if isinstance(pv, dict) and pv.get("type") == "url" and pv.get("url"):
            m = _ROUTE_ID_RE.search(pv["url"])
            if m:
                return m.group(1)

    return None

def list_notion_routes_index(notion: Notion, db_id: str) -> dict[str, dict]:
    """
    Retourne un index: {route_id_str: {"page_id":..., "title":...}}
    pour tout le contenu réel de la DB (pagination gérée).
    """
    index: dict[str, dict] = {}
    start_cursor = None
    while True:
        resp = notion.databases.query(
            **{
                "database_id": db_id,
                "start_cursor": start_cursor,
                "page_size": 100,
            }
        )
        for page in (resp.get("results") or []):
            pid = page.get("id")
            props = page.get("properties") or {}
            title = _title_from_page_properties(props) or "(sans titre)"
            rid = _route_id_from_page_properties(props)
            if rid:
                index[rid] = {"page_id": pid, "title": title}
        if not resp.get("has_more"):
            break
        start_cursor = resp.get("next_cursor")
    return index

# -------------------------
# Incrémental: checksum / décision de traitement
# -------------------------

def _checksum_route(rt: dict) -> str:
    basis = "|".join([
        str(rt.get("id", "")),
        str(rt.get("name", "")),
        str(rt.get("distance", "")),
        str(rt.get("elevation_gain", "")),
        str(rt.get("type", "")),
        str(rt.get("sub_type", "")),
        str(rt.get("updated_at", "")),
        # on pourrait inclure created_at mais updated_at suffit pour le delta
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()

def _should_process(rt: dict, seen: Dict[int, Tuple[Optional[str], Optional[str]]]) -> bool:
    rid = int(rt["id"])
    prev = seen.get(rid)
    if not prev:
        return True
    prev_updated_at, prev_checksum = prev
    updated_at = rt.get("updated_at") or None
    if updated_at and updated_at != prev_updated_at:
        return True
    checksum = _checksum_route(rt)
    if checksum != prev_checksum:
        return True
    return False


# -------------------------
# Mapping vers Notion (avec géographie + zones + date création)
# -------------------------

def _props_for_routes_db(
    rt: dict,
    schema: Dict[str, str],
    start_city: Optional[str],
    end_city: Optional[str],
    geo_start: Dict[str, Optional[str]] | None = None,
    geo_end: Dict[str, Optional[str]] | None = None,
    zones: List[str] | None = None,
) -> dict:
    """
    Construit les propriétés pour la page Notion “Projets GPX”.
    Remplit uniquement ce qui existe dans ta DB (d’après schema).
    """
    props: Dict[str, dict] = {}

    # champs variables selon ta DB
    TITLE = _first_existing(schema, ["Nom", "Name", "Titre"])
    SPORT = _first_existing(schema, ["Type sport", "Type"])
    DIST  = _first_existing(schema, ["Distance (km)", "Distance"])
    GAIN  = _first_existing(schema, ["D+ (m)", "D+"])
    FILEU = _first_existing(schema, ["Fichier GPX", "Lien GPX", "GPX"])
    STAT  = _first_existing(schema, ["Statut", "Status"])
    LINK  = _first_existing(schema, ["Lien Strava", "Lien", "URL"])

    # ➜ Propriété Notion de type Date pour la création de la route
    CREATED_DATE = _first_existing(schema, ["Date création", "Créé le", "Created at", "Created", "Date"])

    # Dimensions géographiques (selects simples)
    PAYS   = _first_existing(schema, ["Pays", "Country"])
    REGION = _first_existing(schema, ["Région", "Region", "Région/État"])
    DEPART = _first_existing(schema, ["Département", "County", "Province"])

    # Multi-select zones/massifs
    ZONES_PROP = _first_existing(schema, ["Zones", "Zone", "Massif"])

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

    # lien GPX (URL)
    if FILEU and schema.get(FILEU) == "url":
        props[FILEU] = {"url": f"https://www.strava.com/routes/{rt['id']}/export_gpx"}

    # lien Strava (URL)
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

    # Ville - départ / arrivée (selects)
    if start_city and "Ville - départ" in schema and schema["Ville - départ"] == "select":
        sv = _place_select_value(start_city)
        if sv:
            props["Ville - départ"] = sv
    if end_city and "Ville - arrivée" in schema and schema["Ville - arrivée"] == "select":
        ev = _place_select_value(end_city)
        if ev:
            props["Ville - arrivée"] = ev

    # Pays / Région / Département (on prend les infos du point de départ en priorité)
    primary_geo = geo_start or {}
    if PAYS and schema.get(PAYS) == "select":
        val = primary_geo.get("country") or (geo_end or {}).get("country")
        if val:
            pv = _place_select_value(val)
            if pv:
                props[PAYS] = pv

    if REGION and schema.get(REGION) == "select":
        val = primary_geo.get("admin1") or (geo_end or {}).get("admin1")
        if val:
            rv = _place_select_value(val)
            if rv:
                props[REGION] = rv

    if DEPART and schema.get(DEPART) == "select":
        val = primary_geo.get("admin2") or (geo_end or {}).get("admin2")
        if val:
            dv = _place_select_value(val)
            if dv:
                props[DEPART] = dv

    # Zones (multi-select) — massifs / parcs / fallback admin
    if zones and ZONES_PROP and schema.get(ZONES_PROP) == "multi_select":
        props[ZONES_PROP] = {"multi_select": [{"name": z} for z in zones]}

    # ➜ Date de création (Strava) → propriété Date Notion
    if CREATED_DATE and schema.get(CREATED_DATE) == "date":
        date_iso = rt.get("created_at") or rt.get("updated_at")
        if date_iso:
            props[CREATED_DATE] = {"date": {"start": date_iso}}

    return props


# -------------------------
# Sync principal (INCREMENTAL + GEO + ZONES)
# -------------------------

def sync_strava_routes_to_notion(*, force: bool = False) -> Tuple[int, int]:
    """
    Importe/Met à jour les itinéraires Strava dans la DB “🗺️ Projets GPX”.
    - upsert via "Strava Route ID" si elle existe
    - relations Départ/Arrivée vers Lieux si possible (via GPX)
    - renseigne Ville - départ/arrivée + Pays / Région / Département (selects)
    - ajoute des 'Zones' (multi-select) : massifs/parcs via Overpass, puis fallback admin
    - ajoute la Date de création (created_at) si la propriété Date existe
    - **incrémental** : ne traite que les nouvelles/modifiées, sauf force=True
    Retourne (créés_ou_mis_a_jour, sautés_sans_changement).
    """
    init_db()

    notion = Notion(auth=NOTION_TOKEN)
    schema = _db_schema(notion, NOTION_DB_GPX)

    # relations (si présentes dans ta DB GPX)
    has_start_rel = (PROP_ACT_START_REL in schema and schema[PROP_ACT_START_REL] == "relation")
    has_end_rel   = (PROP_ACT_END_REL   in schema and schema[PROP_ACT_END_REL]   == "relation")

    route_id_prop_type = schema.get("Strava Route ID", "rich_text")
    created_or_updated, skipped = 0, 0

    token = _get_strava_access_token()
    seen = get_seen_routes()   # {route_id: (updated_at, checksum)}

    for rt in _iter_strava_routes():
        rid = int(rt["id"])
        updated_at = rt.get("updated_at") or None
        checksum = _checksum_route(rt)

        # Décision incrémentale
        if not force and not _should_process(rt, seen):
            skipped += 1
            continue

        # GPX → coords départ/arrivée → lieux Notion + reverse-geocode + zones
        start_city = end_city = None
        start_rel_id = end_rel_id = None
        start_ll: Optional[Tuple[float, float]] = None
        end_ll: Optional[Tuple[float, float]] = None
        geo_start: Dict[str, Optional[str]] = {"country": None, "admin1": None, "admin2": None, "city": None}
        geo_end:   Dict[str, Optional[str]] = {"country": None, "admin1": None, "admin2": None, "city": None}
        zones: List[str] = []
        gpx_points: List[Tuple[float, float]] = []

        try:
            gpx = _export_route_gpx(rid, token)
            if gpx:
                start_ll, end_ll = _first_last_latlng_from_gpx(gpx)
                gpx_points = _gpx_points(gpx)

                if start_ll:
                    # Lieu Notion (DB "Lieux") + nom de commune
                    start_rel_id, start_city = ensure_place_for_coord(list(start_ll))
                    # Reverse-geocode pour Pays / Région / Département / Ville
                    gs = reverse_geocode(start_ll[0], start_ll[1])
                    geo_start.update(gs)
                    if not start_city and gs.get("city"):
                        start_city = gs["city"]
                if end_ll:
                    end_rel_id, end_city = ensure_place_for_coord(list(end_ll))
                    ge = reverse_geocode(end_ll[0], end_ll[1])
                    geo_end.update(ge)
                    if not end_city and ge.get("city"):
                        end_city = ge["city"]

                # Zones globales (massifs/parcs via Overpass + fallbacks)
                zones = compute_auto_zones_global(start_ll, end_ll, gpx_points, geo_start, geo_end)

        except Exception:
            # tolérant aux erreurs côté GPX/OSM
            pass

        props = _props_for_routes_db(
            rt, schema, start_city, end_city,
            geo_start=geo_start, geo_end=geo_end,
            zones=zones,
        )

        # relations vers Lieux si dispo
        if has_start_rel and start_rel_id:
            props[PROP_ACT_START_REL] = {"relation": [{"id": start_rel_id}]}
        if has_end_rel and end_rel_id:
            props[PROP_ACT_END_REL] = {"relation": [{"id": end_rel_id}]}

        # upsert par "Strava Route ID" si présent
        page_id = None
        if "Strava Route ID" in schema:
            page_id = _find_page_id_by_route_id(notion, NOTION_DB_GPX, route_id_prop_type, str(rid))

        try:
            if page_id:
                notion.pages.update(page_id=page_id, properties=props)
            else:
                notion.pages.create(parent={"database_id": NOTION_DB_GPX}, properties=props)

            created_or_updated += 1
            # marque cette route comme vue/à jour
            mark_route_seen(rid, updated_at=updated_at, checksum=checksum)

        except Exception as e:
            print(f"[routes] Notion upsert failed for {rid}: {e}")

        time.sleep(float(os.getenv("RATE_SAFETY", "0.15")))  # ménage les APIs

    return created_or_updated, skipped


# -------------------------
# Adaptateur rétro-compatible pour l'UI
# -------------------------

def sync_routes(*, new_only: bool = True, limit: Optional[int] = None) -> Tuple[int, int]:
    """
    Adapter conservant l'ancienne signature (new_only/limit) utilisée par l'UI.
    - new_only=True (défaut) => incrémental (nouvelles/modifiées uniquement)
    - limit est ignoré (la logique est déjà incrémentale côté Notion/Strava)
    Renvoie le même tuple (created_or_updated, skipped).
    """
    # new_only=True => force=False ; new_only=False => force=True
    return sync_strava_routes_to_notion(force=not new_only)
