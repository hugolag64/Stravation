from __future__ import annotations
import os
import time
from typing import List, Dict, Optional
import httpx
import pendulum as p

from stravation.utils.envtools import load_dotenv_if_exists

STRAVA_OAUTH_URL = "https://www.strava.com/oauth/token"
BASE_API = "https://www.strava.com/api/v3"


class StravaService:
    """
    Service Strava minimal et propre :
      - charge .env automatiquement
      - gère STRAVA_ACCESS_TOKEN + expirations
      - rafraîchit via STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN si nécessaire
      - expose list_recent() et update_activity()
    """

    def __init__(self, per_page: int = 10, timeout: float = 30.0, user_agent: str = "stravanotion/ui"):
        load_dotenv_if_exists()  # .env -> os.environ (n'écrase pas l'existant)
        self.per_page = per_page
        self.timeout = timeout
        self.user_agent = user_agent

        self._client = httpx.Client(base_url=BASE_API, timeout=timeout, headers={"User-Agent": self.user_agent})
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

        # Initialise le token (direct ou via refresh)
        self._bootstrap_token()

    # ──────────────────────────────────────────────────────────────────────
    # Auth
    # ──────────────────────────────────────────────────────────────────────
    def _bootstrap_token(self) -> None:
        """
        Essaie d'utiliser STRAVA_ACCESS_TOKEN si présent/valide, sinon refresh.
        Variables prises en compte :
          - STRAVA_ACCESS_TOKEN (optionnel)
          - STRAVA_EXPIRES_AT (optionnel)
          - STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REFRESH_TOKEN (requis si pas d'AT)
        """
        tok = os.getenv("STRAVA_ACCESS_TOKEN")
        exp_str = os.getenv("STRAVA_EXPIRES_AT")
        now = time.time()

        if tok:
            # Si on a une date d'expiration et qu'elle est bientôt échue, on refresh
            if exp_str:
                try:
                    exp = float(exp_str)
                except ValueError:
                    exp = now  # force refresh
            else:
                # Pas d'expiration connue → on considère un buffer court
                exp = now + 1800.0
            if exp > now + 60.0:
                self._access_token, self._expires_at = tok, exp
                return

        # Pas de token utilisable → refresh obligatoire
        self._refresh_access_token_or_raise()

    def _refresh_access_token_or_raise(self) -> None:
        cid = os.getenv("STRAVA_CLIENT_ID")
        csec = os.getenv("STRAVA_CLIENT_SECRET")
        rtok = os.getenv("STRAVA_REFRESH_TOKEN")
        if not (cid and csec and rtok):
            raise RuntimeError(
                "STRAVA_ACCESS_TOKEN manquant et aucun trio "
                "STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REFRESH_TOKEN pour rafraîchir."
            )

        data = {
            "client_id": cid,
            "client_secret": csec,
            "grant_type": "refresh_token",
            "refresh_token": rtok,
        }
        res = self._client.post(STRAVA_OAUTH_URL, data=data)
        res.raise_for_status()
        payload = res.json()

        self._access_token = payload["access_token"]
        # Strava renvoie un timestamp (epoch seconds)
        self._expires_at = float(payload.get("expires_at", time.time() + 3600.0))

        # Expose aussi dans l'env pour les autres composants du run
        os.environ["STRAVA_ACCESS_TOKEN"] = self._access_token
        os.environ["STRAVA_EXPIRES_AT"] = str(self._expires_at)

    def _auth_headers(self) -> Dict[str, str]:
        if not self._access_token or self._expires_at < time.time() + 60.0:
            self._refresh_access_token_or_raise()
        return {"Authorization": f"Bearer {self._access_token}"}

    # ──────────────────────────────────────────────────────────────────────
    # Client helper
    # ──────────────────────────────────────────────────────────────────────
    def _req(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())
        return self._client.request(method, path, headers=headers, **kwargs)

    # ──────────────────────────────────────────────────────────────────────
    # Read
    # ──────────────────────────────────────────────────────────────────────
    def list_recent(self, page: int = 1) -> List[Dict]:
        """
        Retourne une liste normalisée :
          {id, name, sport_type, description, distance_km, moving_time_min,
           elevation_gain_m, start_dt_utc, start_local}
        """
        r = self._req("GET", "/athlete/activities", params={"page": page, "per_page": self.per_page})
        r.raise_for_status()
        acts = r.json()

        out: List[Dict] = []
        # Par défaut pour Hugo : Réunion
        tz = os.getenv("SPORT_TZ", "Indian/Reunion")
        for a in acts:
            start = p.parse(a["start_date"])  # UTC
            out.append({
                "id": a["id"],
                "name": a.get("name") or "",
                "sport_type": a.get("sport_type") or a.get("type") or "Run",
                "description": a.get("description") or "",
                "distance_km": round(float(a.get("distance", 0.0)) / 1000.0, 2),
                "moving_time_min": int(a.get("moving_time", 0)) // 60,
                "elevation_gain_m": int(a.get("total_elevation_gain", 0) or 0),
                "start_dt_utc": start.to_iso8601_string(),
                "start_local": start.in_timezone(tz).format("YYYY-MM-DD HH:mm"),
            })
        return out

    # ──────────────────────────────────────────────────────────────────────
    # Update (métadonnées)
    # ──────────────────────────────────────────────────────────────────────
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
        Met à jour une activité Strava (métadonnées).
        NOTE: l’API ne permet pas de changer les métriques mesurées (distance, D+, durée).
        Scopes requis côté app Strava : activity:write (+ activity:read pour lecture).
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

        r = self._req("PUT", f"/activities/{activity_id}", data=data)
        r.raise_for_status()
        return r.json()

    # ──────────────────────────────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────────────────────────────
    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass
