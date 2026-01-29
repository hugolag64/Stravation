# scripts/backfill_strava.py
from __future__ import annotations

import os
import math
import time
from typing import Dict, Optional, Tuple, Any, List, Set

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

from stravation.services.google_sheets import GoogleSheetsClient  # ✅

app = typer.Typer(
    add_completion=False,
    help="Backfill des champs Strava → Notion (FC, TRIMP, Suffer Score, etc.) "
         "et log optionnel vers Google Sheets."
)

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
# Google Sheets (BDD sport)
# ─────────────────────────────────────────────────────────────────────────────
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
SPORT_SHEET_NAME = os.getenv("SPORT_SHEET_NAME", "BDD sport")

_sheets_client: Optional[GoogleSheetsClient] = None
if GOOGLE_SHEETS_ID:
    try:
        _sheets_client = GoogleSheetsClient(GOOGLE_SHEETS_ID)
    except Exception:
        _sheets_client = None


def _format_duration_for_sheet(moving_time_s: Optional[float]) -> Tuple[str, Optional[int]]:
    """
    Retourne (durée 'hh:mm:00', durée en secondes) pour Google Sheets.
    (On utilise surtout la durée en secondes.)
    """
    if not moving_time_s:
        return "", None
    total = int(moving_time_s)
    h = total // 3600
    m = (total % 3600) // 60
    return f"{h:02d}:{m:02d}:00", total


def _format_pace(moving_time_s: Optional[float], distance_km: Optional[float]) -> str:
    """
    Pace moyen en min/km sous forme 'm:ss'.
    (Pas utilisé dans la feuille car calculé ensuite par formule.)
    """
    if not moving_time_s or not distance_km or distance_km <= 0:
        return ""
    pace_min_per_km = (moving_time_s / 60.0) / distance_km
    minutes = int(pace_min_per_km)
    seconds = int(round((pace_min_per_km - minutes) * 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"

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
            time.sleep(15)
            continue
        if r.status_code in (403, 404):
            return {}
        r.raise_for_status()
        return r.json()
    return {}

# ─────────────────────────────────────────────────────────────────────────────
# Calculs
# ─────────────────────────────────────────────────────────────────────────────
def trimp_bannister(
    duration_s: Optional[float],
    hr_avg: Optional[float],
    hr_max: Optional[float],
    hr_rest: float = 60.0,
    sex: str = "M",
) -> Optional[float]:
    if not duration_s or not hr_avg or not hr_max or hr_max <= hr_rest:
        return None
    duration_min = duration_s / 60.0
    hr_r = (hr_avg - hr_rest) / (hr_max - hr_rest)
    a, b = (0.86, 1.67) if sex.upper().startswith("F") else (0.64, 1.92)
    return round(duration_min * hr_r * a * math.e ** (b * hr_r), 1)

# ─────────────────────────────────────────────────────────────────────────────
# Notion helpers
# ─────────────────────────────────────────────────────────────────────────────
def db_schema(notion: Notion, db_id: str) -> Dict[str, str]:
    info = notion.databases.retrieve(db_id)
    return {name: prop.get("type") for name, prop in info.get("properties", {}).items()}


def read_prop(props: dict, name: str) -> Any:
    """Lit quelques types Notion courants sans s'énerver."""
    if name not in props:
        return None
    pprop = props[name]
    t = pprop.get("type")

    if t == "number":
        return pprop.get("number")
    if t == "checkbox":
        return pprop.get("checkbox")
    if t == "url":
        return pprop.get("url")
    if t == "status":
        st = pprop.get("status") or {}
        return st.get("name")
    if t == "select":
        sel = pprop.get("select") or {}
        return sel.get("name")
    if t in ("rich_text", "title"):
        arr = pprop.get(t) or []
        if not arr:
            return None
        return "".join([x.get("plain_text") or x.get("text", {}).get("content", "") for x in arr])
    if t == "date":
        d = pprop.get("date") or {}
        return d.get("start")
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
    targets = [
        "FC moy (bpm)", "FC max (bpm)", "Charge TRIMP", "Suffer Score",
        "Cadence moy", "Puissance moy (W)", "NP / Watts pondérés", "Calories",
    ]
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


def _build_sheet_row(
    detail: dict,
    sid: int,
    trimp: Optional[float],
) -> List[Any]:
    """
    Construit une ligne pour BDD sport à partir du détail Strava + TRIMP.

    Colonnes attendues (A → M) :

    A Nom
    B Date
    C Intensité (RPE) – laissé vide (saisie manuelle)
    D Distance (km)
    E Temps (hh:mm) – calculé plus tard par formule
    F Allure moy (min/km) – calculée plus tard par formule
    G D+ (m)
    H FC moy (bpm)
    I FCmax (bpm)
    J Calorie
    K Charge TRIMP
    L Strava ID
    M Durée (s)
    """
    name = detail.get("name") or ""

    # Date locale
    start_raw = detail.get("start_date_local") or detail.get("start_date")
    if start_raw:
        try:
            date_str = p.parse(start_raw).to_date_string()
        except Exception:
            date_str = ""
    else:
        date_str = ""

    # Distance en km
    distance_km = None
    if detail.get("distance") is not None:
        try:
            distance_km = round(float(detail["distance"]) / 1000.0, 2)
        except Exception:
            distance_km = None

    # Durée
    moving_time = detail.get("moving_time")  # secondes
    _, duree_sec = _format_duration_for_sheet(moving_time)
    _ = _format_pace(moving_time, distance_km)  # pas utilisé dans la feuille

    elev_gain = detail.get("total_elevation_gain") or ""

    avg_hr = detail.get("average_heartrate")
    max_hr = detail.get("max_heartrate")
    kcal   = detail.get("calories")

    return [
        name,                                            # A Nom
        date_str,                                        # B Date
        "",                                              # C Intensité (RPE)
        distance_km if distance_km is not None else "",  # D Distance (km)
        "",                                              # E Temps (formule)
        "",                                              # F Allure moy (formule)
        elev_gain,                                       # G D+ (m)
        avg_hr if avg_hr is not None else "",            # H FC moy
        max_hr if max_hr is not None else "",            # I FCmax
        kcal if kcal is not None else "",                # J Calorie
        trimp if trimp is not None else "",              # K Charge TRIMP
        sid,                                             # L Strava ID
        duree_sec if duree_sec is not None else "",      # M Durée (s)
    ]


def _build_sheet_row_from_notion(
    props: dict,
    schema: Dict[str, str],
    sid: int,
) -> List[Any]:
    """
    Fallback : construit une ligne à partir des propriétés Notion
    quand l'API Strava ne donne pas de détail (403/404).
    """
    name = read_prop(props, "Nom") or ""

    # Date (on prend juste la partie AAAA-MM-JJ)
    date_raw = read_prop(props, "Date")
    if date_raw:
        try:
            date_str = p.parse(date_raw).to_date_string()
        except Exception:
            date_str = str(date_raw)[:10]
    else:
        date_str = ""

    distance_km = read_prop(props, "Distance (km)") or ""
    elev_gain   = read_prop(props, "D+ (m)") or ""
    avg_hr      = read_prop(props, "FC moy (bpm)") or ""
    max_hr      = read_prop(props, "FC max (bpm)") or ""
    kcal        = read_prop(props, "Calories") or read_prop(props, "Calorie") or ""
    trimp       = read_prop(props, "Charge TRIMP") or ""

    duree_sec   = read_prop(props, "Durée (s)") or read_prop(props, "Temps (s)") or ""

    return [
        name,          # A Nom
        date_str,      # B Date
        "",            # C Intensité (RPE)
        distance_km,   # D Distance (km)
        "",            # E Temps (formule)
        "",            # F Allure moy (formule)
        elev_gain,     # G D+ (m)
        avg_hr,        # H FC moy
        max_hr,        # I FCmax
        kcal,          # J Calorie
        trimp,         # K Charge TRIMP
        sid,           # L Strava ID
        duree_sec,     # M Durée (s)
    ]


def _get_existing_strava_ids_from_sheet() -> Set[str]:
    """
    Lit la colonne L (Strava ID) **sur les 300 dernières lignes**
    de la feuille BDD sport et renvoie un set de strings pour déduplication.
    Cela évite toute pollution par d'anciennes données (2021/2022).
    """
    if _sheets_client is None:
        return set()

    # 🔥 On limite volontairement la lecture aux dernières lignes utiles
    # En pratique, 300 lignes couvrent largement ton année d'entraînement.
    RANGE = "L2:L400"

    try:
        values = _sheets_client.get_values(SPORT_SHEET_NAME, RANGE)
    except Exception as e:
        print(f"[warn] impossible de lire les Strava ID existants depuis Sheets: {e}")
        return set()

    existing: Set[str] = set()
    for row in values or []:
        if not row:
            continue
        v = str(row[0]).strip()
        if v:
            existing.add(v)
    return existing


# ─────────────────────────────────────────────────────────────────────────────
# Backfill principal
# ─────────────────────────────────────────────────────────────────────────────
@app.command()
def run(
    limit: int = typer.Option(0, help="Limite de pages à traiter (0 = no limit)."),
    only_missing: bool = typer.Option(True, help="Ne met à jour que si des champs manquent."),
    force: bool = typer.Option(False, help="Ignore only_missing et réécrit tout."),
    since: Optional[str] = typer.Option(None, help="Filtre par date Notion (>= YYYY-MM-DD)."),
    dry_run: bool = typer.Option(False, help="Aucune écriture Notion / Sheets."),
    sleep: float = typer.Option(0.15, help="Throttle entre updates (s)."),
):
    """
    Backfill des colonnes : FC moy, FC max, Charge TRIMP, Suffer Score (+ cadence, watts, calories).
    + log optionnel de chaque séance dans Google Sheets (BDD sport).
    Nécessite dans Notion la colonne 'Strava ID' et dans l'ENV les secrets Strava/Notion.
    """
    notion = Notion(auth=NOTION_TOKEN)
    schema = db_schema(notion, NOTION_DB)
    token  = get_strava_access_token()

    processed = 0
    updated   = 0

    # Déduplication *dans ce run*
    seen_ids: Set[int] = set()

    # Déduplication inter-runs : IDs déjà présents dans Google Sheets (colonne L)
    sheet_existing_ids: Set[str] = set()
    if _sheets_client is not None and not dry_run:
        sheet_existing_ids = _get_existing_strava_ids_from_sheet()

    cursor = None
    while True:
        query_payload: dict = {"database_id": NOTION_DB, "page_size": 100}

        if cursor:
            query_payload["start_cursor"] = cursor

        # Tri par date ascendante (du plus ancien au plus récent) si la prop Date existe
        if "Date" in schema and schema["Date"] == "date":
            query_payload["sorts"] = [
                {"property": "Date", "direction": "ascending"}
            ]

        if since:
            if "Date" in schema and schema["Date"] == "date":
                query_payload["filter"] = {
                    "property": "Date",
                    "date": {"on_or_after": since}
                }

        res = notion.databases.query(**query_payload)
        print("\n=== DEBUG: Résultats Notion renvoyés ===")
        for page in res.get("results", []):
            props = page["properties"]
            sid = extract_strava_id(props, schema)
            date_raw = read_prop(props, "Date")
            title = read_prop(props, "Nom") or "(sans nom)"
            print(f"- Nom={title!r} | Date={date_raw} | StravaID={sid}")
        print("=== FIN DEBUG ===\n")

        for page in res.get("results", []):
            page_id = page["id"]
            props   = page["properties"]

            sid = extract_strava_id(props, schema)
            if not sid:
                continue

            # Déduplication intra-run
            if sid in seen_ids:
                processed += 1
                if limit and processed >= limit:
                    print(f"[done] processed={processed}, updated={updated}")
                    return
                continue

            # On veut logger dans Sheets même si Notion n'a rien à mettre à jour
            must_update_notion = force or need_update(props, schema, only_missing=only_missing)

            # 1) Détails Strava
            detail = get_activity_detail(int(sid), token) or {}

            if not detail:
                # Cas Strava inaccessible (403/404). On log quand même côté Sheets
                nom_notion = read_prop(props, "Nom") or "(sans nom)"
                print(f"[no-detail] Impossible de récupérer Strava {sid} pour '{nom_notion}'")

                if _sheets_client is not None and not dry_run:
                    sid_str = str(sid)
                    if sid_str not in sheet_existing_ids:
                        row = _build_sheet_row_from_notion(props, schema, int(sid))
                        _sheets_client.append_row(SPORT_SHEET_NAME, row)
                        sheet_existing_ids.add(sid_str)

                seen_ids.add(sid)
                processed += 1
                if limit and processed >= limit:
                    print(f"[done] processed={processed}, updated={updated}")
                    return
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

            # ── 1) Update Notion si nécessaire ─────────────────────────────
            update_props: Dict[str, Any] = {}

            if must_update_notion:
                def maybe_set(name: str, value: Optional[float]):
                    if name not in schema:
                        return
                    if only_missing and not force:
                        current = read_prop(props, name)
                        if current not in (None, "", 0):
                            return
                    update_props[name] = {
                        "number": float(value) if value is not None else None
                    }

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

            # ── 2) Log systématique dans Google Sheets (si pas déjà présent) ──
            if _sheets_client is not None and not dry_run:
                try:
                    sid_str = str(sid)
                    if sid_str not in sheet_existing_ids:
                        row = _build_sheet_row(detail, int(sid), trimp)
                        _sheets_client.append_row(SPORT_SHEET_NAME, row)
                        sheet_existing_ids.add(sid_str)
                except Exception as e:
                    print(f"[warn] échec append Sheets pour activité {sid}: {e}")

            seen_ids.add(sid)
            processed += 1
            if limit and processed >= limit:
                print(f"[done] processed={processed}, updated={updated}")
                return

        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")



# ─────────────────────────────────────────────────────────────────────────────
# Backfill dernière année pratique
# ─────────────────────────────────────────────────────────────────────────────
@app.command()
def last_year(
    dry_run: bool = typer.Option(
        False,
        help="Mode test : n'écrit pas dans Notion / Sheets."
    ),
    sleep: float = typer.Option(
        0.15,
        help="Délai entre écritures pour limiter le spam API."
    )
):
    """
    Backfill Strava → Notion + Google Sheets **sur la dernière année**.
    Recalcule TRIMP, FC moy/max, calories, etc.
    Et push chaque activité dans Google Sheets (BDD sport).
    """
    one_year_ago = p.now().subtract(years=1).to_date_string()
    print(f"▶ Backfill dernière année depuis {one_year_ago}")

    run(
        limit=0,
        only_missing=True,
        force=True,          # on réécrit les champs Notion
        since=one_year_ago,
        dry_run=dry_run,
        sleep=sleep,
    )

@app.command()
def recent(
    days: int = typer.Option(
        7,
        help="Nombre de jours à remonter (par défaut 7)."
    ),
    dry_run: bool = typer.Option(
        False,
        help="Mode test : n'écrit pas dans Notion / Sheets."
    ),
    sleep: float = typer.Option(
        0.15,
        help="Délai entre écritures pour limiter le spam API."
    ),
):
    """
    Backfill Strava → Notion + Google Sheets **sur les X derniers jours**.
    Utile après un import Strava récent pour ne pas repasser toute l'année.
    """
    since_date = p.now().subtract(days=days).to_date_string()
    print(f"▶ Backfill {days} jours depuis {since_date}")

    run(
        limit=0,
        only_missing=True,
        force=True,      # on recalcule les champs Notion
        since=since_date,
        dry_run=dry_run,
        sleep=sleep,
    )


if __name__ == "__main__":
    app()
