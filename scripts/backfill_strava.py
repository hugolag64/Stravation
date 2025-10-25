# scripts/backfill_strava.py
from __future__ import annotations

import os
import math
import time
from typing import Dict, Optional, Tuple, Any

import httpx
import pendulum as p
import typer
from notion_client import Client as Notion

# ─────────────────────────────────────────────────────────────────────────────
# Chargement .env facultatif
# ─────────────────────────────────────────────────────────────────────────────
try:
    from stravation.utils.envtools import load_dotenv_if_exists
    load_dotenv_if_exists()
except Exception:
    pass

app = typer.Typer(add_completion=False, help="Backfill des champs Strava → Notion (FC, TRIMP, Suffer Score, etc.)")

# ─────────────────────────────────────────────────────────────────────────────
# ENV requis
# ─────────────────────────────────────────────────────────────────────────────
NOTION_TOKEN = os.getenv("NOTION_API_KEY") or ""
NOTION_DB    = os.getenv("NOTION_DB_ACTIVITIES") or os.getenv("NOTION_DB_SPORT") or ""
STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID") or ""
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET") or ""
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN") or ""

if not (NOTION_TOKEN and NOTION_DB and STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN):
    missing = [k for k, v in {
        "NOTION_API_KEY": NOTION_TOKEN,
        "NOTION_DB_ACTIVITIES / NOTION_DB_SPORT": NOTION_DB,
        "STRAVA_CLIENT_ID": STRAVA_CLIENT_ID,
        "STRAVA_CLIENT_SECRET": STRAVA_CLIENT_SECRET,
        "STRAVA_REFRESH_TOKEN": STRAVA_REFRESH_TOKEN,
    }.items() if not v]
    raise SystemExit(f"[backfill] Variables manquantes: {', '.join(missing)}")

# ─────────────────────────────────────────────────────────────────────────────
# Strava
# ─────────────────────────────────────────────────────────────────────────────
def get_strava_access_token() -> str:
    url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": STRAVA_REFRESH_TOKEN,
    }
    r = httpx.post(url, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

def get_activity_detail(activity_id: int, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://www.strava.com/api/v3/activities/{activity_id}"
    for _ in range(2):
        r = httpx.get(url, headers=headers, params={"include_all_efforts": False}, timeout=60)
        if r.status_code == 429:
            time.sleep(15); continue
        if r.status_code in (403, 404):
            return {}
        r.raise_for_status()
        return r.json()
    return {}

# ─────────────────────────────────────────────────────────────────────────────
# Calculs
# ─────────────────────────────────────────────────────────────────────────────
def trimp_bannister(duration_s: Optional[float],
                    hr_avg: Optional[float],
                    hr_max: Optional[float],
                    hr_rest: float = 60.0,
                    sex: str = "M") -> Optional[float]:
    if not duration_s or not hr_avg or not hr_max or hr_max <= hr_rest:
        return None
    duration_min = duration_s / 60.0
    hr_r = (hr_avg - hr_rest) / (hr_max - hr_rest)
    a, b = (0.86, 1.67) if sex.upper().startswith("F") else (0.64, 1.92)
    return round(duration_min * hr_r * a * math.e ** (b * hr_r), 1)

# ─────────────────────────────────────────────────────────────────────────────
# Notion helpers
# ─────────────────────────────────────────────────────────────────────────────
def db_schema(notion: Notion, db_id: string) -> Dict[str, str]:
    info = notion.databases.retrieve(db_id)
    return {name: prop.get("type") for name, prop in info.get("properties", {}).items()}

def read_prop(props: dict, name: str) -> Any:
    """Lit quelques types Notion courants sans s'énerver."""
    if name not in props:
        return None
    p = props[name]
    t = p.get("type")
    if t == "number":
        return p.get("number")
    if t == "checkbox":
        return p.get("checkbox")
    if t == "url":
        return p.get("url")
    if t == "status":
        st = p.get("status") or {}
        return st.get("name")
    if t == "select":
        sel = p.get("select") or {}
        return sel.get("name")
    if t in ("rich_text", "title"):
        arr = p.get(t) or []
        if not arr:
            return None
        return "".join([x.get("plain_text") or x.get("text", {}).get("content", "") for x in arr])
    return None

def extract_strava_id(props: dict, schema: Dict[str, str]) -> Optional[int]:
    if "Strava ID" not in props:
        return None
    t = schema.get("Strava ID")
    if t == "number":
        v = props["Strava ID"]["number"]
        return int(v) if v is not None else None
    # title / rich_text / text
    txt = read_prop(props, "Strava ID")
    try:
        return int(txt) if txt else None
    except Exception:
        return None

def need_update(props: dict, schema: Dict[str, str], only_missing: bool) -> bool:
    """Retourne True si une MAJ est utile sur cette page."""
    targets = ["FC moy (bpm)", "FC max (bpm)", "Charge TRIMP", "Suffer Score",
               "Cadence moy", "Puissance moy (W)", "NP / Watts pondérés", "Calories"]
    present = [name for name in targets if name in schema]
    if not present:
        return False
    if not only_missing:
        return True
    # Only if au moins un des champs est manquant
    for name in present:
        v = read_prop(props, name)
        if v in (None, "", 0) and name != "Charge TRIMP":
            return True
        if name == "Charge TRIMP" and v in (None, ""):
            return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Backfill
# ─────────────────────────────────────────────────────────────────────────────
@app.command()
def run(
    limit: int = typer.Option(0, help="Limite de pages à traiter (0 = no limit)."),
    only_missing: bool = typer.Option(True, help="Ne met à jour que si des champs manquent."),
    force: bool = typer.Option(False, help="Ignore only_missing et réécrit tout."),
    since: Optional[str] = typer.Option(None, help="Filtre par date Notion (>= YYYY-MM-DD)."),
    dry_run: bool = typer.Option(False, help="Aucune écriture Notion."),
    sleep: float = typer.Option(0.15, help="Throttle entre updates (s)."),
):
    """
    Backfill des colonnes : FC moy, FC max, Charge TRIMP, Suffer Score (+ cadence, watts, calories).
    Nécessite dans Notion la colonne 'Strava ID' et dans l'ENV les secrets Strava/Notion.
    """
    notion = Notion(auth=NOTION_TOKEN)
    schema = db_schema(notion, NOTION_DB)
    token  = get_strava_access_token()

    processed = 0
    updated   = 0

    cursor = None
    while True:
        query_payload: dict = {"database_id": NOTION_DB, "page_size": 100}
        if cursor:
            query_payload["start_cursor"] = cursor
        if since:
            # filtre simple côté Notion si la prop Date existe
            if "Date" in schema and schema["Date"] == "date":
                query_payload["filter"] = {
                    "property": "Date",
                    "date": {"on_or_after": f"{since}"},
                }
        res = notion.databases.query(**query_payload)

        for page in res.get("results", []):
            page_id = page["id"]
            props   = page["properties"]

            sid = extract_strava_id(props, schema)
            if not sid:
                continue

            if not force and not need_update(props, schema, only_missing=True):
                processed += 1
                if limit and processed >= limit:
                    print(f"[done] processed={processed}, updated={updated}")
                    return
                continue

            # 1) Détails Strava
            detail = get_activity_detail(int(sid), token) or {}
            if not detail:
                processed += 1
                continue

            avg_hr  = detail.get("average_heartrate")
            max_hr  = detail.get("max_heartrate")
            suffer  = detail.get("suffer_score")
            cadence = detail.get("average_cadence")
            avg_w   = detail.get("average_watts")
            wavg_w  = detail.get("weighted_average_watts")
            kcal    = detail.get("calories")
            mv_s    = detail.get("moving_time")

            trimp = trimp_bannister(
                duration_s=mv_s,
                hr_avg=avg_hr,
                hr_max=max_hr,
                hr_rest=float(os.getenv("SPORT_HR_REST", "60")),
                sex=os.getenv("SPORT_SEX", "M"),
            )

            update_props: Dict[str, Any] = {}
            def maybe_set(name: str, value: Optional[float]):
                if name not in schema:
                    return
                if only_missing and not force:
                    current = read_prop(props, name)
                    if current not in (None, "", 0):
                        return
                update_props[name] = {"number": float(value) if value is not None else None}

            maybe_set("FC moy (bpm)", avg_hr)
            maybe_set("FC max (bpm)", max_hr)
            maybe_set("Charge TRIMP", trimp)
            maybe_set("Suffer Score", suffer)
            maybe_set("Cadence moy", cadence)
            maybe_set("Puissance moy (W)", avg_w)
            maybe_set("NP / Watts pondérés", wavg_w)
            maybe_set("Calories", kcal)

            if update_props:
                updated += 1
                if dry_run:
                    print(f"[dry-run] update {page_id} ← {update_props}")
                else:
                    notion.pages.update(page_id=page_id, properties=update_props)
                    time.sleep(sleep)

            processed += 1
            if limit and processed >= limit:
                print(f"[done] processed={processed}, updated={updated}")
                return

        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")

    print(f"[done] processed={processed}, updated={updated}")


if __name__ == "__main__":
    app()
