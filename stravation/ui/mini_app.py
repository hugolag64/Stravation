# stravation/ui/mini_app.py
from __future__ import annotations

import os
from typing import List, Dict, Optional

import customtkinter as ctk
import pendulum as p
from stravation.utils.envtools import load_dotenv_if_exists
load_dotenv_if_exists()
# ✅ Imports ABSOLUS depuis le paquet stravation (plus de ModuleNotFoundError)
from stravation.services.strava_service import StravaService
from stravation.services.notion_plans import (
    create_plan, update_plan, find_plans_on_day, page_to_form_defaults,
    find_plans_in_range, page_date_local_iso, ENDURANCE, WOD_ONLY
)
from stravation.services.gcal_service import (
    ensure_calendar, upsert_sport_event, month_shifts
)

# ───────────────────────────── Constantes UI / Mapping ─────────────────────────────
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
PADDING = 14

# Mode visuel sobre “Apple-like”
ctk.set_default_color_theme("dark-blue")
ctk.set_appearance_mode("dark")


# ───────────────────────────── Import Strava ─────────────────────────────
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

        self.scroll = ctk.CTkScrollableFrame(self, height=480)
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
        card = ctk.CTkFrame(parent, corner_radius=16)
        card.pack(fill="x", pady=6)

        # Titre + méta
        ctk.CTkLabel(card, text=f"{act.get('name')} — {act.get('start_local')}", anchor="w") \
            .grid(row=0, column=0, columnspan=6, sticky="w", padx=10, pady=(10, 4))
        ctk.CTkLabel(
            card,
            text=f"{sport_ui} · {act.get('distance_km')} km · {act.get('moving_time_min')} min · D+ {act.get('elevation_gain_m')} m",
            text_color="#A0A0A0",
        ).grid(row=1, column=0, columnspan=6, sticky="w", padx=10, pady=(0, 10))

        # Éditables (Strava live: name, sport_type, description)
        ctk.CTkLabel(card, text="Nom").grid(row=2, column=0, sticky="e", padx=6, pady=6)
        v_name = ctk.StringVar(value=act.get("name"))
        ctk.CTkEntry(card, textvariable=v_name, width=300).grid(row=2, column=1, padx=(0, 16))

        ctk.CTkLabel(card, text="Sport").grid(row=2, column=2, sticky="e", padx=6)
        v_sport = ctk.StringVar(value=sport_ui)
        ctk.CTkComboBox(card, values=SPORTS_UI[1:], variable=v_sport, width=160) \
            .grid(row=2, column=3, padx=(0, 16))

        ctk.CTkLabel(card, text="Description").grid(row=2, column=4, sticky="e", padx=6)
        v_desc = ctk.StringVar(value=act.get("description", ""))
        ctk.CTkEntry(card, textvariable=v_desc, width=320, placeholder_text="Optionnel") \
            .grid(row=2, column=5, padx=(0, 10))

        def _save_live():
            updated = self.svc.update_activity(
                act["id"],
                name=v_name.get().strip(),
                sport_type=UI_TO_STRAVA.get(v_sport.get(), "Run"),
                description=v_desc.get().strip() or None,
            )
            # Reflète localement
            act["name"] = updated.get("name", act["name"])
            act["sport_type"] = updated.get("sport_type", act["sport_type"])
            act["description"] = updated.get("description", act.get("description", ""))
            btn.configure(text="Enregistré ✅")
            self.after(900, lambda: btn.configure(text="Enregistrer sur Strava"))

        btn = ctk.CTkButton(card, text="Enregistrer sur Strava", command=_save_live)
        btn.grid(row=2, column=6, padx=10)

        for i in range(7):
            card.grid_columnconfigure(i, weight=0)
        card.grid_columnconfigure(1, weight=1)
        card.grid_columnconfigure(5, weight=1)


# ───────────────────────────── Calendrier Plans ────────────────────────────
class DayEditor(ctk.CTkFrame):
    """
    Panneau latéral: créer / modifier une séance pour le jour courant.
    """
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

        # Heure par défaut pour la séance (modifiable si tu veux l'exposer)
        # Ici 17:30 locale.
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

            # Respecte la timezone locale (par défaut Réunion)
            tz = os.getenv("SPORT_TZ", "Indian/Reunion")
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
        except Exception as e:
            # UI sobre : on loggue en console, pas de traceback dans l’UI
            print("[GCal] Erreur push:", e)

        self.btn.configure(text="Enregistré ✅")
        self.after(900, lambda: self.btn.configure(text="Enregistrer"))
        self.reload()


class PlanTab(ctk.CTkFrame):
    """
    Vue calendrier par défaut: clique sur un jour → éditeur latéral.
    """
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.ref = p.now()
        self.side: Optional[DayEditor] = None
        self._build()

    def _build(self):
        header = ctk.CTkFrame(self)
        header.pack(fill="x", padx=PADDING, pady=(PADDING, 6))
        ctk.CTkButton(header, text="◀", width=40, command=lambda: self._shift(-1)).pack(side="left")
        self.month_lbl = ctk.CTkLabel(header, text="", font=("SF Pro Display", 18, "bold"))
        self.month_lbl.pack(side="left", padx=8)
        ctk.CTkButton(header, text="▶", width=40, command=lambda: self._shift(1)).pack(side="left")
        ctk.CTkLabel(header, text="(Clique un jour pour créer/modifier)").pack(side="left", padx=10)

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=PADDING, pady=(0, PADDING))
        self.grid = ctk.CTkFrame(body)
        self.grid.pack(side="left", fill="both", expand=True)
        self._render_calendar()

    def _shift(self, delta_months: int):
        self.ref = self.ref.add(months=delta_months)
        self._render_calendar()

    def _render_calendar(self):
        for w in self.grid.winfo_children():
            w.destroy()
        self.month_lbl.configure(text=self.ref.start_of("month").format("MMMM YYYY", locale="fr").capitalize())

        # Pré-charge: séances Notion du mois + planning de taf GCal
        start = self.ref.start_of("month")
        end = self.ref.end_of("month")

        # Séances du mois (groupées par jour)
        plans = find_plans_in_range(start, end)
        plans_by_day: Dict[str, List[str]] = {}
        for pg in plans:
            dt_txt = page_date_local_iso(pg)  # "YYYY-MM-DD HH:mm"
            if not dt_txt:
                continue
            day_key = dt_txt[:10]
            title = page_to_form_defaults(pg)["title"] or "Séance"
            plans_by_day.setdefault(day_key, []).append(title)

        # Planning de taf (GCal Work)
        work_cal_id = os.getenv("WORK_CALENDAR_ID", "")
        shifts = {}
        if work_cal_id:
            try:
                shifts = month_shifts(work_cal_id, start.to_iso8601_string(), end.to_iso8601_string())
            except Exception as e:
                print("[GCal] Erreur chargement planning:", e)

        # en-têtes
        for i, h in enumerate(["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]):
            ctk.CTkLabel(self.grid, text=h, text_color="#A0A0A0").grid(row=0, column=i, padx=6, pady=6)
            self.grid.grid_columnconfigure(i, weight=1)

        first_weekday = (start.day_of_week + 1) % 7  # lun=0 … dim=6
        days = self.ref.days_in_month
        r = 1
        c = (first_weekday - 1) % 7
        for d in range(1, days + 1):
            dt = start.replace(day=d)
            day_key = dt.format("YYYY-MM-DD")

            box = ctk.CTkFrame(self.grid, corner_radius=12)
            box.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")

            # En-tête (numéro + shift)
            head_text = f"{d}"
            if shifts.get(day_key):
                head_text += f"  {shifts[day_key]}"
            ctk.CTkButton(box, text=head_text, width=80,
                          command=lambda day_dt=dt: self._open_editor(day_dt)) \
                .pack(anchor="ne", padx=6, pady=6)

            # Titres des séances (badges)
            for t in plans_by_day.get(day_key, [])[:3]:  # limite visuelle
                ctk.CTkLabel(box, text="• " + t, anchor="w").pack(fill="x", padx=8, pady=(0, 4))

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
    """
    Ancienne extension (non utilisée désormais car on lit GCal au niveau du mois).
    On la conserve pour compat si tu l'appelles ailleurs.
    """
    return None


# ───────────────────────────── App ─────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Stravanotion — Import & Plans")
        self.geometry("1200x720")
        self.minsize(1080, 640)
        tabs = ctk.CTkTabview(self)
        tabs.pack(fill="both", expand=True, padx=PADDING, pady=PADDING)
        ImportTab(tabs.add("Import Strava")).pack(fill="both", expand=True)
        PlanTab(tabs.add("Prévisionnel")).pack(fill="both", expand=True)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
