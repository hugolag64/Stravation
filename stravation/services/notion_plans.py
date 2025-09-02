from __future__ import annotations
import os
from typing import Dict, Any, Optional, List
import pendulum as p
from notion_client import Client as Notion

NOTION_TOKEN = os.getenv("NOTION_API_KEY", "")
DB_PLANS     = os.getenv("NOTION_DB_PLANS", "")

ENDURANCE = {"Course à pied", "Trail", "Vélo"}
WOD_ONLY  = {"CrossFit", "Hyrox"}

def _month_name_fr(dt: p.DateTime) -> str:
    return dt.format("MMMM", locale="fr").capitalize()

def _iso_week(dt: p.DateTime) -> str:
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"

def get_client() -> Notion:
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_API_KEY manquant")
    return Notion(auth=NOTION_TOKEN)

def build_plan_properties(
    *,
    title: str,
    date_local_iso: str,
    sport: str,
    types: Optional[list[str]] = None,
    distance_km: Optional[float] = None,
    dplus_m: Optional[int] = None,
    duree_min: Optional[int] = None,
    notes: str = "",
    statut: str = "Pas commencé",
) -> Dict[str, Any]:
    dt = p.parse(date_local_iso)
    mois = _month_name_fr(dt)
    semaine = _iso_week(dt)

    props: Dict[str, Any] = {
        "Nom": {"title": [{"type": "text", "text": {"content": title}}]},
        "Date prévue": {"date": {"start": dt.to_datetime_string()}},
        "Sport": {"select": {"name": sport}},
        "Type de séance": {"multi_select": [{"name": t} for t in (types or [])]},
        "Semaine ISO": {"rich_text": [{"type": "text", "text": {"content": semaine}}]},
        "Statut": {"status": {"name": statut}},
        "Notes": {"rich_text": [{"type": "text", "text": {"content": notes}}]},
        "Mois": {"select": {"name": mois}},
    }
    if sport in ENDURANCE:
        if distance_km is not None:
            props["Distance prévue (km)"] = {"number": float(distance_km)}
        if dplus_m is not None:
            props["D+ prévu (m)"] = {"number": int(dplus_m)}
    if duree_min is not None:
        props["Durée prévue (min)"] = {"number": int(duree_min)}
    return props

# ---------- Create / Update ----------
def create_plan(**kwargs) -> str:
    if not DB_PLANS:
        raise RuntimeError("NOTION_DB_PLANS manquant")
    client = get_client()
    props = build_plan_properties(**kwargs)
    page = client.pages.create(parent={"database_id": DB_PLANS}, properties=props)
    return page["id"]

def update_plan(page_id: str, **kwargs) -> None:
    client = get_client()
    props = build_plan_properties(**kwargs)
    client.pages.update(page_id=page_id, properties=props)

# ---------- Query helpers ----------
def find_plans_on_day(dt_local: p.DateTime) -> List[Dict[str, Any]]:
    """
    Retourne les pages de la DB Plans dont 'Date prévue' intersecte
    le jour local donné.
    """
    if not DB_PLANS:
        raise RuntimeError("NOTION_DB_PLANS manquant")
    client = get_client()
    start = dt_local.start_of("day").to_datetime_string()
    end   = dt_local.end_of("day").to_datetime_string()
    q = {
        "and": [
            {"property": "Date prévue", "date": {"on_or_after": start}},
            {"property": "Date prévue", "date": {"on_or_before": end}},
        ]
    }
    res = client.databases.query(database_id=DB_PLANS, filter=q)
    return res.get("results", [])

def page_to_form_defaults(page: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extrait {title, date, sport, types, distance_km, dplus_m, duree_min, notes}
    depuis une page Notion Plans.
    """
    props = page.get("properties", {})
    def _get_text(prop):
        arr = props.get(prop, {}).get("title" if prop=="Nom" else "rich_text", [])
        return "".join([x["plain_text"] for x in arr]) if arr else ""
    def _get_select(prop):
        s = props.get(prop, {}).get("select")
        return s["name"] if s else None
    def _get_multiselect(prop):
        arr = props.get(prop, {}).get("multi_select", [])
        return [x["name"] for x in arr]
    def _get_num(prop):
        v = props.get(prop, {}).get("number")
        return v if v is not None else None
    def _get_date(prop):
        d = props.get(prop, {}).get("date", {})
        return (d.get("start") or "")[:16]  # "YYYY-MM-DD HH:mm"

    return {
        "page_id": page["id"],
        "title": _get_text("Nom"),
        "date_local_iso": _get_date("Date prévue"),
        "sport": _get_select("Sport") or "Course à pied",
        "types": _get_multiselect("Type de séance"),
        "distance_km": _get_num("Distance prévue (km)"),
        "dplus_m": _get_num("D+ prévu (m)"),
        "duree_min": _get_num("Durée prévue (min)"),
        "notes": _get_text("Notes"),
    }

def find_plans_in_range(start_local: p.DateTime, end_local: p.DateTime):
    """Toutes les pages Plans dont 'Date prévue' intersecte [start,end]."""
    client = get_client()
    q = {
        "and": [
            {"property": "Date prévue", "date": {"on_or_after": start_local.to_datetime_string()}},
            {"property": "Date prévue", "date": {"on_or_before":  end_local.to_datetime_string()}},
        ]
    }
    res = client.databases.query(database_id=DB_PLANS, filter=q)
    return res.get("results", [])

def page_date_local_iso(page: Dict) -> Optional[str]:
    d = page.get("properties", {}).get("Date prévue", {}).get("date", {})
    return (d.get("start") or "")[:16] if d else None  # "YYYY-MM-DD HH:mm"
