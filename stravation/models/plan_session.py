# stravation/models/plan_session.py
from __future__ import annotations
from pydantic import BaseModel
from typing import List, Optional
import pendulum as p

class PlanSession(BaseModel):
    id: str                       # Notion page id
    name: str
    date_iso: str                 # "YYYY-MM-DDTHH:mm:ss" local
    sport: Optional[str] = None   # Notion select name
    types: List[str] = []         # Notion multi-select names
    duration_min: Optional[int] = None

    @property
    def date(self) -> p.DateTime:
        return p.parse(self.date_iso)

    @property
    def month_key(self) -> str:
        # "YYYY-MM"
        dt = self.date
        return f"{dt.year:04d}-{dt.month:02d}"
