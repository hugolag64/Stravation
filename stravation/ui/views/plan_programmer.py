# stravation/ui/views/plan_programmer.py
from __future__ import annotations
import pendulum as p
import customtkinter as ctk
from typing import List
from stravation.services.notion_plans import fetch_plan_sessions, ensure_month_and_duration
from stravation.services.google_calendar import push_sport_event
from stravation.models.plan_session import PlanSession

APPLE_BG = "#0B0B0C"
APPLE_CARD = "#151517"
APPLE_TEXT = "#EDEDED"
APPLE_MUTED = "#9A9AA2"
APPLE_ACCENT = "#4A90E2"

class PlanProgrammerView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=APPLE_BG, **kwargs)
        self.listbox = ctk.CTkScrollableFrame(self, fg_color=APPLE_BG)
        self.listbox.pack(side="left", fill="both", expand=True, padx=12, pady=12)

        self.detail = ctk.CTkFrame(self, fg_color=APPLE_CARD, corner_radius=16)
        self.detail.pack(side="right", fill="both", expand=True, padx=12, pady=12)

        self._build_detail()
        self._load()

    def _build_detail(self):
        self.title = ctk.CTkLabel(self.detail, text="Séance", font=("SF Pro Display", 18, "bold"), text_color=APPLE_TEXT)
        self.title.pack(anchor="w", padx=16, pady=(16, 8))

        self.meta = ctk.CTkLabel(self.detail, text="", font=("SF Pro Text", 13), text_color=APPLE_MUTED, justify="left")
        self.meta.pack(anchor="w", padx=16)

        self.btn = ctk.CTkButton(self.detail, text="Programmer dans Google Calendar", command=self._push, fg_color=APPLE_ACCENT, hover_color="#2F6EBE")
        self.btn.pack(anchor="w", padx=16, pady=16)

        self.current: PlanSession | None = None

    def _load(self):
        sessions = fetch_plan_sessions(after_days=-1, before_days=30)
        for s in sessions:
            frame = ctk.CTkFrame(self.listbox, fg_color=APPLE_CARD, corner_radius=12)
            frame.pack(fill="x", padx=6, pady=6)

            txt = f"{s.date.format('ddd D MMM HH:mm', locale='fr')} • {s.name}"
            if s.sport: txt += f"  [{s.sport}]"
            lab = ctk.CTkLabel(frame, text=txt, font=("SF Pro Text", 13), text_color=APPLE_TEXT)
            lab.pack(anchor="w", padx=12, pady=10)

            def _select(sess=s):
                self.current = sess
                types = ", ".join(sess.types) if sess.types else "—"
                dur = f"{sess.duration_min} min" if sess.duration_min else "—"
                meta = f"Date prévue : {sess.date.format('DD/MM/YYYY HH:mm')}\nSport : {sess.sport or '—'}\nType : {types}\nDurée prévue : {dur}\nMois : {sess.month_key}"
                self.title.configure(text=sess.name)
                self.meta.configure(text=meta)

            frame.bind("<Button-1>", lambda _e, cb=_select: cb())
            lab.bind("<Button-1>", lambda _e, cb=_select: cb())

    def _push(self):
        if not self.current:
            return
        s = self.current
        start_local = s.date.in_timezone(p.local_timezone())
        duration = s.duration_min or 60
        push_sport_event(
            summary=s.name,
            start_local=start_local,
            duration_min=duration,
            sport=s.sport,
            types=s.types
        )
        ensure_month_and_duration(page_id=s.id, month_key=s.month_key, duration_min=duration)
        self.btn.configure(text="✅ Programmée !", state="disabled")
