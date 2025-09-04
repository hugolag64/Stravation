# stravation/ui/mini_app.py
from __future__ import annotations

import os
import threading
from typing import List, Dict, Optional, Callable

import customtkinter as ctk
import pendulum as p

from stravation.utils.envtools import load_dotenv_if_exists
load_dotenv_if_exists()

# ✅ Imports ABSOLUS depuis le paquet stravation
from stravation.services.strava_service import StravaService
from stravation.services.notion_plans import (
    create_plan, update_plan, find_plans_on_day, page_to_form_defaults,
    find_plans_in_range, page_date_local_iso, ENDURANCE, WOD_ONLY
)
from stravation.services.google_calendar import (
    ensure_calendar, upsert_sport_event, month_shifts
)
from stravation.features.strava_to_notion import sync_strava_to_notion

# ───────────────────────────── Résolution robuste du sync GPX ───────────────
def _resolve_gpx_sync() -> Callable[..., int | None]:
    """Retourne un callable pour synchroniser les routes/GPX → Notion."""
    try:
        import importlib
        mod = importlib.import_module("stravation.features.routes_to_notion")
    except Exception as e:
        raise ImportError(
            "[Sync GPX] Module introuvable: stravation.features.routes_to_notion\n"
            f"Détails: {e}"
        )

    # Inclut les variantes rencontrées
    candidates = [
        "sync_routes_to_notion",
        "sync_strava_routes_to_notion",
        "sync_strava_routes",
        "sync_routes",
        "main",
    ]
    for name in candidates:
        fn = getattr(mod, name, None)
        if callable(fn):
            # On stocke une référence au module sur la fonction pour récupérer le token plus tard
            setattr(fn, "__module_obj__", mod)
            return fn

    available = [k for k, v in vars(mod).items() if callable(v)]
    raise ImportError(
        "[Sync GPX] Aucune fonction de synchro reconnue dans routes_to_notion.\n"
        f"Noms essayés: {', '.join(candidates)}\n"
        f"Fonctions disponibles: {', '.join(available) or 'aucune'}\n"
        "→ Renomme ta fonction en 'sync_routes_to_notion' (recommandé) "
        "ou adapte la liste 'candidates' ci-dessus."
    )

_sync_gpx_callable: Optional[Callable[..., int | None]] = None
def _get_sync_gpx_callable() -> Callable[..., int | None]:
    global _sync_gpx_callable
    if _sync_gpx_callable is None:
        _sync_gpx_callable = _resolve_gpx_sync()
    return _sync_gpx_callable


# ───────────────────────────── Constantes UI / Mapping ──────────────────────
SPORTS_UI = ["Tous", "Course à pied", "Trail", "Vélo", "CrossFit", "Hyrox"]
STRAVA_TO_UI = {
    "Run": "Course à pied",
    "TrailRun": "Trail",
    "Ride": "Vélo",
    "VirtualRide": "Vélo",
    "Workout": "CrossFit",
    "WeightTraining": "CrossFit",
}
UI_TO_STRAVA = {v: k for k, v in STRAVA_TO_UI.items()}
PADDING = 16

# Mode visuel sobre “Apple-like”
ctk.set_default_color_theme("dark-blue")
ctk.set_appearance_mode("dark")


# ───────────────────────────── Helpers format ───────────────────────────────
def _fmt_duration(minutes: float | int) -> str:
    """41 → '41 min' ; 65 → '1 h 05' ; 120 → '2 h' ; valeurs invalides → ''"""
    try:
        m = int(round(float(minutes)))
    except Exception:
        return ""
    if m <= 0:
        return ""
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h" if r == 0 else f"{h} h {r:02d}"

def _fmt_distance_km(val) -> str:
    """Retourne 'X km' (trim fin) ou '' si None/0/invalid."""
    if val is None:
        return ""
    try:
        d = float(val)
    except Exception:
        return ""
    if d <= 0:
        return ""
    s = f"{d:.2f}".rstrip("0").rstrip(".")
    return f"{s} km"

def _fmt_elev_gain(val) -> str:
    """Retourne 'D+ X m' ou '' si None/0/invalid."""
    if val is None:
        return ""
    try:
        e = int(round(float(val)))
    except Exception:
        return ""
    return f"D+ {e} m" if e > 0 else ""

def _tz() -> str:
    return os.getenv("SPORT_TZ", "Indian/Reunion")


# ───────────────────────────── Shifts (planning travail) ────────────────────
SHIFT_COLORS = {
    "A": "#22C55E",   # vert
    "B": "#3B82F6",   # bleu
    "C": "#8B5CF6",   # violet
    "W": "#F59E0B",   # orange
}
def _shift_text_for_day(code: str | None) -> str:
    return (code or "").strip().upper()[:1] if code else ""

def _make_shift_badge(parent, code: str | None):
    txt = _shift_text_for_day(code)
    if not txt:
        return None
    color = SHIFT_COLORS.get(txt, "#64748B")  # fallback gris
    lab = ctk.CTkLabel(
        parent, text=txt, fg_color=color, corner_radius=999,
        text_color="black", width=26, height=18, font=("SF Pro Display", 12, "bold")
    )
    return lab


# ───────────────────────────── Import Strava (onglet) ───────────────────────
class ImportTab(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.page = 1
        self.per_page = 10
        self.svc = StravaService(per_page=self.per_page)
        self.rows: List[Dict] = []
        self.sport_filter = ctk.StringVar(value="Tous")
        self._build()

    def _build(self):
        title = ctk.CTkLabel(self, text="Import Strava (édition live)",
                             font=("SF Pro Display", 18, "bold"))
        title.pack(anchor="w", padx=PADDING, pady=(PADDING, 6))

        bar = ctk.CTkFrame(self)
        bar.pack(fill="x", padx=PADDING)
        ctk.CTkLabel(bar, text="Filtrer par sport").pack(side="left", padx=(0, 8))
        ctk.CTkComboBox(
            bar, values=SPORTS_UI, variable=self.sport_filter,
            command=lambda _=None: self._refresh_list()
        ).pack(side="left")
        ctk.CTkButton(bar, text=f"Charger +{self.per_page}",
                      command=self.load_more).pack(side="left", padx=8)
        self.info_lbl = ctk.CTkLabel(bar, text="0 activité chargée")
        self.info_lbl.pack(side="left", padx=8)

        self.scroll = ctk.CTkScrollableFrame(self, height=480, corner_radius=18)
        self.scroll.pack(fill="both", expand=True, padx=PADDING, pady=(8, PADDING))

    def load_more(self):
        acts = self.svc.list_recent(page=self.page)
        self.rows.extend(acts)
        self.page += 1
        self._refresh_list()
        self.info_lbl.configure(text=f"{len(self.rows)} activité(s) chargée(s)")

    def _refresh_list(self):
        for w in self.scroll.winfo_children():
            w.destroy()
        filt = self.sport_filter.get()
        for a in self.rows:
            sport_ui = STRAVA_TO_UI.get(a.get("sport_type"), "Course à pied")
            if filt != "Tous" and sport_ui != filt:
                continue
            self._card(self.scroll, a, sport_ui)

    def _card(self, parent, act: Dict, sport_ui: str):
        card = ctk.CTkFrame(parent, corner_radius=18)
        card.pack(fill="x", pady=8, padx=2)

        # Titre + date locale
        ctk.CTkLabel(card, text=f"{act.get('name')} — {act.get('start_local')}", anchor="w",
                     font=("SF Pro Display", 14, "bold")) \
            .grid(row=0, column=0, columnspan=7, sticky="w", padx=12, pady=(12, 2))

        # Ligne méta formatée
        distance_txt = _fmt_distance_km(act.get("distance_km"))
        duration_txt = _fmt_duration(act.get("moving_time_min") or 0)
        elev_txt     = _fmt_elev_gain(act.get("elevation_gain_m"))

        parts = [sport_ui]
        if distance_txt: parts.append(distance_txt)
        if duration_txt: parts.append(duration_txt)
        if elev_txt:     parts.append(elev_txt)

        meta = " · ".join(parts)
        ctk.CTkLabel(card, text=meta, text_color="#A0A0A0") \
           .grid(row=1, column=0, columnspan=7, sticky="w", padx=12, pady=(0, 10))

        # Éditables (Strava live: name, sport_type, description)
        ctk.CTkLabel(card, text="Nom").grid(row=2, column=0, sticky="e", padx=8, pady=6)
        v_name = ctk.StringVar(value=act.get("name"))
        ctk.CTkEntry(card, textvariable=v_name, width=320).grid(row=2, column=1, padx=(0, 16))

        ctk.CTkLabel(card, text="Sport").grid(row=2, column=2, sticky="e", padx=8)
        v_sport = ctk.StringVar(value=sport_ui)
        ctk.CTkComboBox(card, values=SPORTS_UI[1:], variable=v_sport, width=160) \
            .grid(row=2, column=3, padx=(0, 16))

        ctk.CTkLabel(card, text="Description").grid(row=2, column=4, sticky="e", padx=8)
        v_desc = ctk.StringVar(value=act.get("description", ""))
        ctk.CTkEntry(card, textvariable=v_desc, width=360, placeholder_text="Optionnel") \
            .grid(row=2, column=5, padx=(0, 12))

        def _save_live():
            # Protection : on intercepte les erreurs HTTP et on affiche un message clair
            try:
                updated = self.svc.update_activity(
                    act["id"],
                    name=v_name.get().strip(),
                    sport_type=UI_TO_STRAVA.get(v_sport.get(), "Run"),
                    description=v_desc.get().strip() or None,
                )
            except Exception as e:
                try:
                    from httpx import HTTPStatusError
                    if isinstance(e, HTTPStatusError):
                        code = e.response.status_code
                        msg = {
                            401: "401 — token/scope Strava",
                            403: "403 — non éditable",
                            404: "404 — introuvable",
                        }.get(code, f"{code} — échec")
                    else:
                        msg = str(e)
                except Exception:
                    msg = str(e)
                btn.configure(text=msg + " ❌")
                self.after(1600, lambda: btn.configure(text="Enregistrer sur Strava"))
                return

            # Reflète localement
            act["name"] = updated.get("name", act["name"])
            act["sport_type"] = updated.get("sport_type", act["sport_type"])
            act["description"] = updated.get("description", act.get("description", ""))
            btn.configure(text="Enregistré ✅")
            self.after(900, lambda: btn.configure(text="Enregistrer sur Strava"))

        btn = ctk.CTkButton(card, text="Enregistrer sur Strava", command=_save_live)
        btn.grid(row=2, column=6, padx=12)

        for i in range(7):
            card.grid_columnconfigure(i, weight=0)
        card.grid_columnconfigure(1, weight=1)
        card.grid_columnconfigure(5, weight=1)


# ───────────────────────────── Calendrier Plans ────────────────────────────
class DayEditor(ctk.CTkFrame):
    """Panneau latéral: créer / modifier une séance pour le jour courant."""
    def __init__(self, master, date_local: p.DateTime):
        super().__init__(master, width=360, corner_radius=16)
        self.date_local = date_local
        self.current_page_id: Optional[str] = None
        self._build()

    def _build(self):
        ctk.CTkLabel(
            self,
            text=self.date_local.format("dddd D MMMM YYYY", locale="fr").capitalize(),
            font=("SF Pro Display", 16, "bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # Liste des séances existantes (si plusieurs)
        self.list_box = ctk.CTkComboBox(self, values=["— Aucune —"], width=320, command=self._load_selected)
        self.list_box.pack(padx=12, pady=(0, 10), anchor="w")

        # Form
        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=10)
        self.f_nom = self._row(form, "Nom", 0, width=300)
        self.f_sport = self._combo(
            form, "Sport", 1, ["Course à pied", "Trail", "Vélo", "CrossFit", "Hyrox"], "Course à pied",
            on_change=self._toggle_fields,
        )
        self.f_types = self._row(form, "Type de séance (csv)", 2, width=300, placeholder="Endurance, EF…")
        self.f_dist = self._row(form, "Distance (km)", 3, width=120)
        self.f_dplus = self._row(form, "D+ (m)", 4, width=120)
        self.f_duree = self._row(form, "Durée (min)", 5, width=120, placeholder="optionnel")
        self.f_notes = self._row(form, "Notes", 6, width=300, placeholder="détails…")

        self.btn = ctk.CTkButton(self, text="Enregistrer", command=self._save)
        self.btn.pack(padx=12, pady=(8, 12), anchor="e")

        self.reload()

    def _row(self, parent, label, r, *, width=220, placeholder=""):
        ctk.CTkLabel(parent, text=label).grid(row=r, column=0, sticky="e", padx=6, pady=6)
        e = ctk.CTkEntry(parent, width=width, placeholder_text=placeholder)
        e.grid(row=r, column=1, padx=(0, 6))
        parent.grid_columnconfigure(1, weight=1)
        return e

    def _combo(self, parent, label, r, values, default, on_change=None):
        ctk.CTkLabel(parent, text=label).grid(row=r, column=0, sticky="e", padx=6, pady=6)
        var = ctk.StringVar(value=default)
        cb = ctk.CTkComboBox(
            parent, values=values, variable=var, width=160,
            command=(lambda _=None: on_change(var.get())) if on_change else None,
        )
        cb.grid(row=r, column=1, padx=(0, 6))
        return cb

    def _toggle_fields(self, sport: str):
        wod = sport in WOD_ONLY
        state = "disabled" if wod else "normal"
        self.f_dist.configure(state=state)
        self.f_dplus.configure(state=state)

    def reload(self):
        # Charge la liste des plans du jour
        pages = find_plans_on_day(self.date_local)
        items = ["— Aucune —"]
        self.page_defaults: Dict[str, Dict] = {}
        for p_ in pages:
            d = page_to_form_defaults(p_)
            items.append(f"{d['title']} ({d['sport']})")
            self.page_defaults[items[-1]] = d
        self.list_box.configure(values=items)
        self.list_box.set(items[0])
        self._fill_new()

    def _fill_new(self):
        # remplit un formulaire vide
        self.current_page_id = None
        self.f_nom.delete(0, "end")
        self.f_nom.insert(0, "Séance")
        self.f_sport.set("Course à pied")
        self._toggle_fields("Course à pied")
        for e in (self.f_types, self.f_dist, self.f_dplus, self.f_duree, self.f_notes):
            e.delete(0, "end")

    def _load_selected(self, label: str):
        if label == "— Aucune —":
            self._fill_new()
            return
        d = self.page_defaults.get(label)
        if not d:
            return
        self.current_page_id = d["page_id"]
        self.f_nom.delete(0, "end")
        self.f_nom.insert(0, d["title"] or "Séance")
        self.f_sport.set(d["sport"] or "Course à pied")
        self._toggle_fields(self.f_sport.get())
        self.f_types.delete(0, "end")
        self.f_types.insert(0, ", ".join(d.get("types", []) or []))
        self.f_dist.delete(0, "end")
        self.f_dist.insert(0, "" if d["distance_km"] is None else str(d["distance_km"]))
        self.f_dplus.delete(0, "end")
        self.f_dplus.insert(0, "" if d["dplus_m"] is None else str(d["dplus_m"]))
        self.f_duree.delete(0, "end")
        self.f_duree.insert(0, "" if d["duree_min"] is None else str(d["duree_min"]))
        self.f_notes.delete(0, "end")
        self.f_notes.insert(0, d.get("notes", ""))

    def _save(self):
        # Données formulaire
        title = self.f_nom.get().strip() or "Séance"
        sport = self.f_sport.get()
        types = [t.strip() for t in self.f_types.get().split(",") if t.strip()]
        notes = self.f_notes.get().strip()
        duree = int(float(self.f_duree.get() or 0)) if self.f_duree.get().strip() else None

        # Heure par défaut pour la séance
        date_iso_for_notion = self.date_local.format("YYYY-MM-DD 17:30")

        # Création/MàJ Notion
        page_id: Optional[str] = None
        if self.current_page_id:
            page_id = self.current_page_id
            if sport in ENDURANCE:
                update_plan(
                    page_id,
                    title=title, date_local_iso=date_iso_for_notion,
                    sport=sport, types=types,
                    distance_km=(float(self.f_dist.get() or 0) if self.f_dist.get().strip() else None),
                    dplus_m=(int(float(self.f_dplus.get() or 0)) if self.f_dplus.get().strip() else None),
                    duree_min=duree, notes=notes,
                )
            else:
                update_plan(
                    page_id,
                    title=title, date_local_iso=date_iso_for_notion,
                    sport=sport, types=types, duree_min=duree, notes=notes,
                )
        else:
            if sport in ENDURANCE:
                page_id = create_plan(
                    title=title, date_local_iso=date_iso_for_notion, sport=sport, types=types,
                    distance_km=(float(self.f_dist.get() or 0) if self.f_dist.get().strip() else None),
                    dplus_m=(int(float(self.f_dplus.get() or 0)) if self.f_dplus.get().strip() else None),
                    duree_min=duree, notes=notes,
                )
            else:
                page_id = create_plan(
                    title=title, date_local_iso=date_iso_for_notion, sport=sport, types=types,
                    duree_min=duree, notes=notes,
                )
            self.current_page_id = page_id

        # Push automatique vers Google Calendar "Sport"
        try:
            sport_cal_summary = os.getenv("SPORT_CALENDAR_SUMMARY", "Sport")
            sport_cal_id = ensure_calendar(sport_cal_summary)

            tz = _tz()
            start_local = p.parse(date_iso_for_notion).in_timezone(tz)
            start_iso = start_local.to_iso8601_string()
            dur = int(float(self.f_duree.get() or 60)) if self.f_duree.get().strip() else 60

            upsert_sport_event(
                calendar_id=sport_cal_id,
                start_iso=start_iso,
                duration_min=dur,
                title=title,
                description=notes,
                external_key=page_id,   # Upsert par Notion page id
                color_id="9",
            )
        except Exception:
            # GCal optionnel → on n'affiche pas d'erreur dans l'UI
            pass

        self.btn.configure(text="Enregistré ✅")
        self.after(900, lambda: self.btn.configure(text="Enregistrer"))
        self.reload()


class PlanTab(ctk.CTkFrame):
    """Calendrier mensuel : pastille Shift (A/B/C/W). Clic sur la case -> éditeur."""
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.ref = p.now()
        self.side: Optional[DayEditor] = None
        self.info_var = ctk.StringVar(value="")
        self._build()

    def _build(self):
        header = ctk.CTkFrame(self)
        header.pack(fill="x", padx=PADDING, pady=(PADDING, 6))

        ctk.CTkButton(header, text="◀", width=40, command=lambda: self._shift(-1)).pack(side="left")
        ctk.CTkButton(header, text="Aujourd’hui", width=120, command=self._today).pack(side="left", padx=6)
        ctk.CTkButton(header, text="▶", width=40, command=lambda: self._shift(1)).pack(side="left")

        self.month_lbl = ctk.CTkLabel(header, text="", font=("SF Pro Display", 18, "bold"))
        self.month_lbl.pack(side="left", padx=10)

        ctk.CTkLabel(header, textvariable=self.info_var, text_color="#A0A0A0").pack(side="right")

        body = ctk.CTkFrame(self, corner_radius=18)
        body.pack(fill="both", expand=True, padx=PADDING, pady=(0, PADDING))
        self.grid = ctk.CTkFrame(body, fg_color="transparent")
        self.grid.pack(fill="both", expand=True)
        self._render_calendar()

    def _today(self):
        self.ref = p.now()
        self._render_calendar()

    def _shift(self, delta_months: int):
        self.ref = self.ref.add(months=delta_months)
        self._render_calendar()

    def _render_calendar(self):
        for w in self.grid.winfo_children():
            w.destroy()

        # Titre
        self.month_lbl.configure(
            text=self.ref.start_of("month").format("MMMM YYYY", locale="fr").capitalize()
        )

        # Intervalle du mois en TZ sport
        tz = _tz()
        start = self.ref.start_of("month").in_timezone(tz)
        end   = self.ref.end_of("month").in_timezone(tz)

        # 1) Séances Notion (groupées par jour)
        try:
            pages = find_plans_in_range(start, end)
        except Exception:
            pages = []
        plans_by_day: Dict[str, List[str]] = {}
        for pg in pages:
            dt_txt = page_date_local_iso(pg)  # "YYYY-MM-DD HH:mm" (local)
            if not dt_txt:
                continue
            day_key = dt_txt[:10]
            title = page_to_form_defaults(pg)["title"] or "Séance"
            plans_by_day.setdefault(day_key, []).append(title)

        # 2) Shifts GCal travail (A/B/C/W)
        work_cal_id = os.getenv("WORK_CALENDAR_ID", "")
        shifts = {}
        gcal_err = ""
        if work_cal_id:
            try:
                shifts = month_shifts(work_cal_id, start.to_iso8601_string(), end.to_iso8601_string())
            except Exception as e:
                gcal_err = str(e).splitlines()[0][:120]
                shifts = {}

        total_sessions = sum(len(v) for v in plans_by_day.values())
        if gcal_err:
            info = f"{total_sessions} séance(s) · TZ {tz} · GCal err: {gcal_err}"
        elif not work_cal_id:
            info = f"{total_sessions} séance(s) · TZ {tz} · GCal off"
        elif not shifts:
            info = f"{total_sessions} séance(s) · TZ {tz} · 0 shifts"
        else:
            info = f"{total_sessions} séance(s) · TZ {tz}"
        self.info_var.set(info)

        # En-têtes jour semaine
        for i, h in enumerate(["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]):
            lab = ctk.CTkLabel(self.grid, text=h, text_color="#A0A0A0", font=("SF Pro Display", 13, "bold"))
            lab.grid(row=0, column=i, padx=6, pady=(6, 10))
            self.grid.grid_columnconfigure(i, weight=1, uniform="day")

        # Cases du mois
        first_day = start.start_of("month")
        first_weekday = (first_day.day_of_week + 1) % 7  # lun=0 … dim=6
        days = self.ref.days_in_month
        r = 1
        c = (first_weekday - 1) % 7

        for d in range(1, days + 1):
            dt = first_day.replace(day=d)
            day_key = dt.format("YYYY-MM-DD")

            box = ctk.CTkFrame(self.grid, corner_radius=14)
            box.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
            self.grid.grid_rowconfigure(r, weight=1)

            # Clic sur la case entière -> éditeur
            def _open(day_dt=dt):
                self._open_editor(day_dt)
            box.bind("<Button-1>", lambda _ev, dd=dt: _open(dd))

            # Ligne entête: numéro + pastille shift
            head = ctk.CTkFrame(box, fg_color="transparent")
            head.pack(fill="x", padx=8, pady=6)

            num = ctk.CTkLabel(head, text=str(d), font=("SF Pro Display", 14, "bold"))
            num.pack(side="left")

            badge = _make_shift_badge(head, shifts.get(day_key))
            if badge:
                badge.pack(side="right")

            # Titres des séances (max 3)
            titles = plans_by_day.get(day_key, [])
            for t in titles[:3]:
                tag = ctk.CTkLabel(
                    box, text="• " + t, anchor="w",
                    fg_color=("gray23"), corner_radius=10, padx=8
                )
                tag.pack(fill="x", padx=8, pady=(0, 4))

            ctk.CTkLabel(box, text=" ").pack(pady=2)

            c += 1
            if c >= 7:
                c = 0
                r += 1

    def _open_editor(self, day: p.DateTime):
        if self.side:
            self.side.destroy()
        self.side = DayEditor(self, day)
        self.side.pack(side="right", fill="y", padx=(8, PADDING), pady=(PADDING, PADDING))


def get_work_shift_for_day(dt: p.DateTime) -> Optional[str]:
    """Ancienne extension (non utilisée désormais)."""
    return None


# ───────────────────────────── App ─────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Stravanotion — Import & Plans")
        self.geometry("1200x760")
        self.minsize(1080, 640)

        # Header (barre d’actions)
        header = ctk.CTkFrame(self)
        header.pack(fill="x", padx=PADDING, pady=(PADDING, 0))

        self.btn_sync_act = ctk.CTkButton(
            header, text="🔄 Sync Activités",
            command=self._sync_activities, height=36, corner_radius=12
        )
        self.btn_sync_act.pack(side="left")

        self.btn_sync_gpx = ctk.CTkButton(
            header, text="🗺️ Sync GPX",
            command=self._sync_gpx, height=36, corner_radius=12
        )
        self.btn_sync_gpx.pack(side="left", padx=8)

        self.status_var = ctk.StringVar(value="Prêt")
        self.status_lbl = ctk.CTkLabel(header, textvariable=self.status_var, text_color="#A0A0A0")
        self.status_lbl.pack(side="right")

        # Tabs
        tabs = ctk.CTkTabview(self)
        tabs.pack(fill="both", expand=True, padx=PADDING, pady=PADDING)
        ImportTab(tabs.add("Import Strava")).pack(fill="both", expand=True)
        PlanTab(tabs.add("Prévisionnel")).pack(fill="both", expand=True)

        # Footer (fine barre d’état)
        footer = ctk.CTkFrame(self, height=28)
        footer.pack(fill="x", padx=PADDING, pady=(0, PADDING))
        ctk.CTkLabel(footer, textvariable=self.status_var, anchor="w").pack(side="left", padx=6)

    # ── Actions header (threadées pour ne pas geler l’UI) ───────────────────
    def _set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def _run_bg(self, fn, on_ok: str, on_err_prefix: str, btn: ctk.CTkButton):
        def job():
            try:
                fn()
                self._set_status(on_ok)
                self.after(0, lambda: btn.configure(text=on_ok + " ✅"))
                self.after(1400, lambda: btn.configure(text=btn._text.split(" ✅")[0]))
            except Exception as e:
                msg = f"{on_err_prefix}: {e}"
                print(msg)
                self._set_status(msg)
                self.after(0, lambda: btn.configure(text="Erreur ❌"))
                self.after(1600, lambda: btn.configure(text=btn._text.split(" ❌")[0]))
        threading.Thread(target=job, daemon=True).start()

    def _sync_activities(self):
        self._set_status("Synchronisation des activités en cours…")
        self.btn_sync_act.configure(text="Sync en cours…")
        self._run_bg(
            sync_strava_to_notion,
            on_ok="Activités synchronisées",
            on_err_prefix="[Sync activités] Erreur",
            btn=self.btn_sync_act,
        )

    def _sync_gpx(self):
        self._set_status("Synchronisation des GPX en cours…")
        self.btn_sync_gpx.configure(text="Sync en cours…")

        def _call():
            fn = _get_sync_gpx_callable()
            # Récupère un token Strava si dispo dans le module
            token = None
            try:
                mod = getattr(fn, "__module_obj__", None)
                get_tok = getattr(mod, "_get_strava_access_token", None) if mod else None
                token = get_tok() if callable(get_tok) else None
            except Exception:
                token = None

            # Essaye plusieurs signatures (avec/without token)
            limit_env = os.getenv("SYNC_GPX_LIMIT")
            limit = int(limit_env) if (limit_env and limit_env.isdigit()) else None

            tried = []
            for kwargs in (
                {"new_only": True, "limit": limit, "token": token},
                {"new_only": True, "limit": limit},
                {"token": token},
                {},
            ):
                try:
                    # supprime les None
                    call_kwargs = {k: v for k, v in kwargs.items() if v is not None}
                    tried.append(str(list(call_kwargs.keys())))
                    return fn(**call_kwargs)
                except TypeError:
                    continue
            # Si on arrive ici, c'est que la signature ne matche aucune variante
            raise TypeError(f"Signature sync GPX incompatible, variantes testées: {', '.join(tried)}")

        self._run_bg(
            _call,
            on_ok="GPX synchronisés",
            on_err_prefix="[Sync GPX] Erreur",
            btn=self.btn_sync_gpx,
        )


def main():
    App().mainloop()


if __name__ == "__main__":
    main()