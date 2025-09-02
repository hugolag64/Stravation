from __future__ import annotations
import os
from typing import List, Dict, Optional
import httpx
import pendulum as p

STRAVA_TOKEN = os.getenv("STRAVA_ACCESS_TOKEN", "")

class StravaService:
    """
    Wrapper Strava: listage paginé + mise à jour live des métadonnées.
    """
    BASE = "https://www.strava.com/api/v3"

    def __init__(self, per_page: int = 10):
        self.per_page = per_page
        if not STRAVA_TOKEN:
            raise RuntimeError("STRAVA_ACCESS_TOKEN manquant")

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.BASE,
            headers={"Authorization": f"Bearer {STRAVA_TOKEN}", "User-Agent": "stravanotion/ui"},
            timeout=30.0,
        )

    # ---------- Read ----------
    def list_recent(self, page: int = 1) -> List[Dict]:
        """
        Retourne une liste normalisée (id, name, sport_type, dates, metrics).
        """
        with self._client() as c:
            r = c.get("/athlete/activities", params={"page": page, "per_page": self.per_page})
            r.raise_for_status()
            acts = r.json()

        out = []
        tz = os.getenv("SPORT_TZ", "Indian/Reunion")
        for a in acts:
            start = p.parse(a["start_date"])  # UTC
            out.append({
                "id": a["id"],
                "name": a.get("name") or "",
                "sport_type": a.get("sport_type") or a.get("type") or "Run",
                "description": a.get("description") or "",
                "distance_km": round(float(a.get("distance", 0.0))/1000.0, 2),
                "moving_time_min": int(a.get("moving_time", 0)) // 60,
                "elevation_gain_m": int(a.get("total_elevation_gain", 0) or 0),
                "start_dt_utc": start.to_iso8601_string(),
                "start_local": start.in_timezone(tz).format("YYYY-MM-DD HH:mm"),
            })
        return out

    # ---------- Update (live) ----------
    def update_activity(
        self,
        activity_id: int,
        *,
        name: Optional[str] = None,
        sport_type: Optional[str] = None,      # ex: Run, TrailRun, Ride, Workout…
        description: Optional[str] = None,
        commute: Optional[bool] = None,
        trainer: Optional[bool] = None,
        gear_id: Optional[str] = None,
        is_private: Optional[bool] = None,
    ) -> Dict:
        """
        Met à jour une activité Strava (métadonnées). L’API NE permet PAS
        d’éditer distance/D+/durations (valeurs mesurées).
        """
        data: Dict[str, object] = {}
        if name is not None:
            data["name"] = name
        if sport_type is not None:
            data["sport_type"] = sport_type
        if description is not None:
            data["description"] = description
        if commute is not None:
            data["commute"] = int(bool(commute))
        if trainer is not None:
            data["trainer"] = int(bool(trainer))
        if gear_id is not None:
            data["gear_id"] = gear_id
        if is_private is not None:
            data["private"] = int(bool(is_private))

        with self._client() as c:
            r = c.put(f"/activities/{activity_id}", data=data)
            r.raise_for_status()
            return r.json()
