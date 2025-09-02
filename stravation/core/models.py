from __future__ import annotations
from pydantic import BaseModel, field_serializer
from typing import Optional
import pendulum as p
from ..config import SPORT_TZ  # ✅ config est au niveau parent (stravation/config.py)

# petit helper fuseau
def _tz():
    return p.timezone(SPORT_TZ)


class StravaActivity(BaseModel):
    id: int
    name: str
    sport_type: str
    distance: float               # m
    moving_time: int              # s
    elapsed_time: int             # s
    total_elevation_gain: float   # m
    start_date: str               # ISO UTC (ex: "2025-08-29T01:45:00Z")
    start_date_local: Optional[str] = None  # ISO local (Strava) sans offset
    timezone: Optional[str] = None          # tz Strava si fourni

    @property
    def start_dt_utc(self) -> p.DateTime:
        """Datetime en UTC (source Strava)."""
        return p.parse(self.start_date).in_timezone("UTC")

    @property
    def start_dt_local(self) -> p.DateTime:
        """
        Toujours partir de start_date (UTC) et convertir vers SPORT_TZ.
        Évite le double-shift causé par start_date_local souvent tagué 'Z'.
        """
        return p.parse(self.start_date).in_timezone(_tz())


class NotionActivity(BaseModel):
    # Champs “source” et dérivés normalisés pour Notion
    strava_id: str
    name: str
    sport_raw: str
    sport: str                    # libellé mappé (tes noms)
    date_local: p.DateTime        # date/heure locale (Réunion)
    week_iso: str                 # ex: "2025-W34"
    year: int
    distance_km: float
    moving_time_s: int
    elevation_gain_m: float

    # Dérivés utiles pour affichage
    @property
    def pace_min_km(self) -> Optional[str]:
        if self.distance_km <= 0 or self.moving_time_s <= 0:
            return None
        sec_per_km = int(self.moving_time_s / self.distance_km)
        mm = sec_per_km // 60
        ss = sec_per_km % 60
        return f"{mm:02d}:{ss:02d}"

    @property
    def time_hm(self) -> str:
        m = self.moving_time_s // 60
        return f"{m // 60:02d}:{m % 60:02d}"

    @property
    def strava_url(self) -> str:
        return f"https://www.strava.com/activities/{self.strava_id}"

    model_config = {"arbitrary_types_allowed": True}

    @field_serializer("date_local")
    def _ser_date_local(self, dt: p.DateTime, _info):
        # Notion aime l’ISO 8601 avec offset, ex: "2025-08-29T05:45:00+04:00"
        return dt.in_timezone(_tz()).to_iso8601_string()


class Session(BaseModel):
    # (gardé pour ton planner)
    date: p.DateTime      # date locale (jour)
    sport: str            # "course" | "crossfit" | ...
    title: str            # "Course – Endurance 45’"
    minutes: int
    time_hm: str          # "HH:MM" pour la séance
    morning_hm: str       # "HH:MM" pour le rappel matin

    model_config = {"arbitrary_types_allowed": True}
