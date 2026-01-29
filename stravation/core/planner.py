from __future__ import annotations
from typing import List
import pendulum as p
from .models import Session
from ..config import DEFAULT_SESSION_TIME, MORNING_REMINDER_TIME

def week_plan(start_monday: str,
              *,
              crossfit_time_am: str = "06:00",
              crossfit_time_pm: str = "17:30") -> List[Session]:
    d0 = p.parse(start_monday).in_timezone("Europe/Paris")
    if d0.weekday() != 0:
        d0 = d0.start_of("week")  # lundi

    def S(d, sport, title, minutes, time_hm):
        return Session(date=d, sport=sport, title=title,
                       minutes=minutes, time_hm=time_hm, morning_hm=MORNING_REMINDER_TIME)

    days: List[Session] = []
    days.append(S(d0, "crossfit", "CrossFit – Force", 60, crossfit_time_pm))
    days.append(S(d0.add(days=1), "course", "Course – Endurance 45’", 45, "18:00"))
    days.append(S(d0.add(days=2), "crossfit", "CrossFit – Métabo", 60, crossfit_time_am))
    days.append(S(d0.add(days=3), "course", "Course – VMA courtes (10×200m)", 50, "18:00"))
    days.append(S(d0.add(days=4), "crossfit", "CrossFit – Gym + Cardio", 60, crossfit_time_pm))
    days.append(S(d0.add(days=5), "repos", "Repos actif (mobilité 20’)", 20, "10:00"))
    days.append(S(d0.add(days=6), "course", "Course – Sortie longue 75’", 75, "09:00"))
    return days
