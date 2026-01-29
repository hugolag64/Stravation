# stravation/features/calendar_sync.py
from __future__ import annotations
import pendulum as p
from typing import List, Dict
from stravation.services.notion_plans import fetch_plan_sessions
from stravation.services.google_calendar import (
    list_events,
    delete_event,
    push_sport_event,
)
import os

DEFAULT_TIME_H = 7
DEFAULT_TIME_M = 0

def sync_gcal_with_notion(after_days: int = -1, before_days: int = 180) -> Dict[str, int]:
    """
    Synchronise complètement GCal à partir de Notion.
    - crée les séances manquantes
    - met à jour celles existantes
    - supprime dans GCal celles qui n'existent plus dans Notion

    Retourne un dict avec {"created": X, "updated": Y, "deleted": Z}
    """
    calendar_hint = os.getenv("SPORT_CALENDAR_ID")
    if not calendar_hint:
        raise RuntimeError("SPORT_CALENDAR_ID manquant")

    # 1) Charger toutes les séances Notion dans la fenêtre
    sessions = fetch_plan_sessions(after_days=after_days, before_days=before_days)
    sessions = [s for s in sessions if s.date]

    notion_by_id = {s.page_id: s for s in sessions if s.page_id}

    # 2) Charger tous les events GCal dans la même fenêtre
    start = p.now().add(days=after_days).to_iso8601_string()
    end   = p.now().add(days=before_days).to_iso8601_string()
    gcal_events = list_events(calendar_hint, start, end)

    gcal_by_id = {
        e.get("extendedProperties", {}).get("private", {}).get("notion_page_id"): e
        for e in gcal_events
        if e.get("extendedProperties", {}).get("private", {}).get("notion_page_id")
    }

    stats = {"created": 0, "updated": 0, "deleted": 0}

    # 3) Supprimer dans GCal ce qui n'est plus dans Notion
    for ext_id, evt in list(gcal_by_id.items()):
        if ext_id not in notion_by_id:
            delete_event(calendar_hint, evt["id"])
            stats["deleted"] += 1

    # 4) Créer / mettre à jour les séances Notion → GCal
    for page_id, s in notion_by_id.items():
        start_dt = s.date.replace(hour=DEFAULT_TIME_H, minute=DEFAULT_TIME_M)

        created_event = push_sport_event(
            summary=s.title or "Séance",
            start_local=start_dt,
            duration_min=s.duration_min or 60,
            sport=s.sport,
            types=s.types,
            calendar_hint=calendar_hint,
            external_key=page_id,   # clé unique Notion => upsert
            skip_if_exists=False,
        )

        # si l'event existait déjà → update
        if page_id in gcal_by_id:
            stats["updated"] += 1
        else:
            stats["created"] += 1

    return stats
