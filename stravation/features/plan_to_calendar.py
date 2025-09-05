# stravation/features/plan_to_calendar.py
from __future__ import annotations
import pendulum as p
from typing import Optional
from stravation.services.notion_plans import fetch_plan_sessions, ensure_month_and_duration
from stravation.services.google_calendar import push_sport_event

DEFAULT_DURATION = 60  # minutes si rien en Notion

def push_plans_window(after_days: int = -1, before_days: int = 30) -> int:
    """
    Exporte les séances Notion Plan vers GCal dans une fenêtre glissante.
    Retourne le nombre d'events créés.
    """
    sessions = fetch_plan_sessions(after_days=after_days, before_days=before_days)
    count = 0
    for s in sessions:
        start_local = s.date.in_timezone(p.local_timezone())  # cohérent avec Réunion
        duration = s.duration_min or DEFAULT_DURATION

        # Envoi vers GCal
        push_sport_event(
            summary=s.name,
            start_local=start_local,
            duration_min=duration,
            sport=s.sport,
            types=s.types,
        )
        # Mise à jour Notion : Mois + Durée (si manquante)
        ensure_month_and_duration(page_id=s.id, month_key=s.month_key, duration_min=s.duration_min or duration)
        count += 1
    return count
