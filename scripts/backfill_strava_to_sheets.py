from __future__ import annotations
import os
import time
import math
from typing import List, Dict, Optional

import httpx
import pendulum as p
from dotenv import load_dotenv

# Charge .env
load_dotenv()

from stravation.services.google_sheets import GoogleSheetsClient

# ─────────────────────────────────────────────────────────────
# STRAVA CONFIG
# ─────────────────────────────────────────────────────────────
STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")
SPORT_TZ             = os.getenv("SPORT_TZ", "Indian/Reunion")

if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET or not STRAVA_REFRESH_TOKEN:
    raise RuntimeError("❌ Missing Strava env vars in .env")


def get_strava_token() -> str:
    url = "https://www.strava.com/oauth/token"
    data = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    r = httpx.post(url, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def list_activities(token: str, after_ts: int) -> List[Dict]:
    """
    Télécharge toutes les activités Strava depuis after_ts (epoch seconds).
    On récupère les "summary activities" (suffisant pour distance, FC, calories…)
    """
    out: List[Dict] = []
    page = 1

    while True:
        url = "https://www.strava.com/api/v3/athlete/activities"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"page": page, "per_page": 100, "after": after_ts}

        r = httpx.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        acts = r.json()

        if not acts:
            break

        out.extend(acts)
        page += 1
        time.sleep(0.2)  # throttle léger

    # Tri du plus ancien au plus récent
    out.sort(key=lambda a: a.get("start_date") or "")
    return out


# ─────────────────────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
SHEET_NAME       = os.getenv("SPORT_SHEET_NAME", "BDD sport")

if not GOOGLE_SHEETS_ID:
    raise RuntimeError("❌ GOOGLE_SHEETS_ID manquant dans .env")

sheets = GoogleSheetsClient(GOOGLE_SHEETS_ID)

# ─────────────────────────────────────────────────────────────
# Calculs utilitaires (durée, allure, TRIMP)
# ─────────────────────────────────────────────────────────────
def format_duration(seconds: int) -> str:
    """
    Retourne une durée au format 'hh:mm:00' (Google Sheets affichera hh:mm).
    """
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}:{m:02d}:00"


def format_pace(seconds: int, distance_km: float) -> str:
    """
    Allure moyenne en min/km au format 'm:ss'.
    """
    if not distance_km or distance_km <= 0 or not seconds:
        return ""
    pace_min_per_km = (seconds / 60.0) / distance_km
    minutes = int(pace_min_per_km)
    sec = int(round((pace_min_per_km - minutes) * 60))
    if sec == 60:
        minutes += 1
        sec = 0
    return f"{minutes}:{sec:02d}"


def trimp_bannister(
    duration_s: Optional[float],
    hr_avg: Optional[float],
    hr_max: Optional[float],
    hr_rest: float = 60.0,
    sex: str = "M",
) -> Optional[float]:
    """
    Même formule que dans scripts/backfill_strava.py
    """
    if not duration_s or not hr_avg or not hr_max or hr_max <= hr_rest:
        return None
    duration_min = duration_s / 60.0
    hr_r = (hr_avg - hr_rest) / (hr_max - hr_rest)
    a, b = (0.86, 1.67) if sex.upper().startswith("F") else (0.64, 1.92)
    return round(duration_min * hr_r * a * math.e ** (b * hr_r), 1)


# ─────────────────────────────────────────────────────────────
# Transformation activité → ligne Sheets
# ─────────────────────────────────────────────────────────────
def activity_to_row(a: Dict) -> List:
    """
    Convertit une activité Strava brute → ligne Google Sheets.

    Colonnes attendues dans 'BDD sport' (A → N) :
    A Nom
    B Date
    C Intensité (RPE)
    D Distance (km)
    E Temps (hh:mm) – laissé vide (calculé par formule à partir de N)
    F Allure moy (min/km) – laissée vide (calculée par formule)
    G D+ (m)
    H FC moyenne
    I FCmax (bpm)
    J Calories
    K Charge TRIMP
    L (réservé / vide)
    M Strava ID
    N Durée (s)
    """
    # Date locale
    start_raw = a.get("start_date_local") or a.get("start_date")
    start = p.parse(start_raw).in_timezone(SPORT_TZ) if start_raw else p.now(tz=SPORT_TZ)
    date_str = start.to_date_string()

    duration_s = int(a.get("moving_time") or 0)

    # Distance en km
    if a.get("distance") is not None:
        distance_km = round(float(a.get("distance", 0.0)) / 1000.0, 2)
    else:
        distance_km = ""

    elevation = a.get("total_elevation_gain", "") or ""

    avg_hr = a.get("average_heartrate")
    max_hr = a.get("max_heartrate")
    calories = a.get("calories")

    trimp = trimp_bannister(
        duration_s=duration_s,
        hr_avg=avg_hr,
        hr_max=max_hr,
        hr_rest=float(os.getenv("SPORT_HR_REST", "60")),
        sex=os.getenv("SPORT_SEX", "M"),
    )

    sid = a["id"]

    return [
        a.get("name") or "",                     # A Nom
        date_str,                                # B Date
        "",                                      # C Intensité (RPE)
        distance_km,                             # D Distance (km)
        "",                                      # E Temps (formule)
        "",                                      # F Allure moy (formule)
        elevation,                               # G D+ (m)
        avg_hr or "",                            # H FC moyenne
        max_hr or "",                            # I FCmax (bpm)
        calories or "",                          # J Calories
        trimp if trimp is not None else "",      # K Charge TRIMP
        "",                                      # L (réservé)
        sid,                                     # M Strava ID
        duration_s,                              # N Durée (s)
    ]



# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main(years_back: int = 2):
    """
    Backfill complet vers Google Sheets.
    years_back = nombre d'années à remonter (par défaut 2 ans)
    """
    print(f"📥 Import Strava → Google Sheets ({years_back} ans en arrière)…")

    token = get_strava_token()
    date_start = p.now(tz=SPORT_TZ).subtract(years=years_back)
    after_ts = int(date_start.timestamp())

    acts = list_activities(token, after_ts)
    print(f"👉 {len(acts)} activités trouvées")

    rows = [activity_to_row(a) for a in acts]

    # Push en une fois (du plus ancien au plus récent)
    sheets.append_rows(SHEET_NAME, rows)

    print("✅ Backfill terminé : activités importées dans Google Sheets.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backfill Strava → Google Sheets (BDD sport).")
    parser.add_argument("--years", type=int, default=2, help="Nombre d'années à remonter (par défaut 2)")
    args = parser.parse_args()

    main(years_back=args.years)
