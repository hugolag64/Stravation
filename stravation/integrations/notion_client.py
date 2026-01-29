from __future__ import annotations
from typing import Dict, Any, Optional
from notion_client import Client
from ..config import NOTION_API_KEY, NOTION_DB_ACTIVITIES, NOTION_DB_PLANNING

_client: Client | None = None

def notion() -> Client:
    global _client
    if _client is None:
        if not NOTION_API_KEY:
            raise RuntimeError("NOTION_API_KEY manquant.")
        _client = Client(auth=NOTION_API_KEY)
    return _client

# ----- ACTIVITÉS (Strava → Notion) -------------------------------------------

def _find_activity_page_by_strava_id(db_id: str, strava_id: int | str) -> Optional[str]:
    """
    Cherche une page par 'Strava ID' (Number). Retourne page_id ou None.
    """
    resp = notion().databases.query(
        **{
            "database_id": db_id,
            "filter": {
                "property": "Strava ID",
                "number": {"equals": int(strava_id)}
            },
            "page_size": 1,
        }
    )
    results = resp.get("results", [])
    if results:
        return results[0]["id"]
    return None

def upsert_activity(properties: Dict[str, Any]) -> str:
    """
    Upsert par 'Strava ID' (Number). 'properties' doit être un dict pour 'properties' Notion.
    """
    if not NOTION_DB_ACTIVITIES:
        raise RuntimeError("NOTION_DB_ACTIVITIES non défini.")
    sid = properties.get("Strava ID", {}).get("number")
    if sid is None:
        raise ValueError("Propriété 'Strava ID' (number) manquante pour l'upsert.")

    page_id = _find_activity_page_by_strava_id(NOTION_DB_ACTIVITIES, sid)
    if page_id:
        updated = notion().pages.update(page_id=page_id, properties=properties)
        return updated["id"]
    else:
        created = notion().pages.create(
            **{
                "parent": {"type": "database_id", "database_id": NOTION_DB_ACTIVITIES},
                "properties": properties,
            }
        )
        return created["id"]

# ----- PLANNING (placeholder) ------------------------------------------------

def upsert_planning(properties: Dict[str, Any]) -> str:
    if not NOTION_DB_PLANNING:
        raise RuntimeError("NOTION_DB_PLANNING non défini.")
    created = notion().pages.create(
        **{
            "parent": {"type": "database_id", "database_id": NOTION_DB_PLANNING},
            "properties": properties,
        }
    )
    return created["id"]
