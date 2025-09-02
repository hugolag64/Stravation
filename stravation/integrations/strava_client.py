from __future__ import annotations
import httpx, time
from typing import Iterator, Dict, Any, Optional
from ..config import STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN, RATE_SAFETY

BASE = "https://www.strava.com/api/v3"

def _access_token() -> str:
    """Échange refresh_token → access_token (flux simplifié)."""
    if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN):
        raise RuntimeError("Variables STRAVA_* manquantes (client_id/secret/refresh_token).")
    url = "https://www.strava.com/oauth/token"
    data = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": STRAVA_REFRESH_TOKEN,
    }
    r = httpx.post(url, data=data, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

def list_activities(per_page: int = 100, *, after_epoch: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """
    Itère les activités par pages, optionnellement filtrées par 'after' (epoch, secondes).
    """
    token = _access_token()
    page = 1
    while True:
        params = {"page": page, "per_page": per_page}
        if after_epoch:
            params["after"] = after_epoch
        r = httpx.get(f"{BASE}/athlete/activities", params=params,
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        for it in items:
            yield it
        page += 1
        time.sleep(1.0 + RATE_SAFETY)
