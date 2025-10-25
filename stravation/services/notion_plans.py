# stravation/services/notion_plans.py
from __future__ import annotations

import os
import re
from typing import Dict, Any, Optional, List, Iterable
import pendulum as p
from pydantic import BaseModel, ConfigDict
from notion_client import Client
from notion_client.errors import APIResponseError

from stravation.utils.envtools import load_dotenv_if_exists
load_dotenv_if_exists()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN = os.getenv("NOTION_API_KEY")
DB_PLAN = os.getenv("NOTION_DB_PLANNING") or os.getenv("NOTION_DB_PLANS")
SPORT_TZ = os.getenv("SPORT_TZ", "Indian/Reunion")

if not NOTION_TOKEN:
    raise RuntimeError("NOTION_API_KEY manquant dans l'environnement.")
if not DB_PLAN:
    raise RuntimeError("NOTION_DB_PLANNING (ou NOTION_DB_PLANS) manquant dans l'environnement.")

client = Client(auth=NOTION_TOKEN)

def _now_tz() -> p.DateTime:
    return p.now(tz=SPORT_TZ)

# ─────────────────────────────────────────────────────────────────────────────
# Modèle compatible avec l'UI
# ─────────────────────────────────────────────────────────────────────────────
class PlanSession(BaseModel):
    # Compat Pydantic v2 avec pendulum.DateTime
    model_config = ConfigDict(arbitrary_types_allowed=True)

    page_id: str
    title: str
    date: Optional[p.DateTime] = None
    sport: Optional[str] = None
    types: List[str] = []
    duration_min: Optional[int] = None
    month: Optional[str] = None
    notes: Optional[str] = None
    distance_km: Optional[float] = None  # ✅

# ─────────────────────────────────────────────────────────────────────────────
# Alias de propriétés (tolérant aux intitulés variés)
# ─────────────────────────────────────────────────────────────────────────────
PROP_ALIASES: Dict[str, List[str]] = {
    "title":        ["Nom de la séance", "Name", "Titre", "Title", "Nom"],
    "date":         ["Date prévue", "Date", "Jour"],
    "sport":        ["Sport", "Discipline"],
    "types":        ["Type de séance", "Types", "Catégories", "Type"],
    "duration":     ["Durée prévue (min)", "Durée (min)", "Durée"],
    "month":        ["Mois", "Month"],
    "notes":        ["Notes", "Commentaire", "Description"],
    "distance_km":  ["Distance prévue (km)", "Distance (km)", "Distance prévue", "Distance"],
    "iso_week":     ["Semaine ISO", "ISO Week", "Semaine"],  # ✅ texte/rich_text/select
}

# type attendu pour un fallback intelligent (utilisé en dernier recours)
_EXPECTED_TYPE = {
    "title": "title",
    "date": "date",
    "sport": "select",
    "types": "multi_select",
    "duration": "number",
    "month": "select",
    "notes": "rich_text",
    "distance_km": "number",
    # "iso_week": pas de fallback par type (peut être rich_text OU select)
}

_DB_PROP_CACHE: Optional[Dict[str, Dict[str, Any]]] = None

def _slug(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())

def _load_db_properties() -> Dict[str, Dict[str, Any]]:
    """Récupère et *cache* le schéma des propriétés de la DB Notion."""
    global _DB_PROP_CACHE
    if _DB_PROP_CACHE is not None:
        return _DB_PROP_CACHE
    db = client.databases.retrieve(database_id=DB_PLAN)
    props: Dict[str, Dict[str, Any]] = db.get("properties", {})
    _DB_PROP_CACHE = props
    return props

def _resolve_property_name(kind: str) -> Optional[str]:
    props = _load_db_properties()

    # 1) alias exact
    for alias in PROP_ALIASES.get(kind, []):
        if alias in props:
            return alias

    # 2) fuzzy via slug
    alias_slugs = [_slug(a) for a in PROP_ALIASES.get(kind, [])]
    for real_name in props.keys():
        if _slug(real_name) in alias_slugs:
            return real_name

    # 3) fallback par type si vraiment rien trouvé (sauf iso_week)
    if kind != "iso_week":
        expected = _EXPECTED_TYPE.get(kind)
        if expected:
            candidates = [n for n, meta in props.items() if meta.get("type") == expected]
            if len(candidates) == 1:
                return candidates[0]
            if kind == "title" and candidates:
                return candidates[0]

    return None

def _text_from_rich(rich: Any) -> str:
    if not rich:
        return ""
    try:
        return "".join([b.get("plain_text") or b.get("text", {}).get("content", "") for b in rich])
    except Exception:
        return ""

# ─────────────────────────────────────────────────────────────────────────────
# Semaine ISO helpers
# ─────────────────────────────────────────────────────────────────────────────
def _iso_week_label(dt: p.DateTime) -> str:
    """Retourne 'YYYY-Www' basé sur *l'année ISO* et la *semaine ISO*."""
    iso = dt.isocalendar()  # (iso_year, iso_week, iso_weekday)
    return f"{iso[0]}-W{iso[1]:02d}"

def _ensure_select_option(prop_name: str, option_name: str) -> None:
    """
    Ajoute l'option 'option_name' dans la propriété 'prop_name' (type select)
    si elle n'existe pas encore.
    """
    # rafraîchir schéma courant
    db = client.databases.retrieve(database_id=DB_PLAN)
    prop = db.get("properties", {}).get(prop_name)
    if not prop or prop.get("type") != "select":
        return
    existing = {opt.get("name") for opt in prop.get("select", {}).get("options", [])}
    if option_name in existing:
        return
    client.databases.update(
        database_id=DB_PLAN,
        properties={
            prop_name: {
                "select": {
                    "options": [{"name": option_name, "color": "default"}]  # couleur par défaut
                }
            }
        },
    )
    # invalider le cache pour tenir compte de la nouvelle option
    global _DB_PROP_CACHE
    _DB_PROP_CACHE = None

# ─────────────────────────────────────────────────────────────────────────────
# Parsing avancé de la durée depuis "Nom"
# ─────────────────────────────────────────────────────────────────────────────
_NUM = r"(?:\d+(?:[.,]\d+)?)"
APOST = "[’']"  # apostrophe ou quote

def _minutes_from_h(match: re.Match) -> str:
    h = float(match.group(1).replace(",", "."))
    m = float((match.group(2) or "0").replace(",", "."))
    return str(int(round(h * 60 + m)))

def _minutes_from_seconds(match: re.Match) -> str:
    s = float(match.group(1).replace(",", "."))
    return str(int(round(s / 60.0)))

def _normalize_duration_expression(raw: str) -> str:
    """
    Convertit une description libre en une *expression arithmétique* (en minutes)
    ne contenant plus que chiffres, +, *, (, ).
    Exemple:
      "Footing 20' + 6x(3' + 1')" → "20 + 6*(3 + 1)"
      "8×400\"" → "8*7" (≈ 400" ≈ 6.7 min → 7 min arrondi)
    """
    s = (raw or "").lower()

    # RAC +10' s'il n'y a PAS de durée explicite après "rac"
    if "rac" in s and not re.search(r"rac\s*\d", s):
        s += " + 10'"

    # Unifier opérateurs / apostrophes
    s = s.replace("×", "*").replace("x", "*")

    # Heures: "1h30" / "1h" / "1 h 30"
    s = re.sub(fr"\b({_NUM})\s*h\s*({_NUM})?\b", _minutes_from_h, s)

    # minutes "45’" / "45'" / "45 min"
    s = re.sub(fr"\b({_NUM})\s*min\b", lambda m: str(int(float(m.group(1).replace(',', '.')))), s)
    s = re.sub(fr"\b({_NUM})\s*{APOST}\b", lambda m: str(int(float(m.group(1).replace(',', '.')))), s)

    # secondes: 400" / 30 s
    s = re.sub(fr"\b({_NUM})\s*sec\b", _minutes_from_seconds, s)
    s = re.sub(fr"\b({_NUM})\s*s\b", _minutes_from_seconds, s)
    s = re.sub(fr"\b({_NUM})\s*\"\b", _minutes_from_seconds, s)

    # Retirer tout sauf chiffres, +, *, (, ), espaces
    s = re.sub(r"[^0-9+*() \t]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Empêcher "3 4" → "3+4"
    s = re.sub(r"(?<=\d)\s+(?=\d)", "+", s)

    # Multiplication implicite
    s = re.sub(r"(?<=\d)\s*(?=\()", "*", s)   # digit (
    s = re.sub(r"(?<=\))\s*(?=\d)", "*", s)   # ) digit
    s = re.sub(r"\)\s*\(", ")*(", s)          # )(

    return s

def _safe_eval_minutes(expr: str) -> Optional[int]:
    """Évalue une expression arithmétique simple. Retourne des minutes (int) ou None."""
    if not expr:
        return None
    if not re.fullmatch(r"[0-9+*() \t]+", expr):
        return None
    try:
        if "()" in expr or "**" in expr or "++" in expr or "+*" in expr or "*+" in expr:
            return None
        value = eval(expr, {"__builtins__": {}}, {})
        if value is None:
            return None
        return int(round(float(value)))
    except Exception:
        return None

def parse_duration_from_name(name: str, sport_hint: Optional[str] = None) -> Optional[int]:
    """
    Règles:
      - parsing des fractionnés (+, *, parenthèses)
      - 'RAC' => +10' si non précisée
      - Crossfit / Hyrox => 60' par défaut si aucune durée détectée
    """
    expr = _normalize_duration_expression(name)
    minutes = _safe_eval_minutes(expr)

    if minutes is None or minutes == 0:
        low = (name or "").lower()
        if ("crossfit" in low or "hyrox" in low) or (sport_hint and sport_hint.lower() in {"crossfit", "hyrox"}):
            return 60
        return minutes
    return minutes

# ─────────────────────────────────────────────────────────────────────────────
# Construction de payloads Notion
# ─────────────────────────────────────────────────────────────────────────────
def _build_properties_payload(
    title: str,
    date: p.DateTime,
    sport: Optional[str],
    types: Optional[List[str]],
    duration_min: Optional[int],
    notes: Optional[str],
    distance_km: Optional[float],  # ✅
) -> Dict[str, Any]:
    """
    Construit un payload "properties" prêt pour pages.create/update,
    en ne mappant que les champs réellement présents dans la DB.
    """
    props_schema = _load_db_properties()
    properties: Dict[str, Any] = {}

    # Title
    p_title = _resolve_property_name("title")
    if not p_title:
        raise RuntimeError(
            "Aucune propriété 'title' trouvée dans la base (ex: 'Name' / 'Nom de la séance')."
        )
    properties[p_title] = {"title": [{"text": {"content": title or ""}}]}

    # Date
    p_date = _resolve_property_name("date")
    if date and p_date and props_schema[p_date]["type"] == "date":
        properties[p_date] = {"date": {"start": date.to_datetime_string()}}

    # Sport (select)
    p_sport = _resolve_property_name("sport")
    if sport and p_sport and props_schema[p_sport]["type"] == "select":
        properties[p_sport] = {"select": {"name": sport}}

    # Types (multi-select)
    p_types = _resolve_property_name("types")
    if types and p_types and props_schema[p_types]["type"] == "multi_select":
        properties[p_types] = {"multi_select": [{"name": t} for t in types]}

    # Duration (number)
    p_duration = _resolve_property_name("duration")
    if duration_min is not None and p_duration and props_schema[p_duration]["type"] == "number":
        properties[p_duration] = {"number": int(duration_min)}

    # Distance prévue (number) ✅
    p_dist = _resolve_property_name("distance_km")
    if distance_km is not None and p_dist and props_schema[p_dist]["type"] == "number":
        try:
            properties[p_dist] = {"number": float(distance_km)}
        except Exception:
            pass

    # Mois (select) — **nom du mois FR** (Janvier, Février…)
    p_month = _resolve_property_name("month")
    if date and p_month and props_schema[p_month]["type"] == "select":
        month_label = date.format("MMMM", locale="fr").capitalize()
        properties[p_month] = {"select": {"name": month_label}}

    # Semaine ISO => 'YYYY-Www' (rich_text OU select)
    p_week = _resolve_property_name("iso_week")
    if date and p_week:
        label = _iso_week_label(date)
        w_type = props_schema[p_week]["type"]
        if w_type in ("rich_text", "text"):
            properties[p_week] = {"rich_text": [{"text": {"content": label}}]}
        elif w_type == "select":
            _ensure_select_option(p_week, label)
            properties[p_week] = {"select": {"name": label}}

    # Notes (rich_text)
    p_notes = _resolve_property_name("notes")
    if notes and p_notes and props_schema[p_notes]["type"] in ("rich_text", "text"):
        properties[p_notes] = {"rich_text": [{"text": {"content": notes}}]}

    return properties

def _parse_page_to_plan(page: Dict[str, Any]) -> PlanSession:
    props = page.get("properties", {})

    def get_prop(kind: str) -> Optional[Dict[str, Any]]:
        name = _resolve_property_name(kind)
        if not name:
            return None
        return props.get(name)

    # Title
    p_title = get_prop("title")
    title = ""
    if p_title and p_title.get("title"):
        title = _text_from_rich(p_title["title"])

    # Date
    date_val: Optional[p.DateTime] = None
    p_date = get_prop("date")
    if p_date and p_date.get("date") and p_date["date"].get("start"):
        try:
            date_val = p.parse(p_date["date"]["start"])
        except Exception:
            date_val = None

    # Sport
    sport = None
    p_sport = get_prop("sport")
    if p_sport and p_sport.get("select"):
        sport = p_sport["select"].get("name")

    # Types
    types: List[str] = []
    p_types = get_prop("types")
    if p_types and p_types.get("multi_select"):
        types = [t.get("name") for t in p_types["multi_select"] if t.get("name")]

    # Duration
    duration_min = None
    p_duration = get_prop("duration")
    if p_duration and p_duration.get("number") is not None:
        duration_min = int(p_duration["number"])

    # Distance prévue (km) ✅
    distance_km = None
    p_dist = get_prop("distance_km")
    if p_dist and p_dist.get("number") is not None:
        try:
            distance_km = float(p_dist["number"])
        except Exception:
            distance_km = None

    # Mois
    month = None
    p_month = get_prop("month")
    if p_month and p_month.get("select"):
        month = p_month["select"].get("name")

    # Notes
    notes = None
    p_notes = get_prop("notes")
    if p_notes and p_notes.get("rich_text"):
        notes_text = _text_from_rich(p_notes["rich_text"])
        notes = notes_text or None

    return PlanSession(
        page_id=page.get("id"),
        title=title,
        date=date_val,
        sport=sport,
        types=types,
        duration_min=duration_min,
        month=month,
        notes=notes,
        distance_km=distance_km,
    )

# ─────────────────────────────────────────────────────────────────────────────
# API publique (CRUD)
# ─────────────────────────────────────────────────────────────────────────────
def create_plan(
    *,
    title: str,
    date: p.DateTime,
    sport: Optional[str] = None,
    types: Optional[List[str]] = None,
    duration_min: Optional[int] = None,
    notes: Optional[str] = None,
    distance_km: Optional[float] = None,  # ✅
) -> Dict[str, Any]:
    """Crée une page dans la DB Planning Notion. Tolérant aux alias de propriétés."""
    try:
        props = _build_properties_payload(
            title=title,
            date=date,
            sport=sport,
            types=types,
            duration_min=duration_min,
            notes=notes,
            distance_km=distance_km,
        )
        page = client.pages.create(
            parent={"database_id": DB_PLAN},
            properties=props,
        )
        return page
    except APIResponseError as e:
        raise RuntimeError(
            f"[Notion] Échec création page: {e.message} (code={getattr(e, 'code', 'NA')})"
        ) from e

def update_plan(page_id: str, **fields: Any) -> Dict[str, Any]:
    """
    Met à jour une page existante. On reconstruit un payload puis on garde
    uniquement les clés correspondant aux champs réellement fournis.
    """
    # Normalisation de la date (optionnelle)
    raw_date = fields.get("date")
    date_norm: Optional[p.DateTime]
    if raw_date is None:
        date_norm = None
    elif isinstance(raw_date, p.DateTime):
        date_norm = raw_date
    else:
        try:
            date_norm = p.instance(raw_date)
        except Exception:
            date_norm = None

    # On construit un payload complet puis on filtrera
    payload_full = _build_properties_payload(
        title=fields.get("title", ""),
        date=date_norm or _now_tz(),  # valeur temporaire si pas de date; filtrée si non demandée
        sport=fields.get("sport"),
        types=fields.get("types"),
        duration_min=fields.get("duration_min"),
        notes=fields.get("notes"),
        distance_km=fields.get("distance_km"),  # ✅
    )

    # Ne garder que les propriétés correspondant aux champs *explicitement* passés
    requested_kinds = set()
    if "title" in fields: requested_kinds.add("title")
    if "date" in fields: requested_kinds.add("date")
    if "sport" in fields: requested_kinds.add("sport")
    if "types" in fields: requested_kinds.add("types")
    if "duration_min" in fields: requested_kinds.add("duration")
    if "notes" in fields: requested_kinds.add("notes")
    if "distance_km" in fields: requested_kinds.add("distance_km")  # ✅
    # Si date fournie → on met aussi 'month' + 'iso_week'
    if "date" in fields:
        requested_kinds.update({"month", "iso_week"})

    keep_real_names = set()
    for kind in requested_kinds:
        real = _resolve_property_name(kind)
        if real:
            keep_real_names.add(real)

    payload = {k: v for k, v in payload_full.items() if k in keep_real_names}

    return client.pages.update(page_id=page_id, properties=payload)

def fetch_plan_sessions(
    *,
    after_days: Optional[int] = None,
    before_days: Optional[int] = None,
    ref: Optional[p.DateTime] = None,
    page_size: int = 100,
    hard_limit: int = 500,
) -> List[PlanSession]:
    """
    Récupère les pages (optionnellement filtrées par date autour d'une référence).
    Compatible avec l'appel de l'UI: fetch_plan_sessions(after_days=..., before_days=...).
    """
    date_prop = _resolve_property_name("date")
    has_date = date_prop is not None

    ref_dt = ref or _now_tz()
    start_iso = None
    end_iso = None
    if has_date and (after_days is not None or before_days is not None):
        if after_days is not None:
            start_iso = ref_dt.add(days=after_days).to_datetime_string()
        if before_days is not None:
            end_iso = ref_dt.add(days=before_days).to_datetime_string()

    notion_filter: Optional[Dict[str, Any]] = None
    if has_date and (start_iso or end_iso):
        and_filters: List[Dict[str, Any]] = []
        if start_iso:
            and_filters.append({"property": date_prop, "date": {"on_or_after": start_iso}})
        if end_iso:
            and_filters.append({"property": date_prop, "date": {"on_or_before": end_iso}})
        notion_filter = and_filters[0] if len(and_filters) == 1 else {"and": and_filters}

    results: List[PlanSession] = []
    start_cursor: Optional[str] = None

    while True:
        kwargs: Dict[str, Any] = {
            "database_id": DB_PLAN,
            "page_size": min(max(1, page_size), 100),
        }
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        if notion_filter:
            kwargs["filter"] = notion_filter

        res = client.databases.query(**kwargs)
        pages = res.get("results", [])
        for ppage in pages:
            results.append(_parse_page_to_plan(ppage))
            if len(results) >= hard_limit:
                return results

        start_cursor = res.get("next_cursor")
        if not start_cursor:
            break

    return results

# (Facultatif) fenêtre mensuelle pratique
def fetch_month_sessions(year: int, month: int) -> List[PlanSession]:
    date_prop = _resolve_property_name("date")
    if not date_prop:
        return fetch_plan_sessions()

    tz_now = _now_tz()
    start = p.datetime(year, month, 1, tz=tz_now.tz)
    end = start.end_of("month")
    return fetch_plan_sessions(
        ref=start,
        after_days=0,
        before_days=(end - start).days
    )

# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires UI
# ─────────────────────────────────────────────────────────────────────────────
def ensure_month_and_duration(date: p.DateTime, duration_min: Optional[int]) -> Dict[str, Any]:
    """
    Renvoie des valeurs normalisées (utilitaire simple pour compat UI).
    - month_label: 'Janvier' / 'Février'… dérivé de la date (FR)
    - duration_min: int|None
    """
    month_label = date.format("MMMM", locale="fr").capitalize() if date else None
    dur = int(duration_min) if duration_min is not None else None
    return {"month_label": month_label, "duration_min": dur}

# ─────────────────────────────────────────────────────────────────────────────
# Backfill / Autofill : Semaine ISO / Mois / Durée depuis Nom
# ─────────────────────────────────────────────────────────────────────────────
def _iter_db_pages(notion: Client, db_id: str) -> Iterable[Dict[str, Any]]:
    """Itère sur toutes les pages d'une database Notion (pagination gérée)."""
    cursor = None
    while True:
        resp = notion.databases.query(database_id=db_id, start_cursor=cursor) if cursor else notion.databases.query(database_id=db_id)
        for r in resp.get("results", []):
            yield r
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

def backfill_iso_week_for_plans(
    notion: Optional[Client] = None,
    db_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Calcule et remplit **Semaine ISO** (= 'YYYY-Www') pour toutes les pages
    de la DB Plans, à partir de 'Date prévue'.
    - Supporte propriété **rich_text/text** ou **select** (création d’options auto).
    """
    load_dotenv_if_exists()
    notion = notion or client
    db_id = db_id or DB_PLAN

    props_schema = _load_db_properties()
    p_date  = _resolve_property_name("date")
    p_week  = _resolve_property_name("iso_week")

    if not p_week or not p_date:
        return {"updated": 0, "skipped_no_date": 0, "skipped_unknown_kind": 1}

    updated = skipped_no_date = 0

    for page in _iter_db_pages(notion, db_id):
        props = page.get("properties", {})
        # date
        start = props.get(p_date, {}).get("date", {}).get("start")
        if not start:
            skipped_no_date += 1
            continue
        try:
            dt = p.parse(start).in_timezone(SPORT_TZ)
        except Exception:
            skipped_no_date += 1
            continue

        label = _iso_week_label(dt)
        w_type = props_schema[p_week]["type"]

        # déjà à jour ?
        already = False
        if w_type in ("rich_text", "text"):
            current = _text_from_rich(props.get(p_week, {}).get("rich_text"))
            already = (current == label)
        elif w_type == "select":
            cur = props.get(p_week, {}).get("select", {})
            already = (cur.get("name") == label)

        if already:
            continue

        payload: Dict[str, Any] = {}
        if w_type in ("rich_text", "text"):
            payload[p_week] = {"rich_text": [{"text": {"content": label}}]}
        elif w_type == "select":
            _ensure_select_option(p_week, label)
            payload[p_week] = {"select": {"name": label}}

        if payload and not dry_run:
            notion.pages.update(page_id=page["id"], properties=payload)
            updated += 1

    return {"updated": updated, "skipped_no_date": skipped_no_date, "skipped_unknown_kind": 0}

def autofill_plan_fields(page_size: int = 100, hard_limit: int = 1000) -> Dict[str, int]:
    """
    Parcourt la DB Plans et complète automatiquement :
      - Semaine ISO ('YYYY-Www') ← à partir de 'Date prévue'
      - Mois (select)            ← à partir de 'Date prévue'
      - Durée prévue (min)       ← à partir de 'Nom' [+ RAC si non précisée]
        (Crossfit/Hyrox => 60 min si aucune durée parsée)

    Retourne un petit compteur d'updates effectués.
    """
    props_schema = _load_db_properties()
    p_title = _resolve_property_name("title")
    p_date  = _resolve_property_name("date")
    p_sport = _resolve_property_name("sport")
    p_dur   = _resolve_property_name("duration")
    p_month = _resolve_property_name("month")
    p_week  = _resolve_property_name("iso_week")

    updated = {"week": 0, "month": 0, "duration": 0}
    start_cursor: Optional[str] = None
    processed = 0

    while True:
        res = client.databases.query(
            database_id=DB_PLAN,
            page_size=min(max(1, page_size), 100),
            **({"start_cursor": start_cursor} if start_cursor else {})
        )
        pages = res.get("results", [])
        for pg in pages:
            processed += 1
            props = pg.get("properties", {})
            updates: Dict[str, Any] = {}

            # Lire date
            dt: Optional[p.DateTime] = None
            if p_date and props.get(p_date, {}).get("date", {}).get("start"):
                try:
                    dt = p.parse(props[p_date]["date"]["start"]).in_timezone(SPORT_TZ)
                except Exception:
                    dt = None

            # 1) Semaine ISO -> 'YYYY-Www'
            if dt and p_week:
                label = _iso_week_label(dt)
                w_type = props_schema[p_week]["type"]
                if w_type in ("rich_text", "text"):
                    current_week = _text_from_rich(props.get(p_week, {}).get("rich_text")) if props.get(p_week) else ""
                    if current_week != label:
                        updates[p_week] = {"rich_text": [{"text": {"content": label}}]}
                elif w_type == "select":
                    cur = props.get(p_week, {}).get("select", {}).get("name")
                    if cur != label:
                        _ensure_select_option(p_week, label)
                        updates[p_week] = {"select": {"name": label}}

            # 2) Mois
            if dt and p_month and props_schema[p_month]["type"] == "select":
                new_month = dt.format("MMMM", locale="fr").capitalize()
                cur = props.get(p_month, {}).get("select", {}).get("name")
                if cur != new_month:
                    updates[p_month] = {"select": {"name": new_month}}

            # 3) Durée depuis Nom (+RAC)
            title_txt = ""
            if p_title and props.get(p_title, {}).get("title"):
                title_txt = _text_from_rich(props[p_title]["title"])
            sport_name = None
            if p_sport and props.get(p_sport, {}).get("select"):
                sport_name = props[p_sport]["select"].get("name")

            parsed = parse_duration_from_name(title_txt, sport_hint=sport_name)
            cur_dur = props.get(p_dur, {}).get("number") if p_dur else None
            if parsed is not None and p_dur and props_schema[p_dur]["type"] == "number":
                if cur_dur != int(parsed):
                    updates[p_dur] = {"number": int(parsed)}

            # Push si nécessaire
            if updates:
                client.pages.update(page_id=pg["id"], properties=updates)
                updated["week"] += int(p_week in updates)
                updated["month"] += int(p_month in updates)
                updated["duration"] += int(p_dur in updates)

            if processed >= hard_limit:
                return updated

        start_cursor = res.get("next_cursor")
        if not start_cursor:
            break

    return updated

# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic rapide (utile si la création ne marche pas)
# ─────────────────────────────────────────────────────────────────────────────
def debug_probe(create_dummy: bool = False) -> Dict[str, Any]:
    """
    Diagnostic rapide : renvoie le mapping résolu et (optionnel) crée une page de test.
    """
    props = _load_db_properties()
    resolved = {k: _resolve_property_name(k) for k in PROP_ALIASES.keys()}
    out: Dict[str, Any] = {
        "database_id": DB_PLAN,
        "resolved": resolved,
        "available_props": list(props.keys()),
    }
    if create_dummy:
        dt = _now_tz()
        try:
            page = create_plan(
                title=f"[TEST] {dt.format('YYYY-MM-DD HH:mm')}",
                date=dt,
                sport="Test",
                types=["Debug"],
                duration_min=42,
                notes="Page de test auto",
                distance_km=12.3,
            )
            out["dummy_page_id"] = page.get("id")
        except Exception as e:
            out["dummy_error"] = str(e)
    return out
