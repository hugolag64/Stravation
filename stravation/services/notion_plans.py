# stravation/services/notion_plans.py
from __future__ import annotations

import os
from typing import Dict, Any, Optional, List
import pendulum as p
from notion_client import Client as Notion

from stravation.utils.envtools import load_dotenv_if_exists

# Chargement .env (UTF-8, tolérant) avant toute lecture d'env
load_dotenv_if_exists()

# ─────────────────────────────────────────────────────────────────────────────
# Config & constantes
# ─────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN = os.getenv("NOTION_API_KEY", "").strip()

# Supporte les deux noms d'ENV : priorité à NOTION_DB_PLANNING
DB_PLANS = (os.getenv("NOTION_DB_PLANNING", "").strip()
            or os.getenv("NOTION_DB_PLANS", "").strip())

SPORT_TZ = os.getenv("SPORT_TZ", "Indian/Reunion").strip() or "Indian/Reunion"

ENDURANCE = {"Course à pied", "Trail", "Vélo"}
WOD_ONLY  = {"CrossFit", "Hyrox"}  # (utilisé pour tes affichages/validations éventuelles)

# Noms de propriétés Notion (adapte si ta DB diffère)
PROP_NAME        = "Nom"
PROP_DATE        = "Date prévue"
PROP_SPORT       = "Sport"
PROP_TYPES       = "Type de séance"
PROP_SEMAINE_ISO = "Semaine ISO"
PROP_STATUT      = "Statut"
PROP_NOTES       = "Notes"
PROP_MOIS        = "Mois"
PROP_DIST_KM     = "Distance prévue (km)"
PROP_DPLUS_M     = "D+ prévu (m)"
PROP_DUREE_MIN   = "Durée prévue (min)"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _require_env():
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_API_KEY manquant. Ajoute-le dans ton .env.")
    if not DB_PLANS:
        raise RuntimeError(
            "ID de base Notion pour le planning manquant. "
            "Renseigne NOTION_DB_PLANNING (id recommandé) ou NOTION_DB_PLANS dans ton .env."
        )


def _month_name_fr(dt: p.DateTime) -> str:
    return dt.format("MMMM", locale="fr").capitalize()


def _iso_week(dt: p.DateTime) -> str:
    # p.DateTime.isocalendar() -> (year, week, weekday)
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _parse_local(dt_like: str | p.DateTime) -> p.DateTime:
    """
    Parse une date locale utilisateur.
    - Accepte 'YYYY-MM-DD HH:mm' ou un ISO avec offset.
    - Si naïf, applique SPORT_TZ.
    Retourne un pendulum DateTime aware (avec fuseau).
    """
    if isinstance(dt_like, p.DateTime):
        dt = dt_like
    else:
        dt = p.parse(str(dt_like))
    if dt.tzinfo is None:
        dt = dt.replace(tz=SPORT_TZ)
    return dt


def get_client() -> Notion:
    _require_env()
    return Notion(auth=NOTION_TOKEN)


# ─────────────────────────────────────────────────────────────────────────────
# Build properties
# ─────────────────────────────────────────────────────────────────────────────
def build_plan_properties(
    *,
    title: str,
    date_local_iso: str | p.DateTime,
    sport: str,
    types: Optional[List[str]] = None,
    distance_km: Optional[float] = None,
    dplus_m: Optional[int] = None,
    duree_min: Optional[int] = None,
    notes: str = "",
    statut: str = "Pas commencé",
) -> Dict[str, Any]:
    """
    Construit le dict `properties` pour Notion à partir d'un jeu de champs.
    `date_local_iso` est interprété comme local (SPORT_TZ) s'il est naïf.
    """
    dt = _parse_local(date_local_iso)
    mois = _month_name_fr(dt)
    semaine = _iso_week(dt)

    props: Dict[str, Any] = {
        PROP_NAME: {"title": [{"type": "text", "text": {"content": title}}]},
        PROP_DATE: {"date": {"start": dt.to_iso8601_string()}},  # ISO-8601 avec offset
        PROP_SPORT: {"select": {"name": sport}},
        PROP_TYPES: {"multi_select": [{"name": t} for t in (types or [])]},
        PROP_SEMAINE_ISO: {"rich_text": [{"type": "text", "text": {"content": semaine}}]},
        PROP_STATUT: {"status": {"name": statut}},
        PROP_NOTES: {"rich_text": [{"type": "text", "text": {"content": notes}}]},
        PROP_MOIS: {"select": {"name": mois}},
    }

    # Champs numériques contextuels
    if sport in ENDURANCE:
        if distance_km is not None:
            props[PROP_DIST_KM] = {"number": float(distance_km)}
        if dplus_m is not None:
            props[PROP_DPLUS_M] = {"number": int(dplus_m)}
    if duree_min is not None:
        props[PROP_DUREE_MIN] = {"number": int(duree_min)}

    return props


# ─────────────────────────────────────────────────────────────────────────────
# Create / Update
# ─────────────────────────────────────────────────────────────────────────────
def create_plan(**kwargs) -> str:
    """
    Crée une page dans la DB planning. Retourne l'ID de page.
    Attendus dans kwargs: title, date_local_iso, sport, (types, distance_km, dplus_m, duree_min, notes, statut)
    """
    _require_env()
    client = get_client()
    props = build_plan_properties(**kwargs)
    page = client.pages.create(parent={"database_id": DB_PLANS}, properties=props)
    return page["id"]


def update_plan(page_id: str, **kwargs) -> None:
    """
    Met à jour une page planning existante.
    """
    _require_env()
    client = get_client()
    props = build_plan_properties(**kwargs)
    client.pages.update(page_id=page_id, properties=props)


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────────────────────
def _date_range_filter(start_local: p.DateTime, end_local: p.DateTime) -> Dict[str, Any]:
    """
    Construit un filtre Notion pour intersection sur PROP_DATE dans [start, end].
    Utilise on_or_after / on_or_before en ISO-8601.
    """
    s = _parse_local(start_local).to_iso8601_string()
    e = _parse_local(end_local).to_iso8601_string()
    return {
        "and": [
            {"property": PROP_DATE, "date": {"on_or_after": s}},
            {"property": PROP_DATE, "date": {"on_or_before": e}},
        ]
    }


def find_plans_on_day(dt_local: p.DateTime) -> List[Dict[str, Any]]:
    """
    Retourne les pages de la DB Plans dont 'Date prévue' intersecte
    le jour local donné (00:00→23:59:59).
    """
    _require_env()
    client = get_client()
    day_start = _parse_local(dt_local.start_of("day"))
    day_end   = _parse_local(dt_local.end_of("day"))
    q = _date_range_filter(day_start, day_end)
    res = client.databases.query(database_id=DB_PLANS, filter=q)
    return res.get("results", [])


def find_plans_in_range(start_local: p.DateTime, end_local: p.DateTime) -> List[Dict[str, Any]]:
    """
    Toutes les pages Plans dont 'Date prévue' intersecte [start, end].
    """
    _require_env()
    if end_local <= start_local:
        raise ValueError("find_plans_in_range: end_local doit être > start_local")
    client = get_client()
    q = _date_range_filter(start_local, end_local)
    res = client.databases.query(database_id=DB_PLANS, filter=q)
    return res.get("results", [])


# ─────────────────────────────────────────────────────────────────────────────
# Mapping page -> valeurs de formulaire (pour UI)
# ─────────────────────────────────────────────────────────────────────────────
def page_to_form_defaults(page: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extrait {title, date_local_iso, sport, types, distance_km, dplus_m, duree_min, notes}
    depuis une page Notion Plans.
    """
    props = page.get("properties", {})

    def _get_text(prop: str) -> str:
        if prop == PROP_NAME:
            arr = props.get(prop, {}).get("title", [])
        else:
            arr = props.get(prop, {}).get("rich_text", [])
        return "".join(x.get("plain_text", "") for x in arr) if arr else ""

    def _get_select(prop: str) -> Optional[str]:
        s = props.get(prop, {}).get("select")
        return s.get("name") if s else None

    def _get_multiselect(prop: str) -> List[str]:
        arr = props.get(prop, {}).get("multi_select", [])
        return [x.get("name", "") for x in arr] if arr else []

    def _get_num(prop: str) -> Optional[float]:
        v = props.get(prop, {}).get("number")
        return v if v is not None else None

    def _get_date_local(prop: str) -> str:
        d = props.get(prop, {}).get("date", {}) or {}
        start = d.get("start") or ""
        if not start:
            return ""
        # Normalise en local SPORT_TZ et format "YYYY-MM-DD HH:mm"
        dt = p.parse(start)
        if dt.tzinfo is None:
            dt = dt.replace(tz=SPORT_TZ)
        return dt.in_timezone(SPORT_TZ).format("YYYY-MM-DD HH:mm")

    return {
        "page_id": page.get("id", ""),
        "title": _get_text(PROP_NAME),
        "date_local_iso": _get_date_local(PROP_DATE),
        "sport": _get_select(PROP_SPORT) or "Course à pied",
        "types": _get_multiselect(PROP_TYPES),
        "distance_km": _get_num(PROP_DIST_KM),
        "dplus_m": _get_num(PROP_DPLUS_M),
        "duree_min": _get_num(PROP_DUREE_MIN),
        "notes": _get_text(PROP_NOTES),
    }


def page_date_local_iso(page: Dict[str, Any]) -> Optional[str]:
    """
    Renvoie la date locale "YYYY-MM-DD HH:mm" pour la propriété date principale,
    ou None si absente.
    """
    props = page.get("properties", {})
    d = props.get(PROP_DATE, {}).get("date", {}) or {}
    start = d.get("start")
    if not start:
        return None
    dt = p.parse(start)
    if dt.tzinfo is None:
        dt = dt.replace(tz=SPORT_TZ)
    return dt.in_timezone(SPORT_TZ).format("YYYY-MM-DD HH:mm")
