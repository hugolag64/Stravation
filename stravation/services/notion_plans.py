# stravation/services/notion_plans.py
from __future__ import annotations

import os
from typing import List, Optional, Dict, Any
import pendulum as p
from notion_client import Client
from stravation.utils.envtools import load_dotenv_if_exists
from stravation.models.plan_session import PlanSession

# ─────────────────────────────────────────────────────────────────────────────
# Config & propriétés Notion
# ─────────────────────────────────────────────────────────────────────────────
PROP_TITLE    = "Nom de la séance"      # Title
PROP_DATE     = "Date prévue"           # Date
PROP_SPORT    = "Sport"                 # Select
PROP_TYPES    = "Type de séance"        # Multi-select
PROP_DURATION = "Durée prévue (min)"    # Number
PROP_MONTH    = "Mois"                  # Select

load_dotenv_if_exists()
NOTION_TOKEN = os.getenv("NOTION_API_KEY")
DB_PLAN = os.getenv("NOTION_DB_PLANNING") or os.getenv("NOTION_DB_PLANS")

if not NOTION_TOKEN:
    raise RuntimeError("NOTION_API_KEY manquant. Vérifie ton .env.")
if not DB_PLAN:
    raise RuntimeError("NOTION_DB_PLANNING ou NOTION_DB_PLANS manquant. Pointe la DB Plan.")

client = Client(auth=NOTION_TOKEN)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers lecture
# ─────────────────────────────────────────────────────────────────────────────
def _get_prop(props: Dict[str, Any], key: str) -> Any:
    return props.get(key)

def _text_title(prop: Dict[str, Any]) -> str:
    rich = prop.get("title", [])
    return "".join([t.get("plain_text","") for t in rich]) if rich else ""

def _date_start(prop: Dict[str, Any]) -> Optional[str]:
    if not prop or not prop.get("date"):
        return None
    return prop["date"].get("start")

def _select_name(prop: Dict[str, Any]) -> Optional[str]:
    if not prop or not prop.get("select"):
        return None
    sel = prop["select"]
    return sel.get("name") if sel else None

def _multi_names(prop: Dict[str, Any]) -> List[str]:
    if not prop or not prop.get("multi_select"):
        return []
    return [m.get("name") for m in prop["multi_select"] if m.get("name")]

def _number_val(prop: Dict[str, Any]) -> Optional[int]:
    if not prop:
        return None
    val = prop.get("number")
    return int(val) if isinstance(val, (int, float)) else None

# ─────────────────────────────────────────────────────────────────────────────
# Lecture des séances Plan
# ─────────────────────────────────────────────────────────────────────────────
def fetch_plan_sessions(after_days: int = -14, before_days: int = 120) -> List[PlanSession]:
    """
    Charge les séances entre J-14 et J+120 par défaut.
    """
    now = p.now()
    start = now.add(days=after_days).to_date_string()
    end = now.add(days=before_days).to_date_string()

    res = client.databases.query(
        database_id=DB_PLAN,
        filter={
            "and":[
                {"property": PROP_DATE, "date": {"on_or_after": start}},
                {"property": PROP_DATE, "date": {"on_or_before": end}},
            ]
        },
        page_size=100
    )
    sessions: List[PlanSession] = []
    for page in res.get("results", []):
        props = page["properties"]
        title = _text_title(_get_prop(props, PROP_TITLE))
        date_start = _date_start(_get_prop(props, PROP_DATE))
        if not (title and date_start):
            continue

        # Normaliser: si pas d'heure → 06:30 locale
        dt = p.parse(date_start)
        if "T" not in date_start:
            dt = dt.replace(hour=6, minute=30)

        sess = PlanSession(
            id=page["id"],
            name=title,
            date_iso=dt.to_datetime_string(),
            sport=_select_name(_get_prop(props, PROP_SPORT)),
            types=_multi_names(_get_prop(props, PROP_TYPES)),
            duration_min=_number_val(_get_prop(props, PROP_DURATION)),
        )
        sessions.append(sess)
    return sessions

# ─────────────────────────────────────────────────────────────────────────────
# Écriture / création / mise à jour
# ─────────────────────────────────────────────────────────────────────────────
def ensure_month_and_duration(page_id: str, month_key: str, duration_min: Optional[int]) -> None:
    """
    Met à jour 'Mois' (select) et 'Durée prévue (min)' (number) si besoin.
    """
    update: Dict[str, Any] = {"properties": {}}
    update["properties"][PROP_MONTH] = {"select": {"name": month_key}}
    if duration_min is not None:
        update["properties"][PROP_DURATION] = {"number": int(duration_min)}
    client.pages.update(page_id=page_id, **update)

def _month_key_from_dt(dt: p.DateTime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"

def create_plan(
    *,
    name: str,
    date_iso: str,                  # "YYYY-MM-DDTHH:mm" (locale) ou "YYYY-MM-DD"
    sport: Optional[str] = None,    # Select (ex: "CAP", "Vélo", "CrossFit")
    types: Optional[list[str]] = None,  # Multi-select
    duration_min: Optional[int] = None  # Number
) -> str:
    """
    Crée une page dans la DB Plan avec les propriétés standard + 'Mois' auto.
    Retourne l'ID de la page.
    """
    if not name or not date_iso:
        raise ValueError("create_plan: 'name' et 'date_iso' sont requis.")

    # Normaliser la date: si pas d'heure → 06:30
    dt = p.parse(date_iso)
    if "T" not in date_iso:
        dt = dt.replace(hour=6, minute=30)
    month_key = _month_key_from_dt(dt)

    props: Dict[str, Any] = {
        PROP_TITLE: {"title": [{"type": "text", "text": {"content": name}}]},
        PROP_DATE:  {"date": {"start": dt.to_iso8601_string()}},
        PROP_MONTH: {"select": {"name": month_key}},
    }
    if sport:
        props[PROP_SPORT] = {"select": {"name": sport}}
    if types:
        props[PROP_TYPES] = {"multi_select": [{"name": t} for t in types if t]}
    if duration_min is not None:
        props[PROP_DURATION] = {"number": int(duration_min)}

    page = client.pages.create(
        parent={"database_id": DB_PLAN},
        properties=props,
    )
    return page["id"]

def update_plan(
    *,
    page_id: str,
    name: Optional[str] = None,
    date_iso: Optional[str] = None,
    sport: Optional[str] = None,
    types: Optional[list[str]] = None,
    duration_min: Optional[int] = None
) -> None:
    """
    Met à jour une page Plan existante. Si la date change, met à jour 'Mois' en conséquence.
    Tous les paramètres sont optionnels (upsert partiel).
    """
    if not page_id:
        raise ValueError("update_plan: 'page_id' requis.")

    props: Dict[str, Any] = {}

    if name is not None:
        props[PROP_TITLE] = {"title": [{"type": "text", "text": {"content": name}}]}

    month_key = None
    if date_iso is not None:
        dt = p.parse(date_iso)
        if "T" not in date_iso:
            dt = dt.replace(hour=6, minute=30)
        props[PROP_DATE] = {"date": {"start": dt.to_iso8601_string()}}
        month_key = _month_key_from_dt(dt)

    if sport is not None:
        props[PROP_SPORT] = {"select": ({"name": sport} if sport else None)}

    if types is not None:
        props[PROP_TYPES] = {"multi_select": [{"name": t} for t in types if t]}

    if duration_min is not None:
        props[PROP_DURATION] = {"number": int(duration_min)}

    if month_key:
        props[PROP_MONTH] = {"select": {"name": month_key}}

    if not props:
        return  # rien à faire

    client.pages.update(page_id=page_id, properties=props)

def quick_create_plan(
    name: str,
    date_local: p.DateTime,
    sport: str,
    session_types: Optional[List[str]] = None,
    duration_min: Optional[int] = None,
    notes: Optional[str] = None,
) -> PlanSession:
    """
    Création *simplifiée* d'une ligne dans la DB Plans avec les propriétés
    (Nom, Date prévue, Sport, Type de séance, Durée prévue (min), Notes, Mois).

    Retourne l'objet PlanSession (modèle Pydantic que tu exposes déjà).
    """
    if session_types is None:
        session_types = []

    # Normalisation / propriétés dérivées
    props: Dict = {
        "Nom": name.strip(),
        "Date prévue": date_local,  # pendulum DateTime
        "Sport": sport,
        "Type de séance": session_types,
        "Durée prévue (min)": int(duration_min) if duration_min else None,
        "Notes": notes or "",
    }

    # Laisse la fonction utilitaire remplir Mois / Semaine ISO si tu l’utilises
    props = ensure_month_and_duration(props)

    # Création Notion
    created = create_plan(props)
    return created