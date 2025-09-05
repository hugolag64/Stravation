# stravation/ui/mini_app.py
from __future__ import annotations

import os
import threading
from typing import List, Dict, Optional, Callable, Iterable

import customtkinter as ctk
import pendulum as p

from stravation.utils.envtools import load_dotenv_if_exists
load_dotenv_if_exists()

# Services / features
from stravation.services.strava_service import StravaService
from stravation.services.notion_plans import (
    fetch_plan_sessions,
    create_plan,
    update_plan,
    ensure_month_and_duration,
    PlanSession,  # modèle Pydantic
)
from stravation.services.google_calendar import (
    push_sport_event,
    month_shifts,
)
from stravation.features.strava_to_notion import sync_strava_to_notion
from stravation.features.routes_to_notion import sync_routes  # ✅ appel direct pour GPX

# ───────────────────────────── Constantes / Style ─────────────────────────────
PADDING = 16
SPORTS_UI = ["Course à pied", "Trail", "Vélo", "CrossFit", "Hyrox"]
ENDURANCE = {"Course à pied", "Trail", "Vélo"}
WOD_ONLY = {"CrossFit", "Hyrox"}
DEFAULT_TIME_H = 7
DEFAULT_TIME_M = 0

ctk.set_default_color_theme("dark-blue")
ctk.set_appearance_mode("dark")


def _tz() -> str:
    return os.getenv("SPORT_TZ", "Indian/Reunion")


def _fmt_duration(minutes: float | int | None) -> str:
    if minutes is None:
        return ""
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


# ───────────────────────────── Shifts (planning travail) ────────────────────
SHIFT_COLORS = {
    "A": "#22C55E",
    "B": "#3B82F6",
    "C": "#8B5CF6",
    "W": "#F59E0B",
}
def _make_shift_badge(parent, code: str | None):
    if not code:
        return None
    code = code.strip().upper()[:1]
    color = SHIFT_COLORS.get(code, "#64748B")
    return ctk.CTkLabel(
        parent, text=code, fg_color=color, corner_radius=999,
        text_color="black", width=26, height=18, font=("SF Pro Display", 12, "bold")
    )

# ───────────────────────────── Import Strava (onglet) ────────────────────────
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
            bar, values=["Tous"] + SPORTS_UI, variable=self.sport_filter,
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
            st = (a.get("sport_type") or "").lower()
            if   st in {"run", "road_run", "trailrun", "trail_run"}: sport_ui = "Course à pied"
            elif st in {"ride", "virtualride", "virtual_ride"}:      sport_ui = "Vélo"
            elif st in {"workout", "weighttraining"}:                sport_ui = "CrossFit"
            elif st in {"trail"}:                                    sport_ui = "Trail"
            else:                                                    sport_ui = "Course à pied"
            if filt != "Tous" and sport_ui != filt:
                continue
            self._card(self.scroll, a, sport_ui)

    def _card(self, parent, act: Dict, sport_ui: str):
        card = ctk.CTkFrame(parent, corner_radius=18)
        card.pack(fill="x", pady=8, padx=2)

        ctk.CTkLabel(card, text=f"{act.get('name')} — {act.get('start_local')}", anchor="w",
                     font=("SF Pro Display", 14, "bold")) \
            .grid(row=0, column=0, columnspan=5, sticky="w", padx=12, pady=(12, 2))

        meta_parts = [sport_ui]
        if act.get("distance_km"): meta_parts.append(f"{act['distance_km']:.2f} km")
        if act.get("moving_time_min"): meta_parts.append(_fmt_duration(act["moving_time_min"]))
        if act.get("elevation_gain_m"): meta_parts.append(f"D+ {int(act['elevation_gain_m'])} m")
        ctk.CTkLabel(card, text=" · ".join(meta_parts), text_color="#A0A0A0") \
           .grid(row=1, column=0, columnspan=5, sticky="w", padx=12, pady=(0, 10))

        ctk.CTkLabel(card, text="Nom").grid(row=2, column=0, sticky="e", padx=8, pady=6)
        v_name = ctk.StringVar(value=act.get("name") or "")
        ctk.CTkEntry(card, textvariable=v_name, width=320).grid(row=2, column=1, padx=(0, 16))

        ctk.CTkLabel(card, text="Sport").grid(row=2, column=2, sticky="e", padx=8)
        v_sport = ctk.StringVar(value=sport_ui)
        ctk.CTkComboBox(card, values=SPORTS_UI, variable=v_sport, width=160) \
            .grid(row=2, column=3, padx=(0, 16))

        ctk.CTkLabel(card, text="Description").grid(row=2, column=4, sticky="e", padx=8)
        v_desc = ctk.StringVar(value=act.get("description", "") or "")
        e_desc = ctk.CTkEntry(card, textvariable=v_desc, width=360, placeholder_text="Optionnel")
        e_desc.grid(row=2, column=5, padx=(0, 12))

        def _save_live():
            try:
                sport_map = {
                    "Course à pied": "Run",
                    "Trail": "TrailRun",
                    "Vélo": "Ride",
                    "CrossFit": "Workout",
                    "Hyrox": "Workout",
                }
                self.svc.update_activity(
                    act["id"],
                    name=v_name.get().strip() or None,
                    sport_type=sport_map.get(v_sport.get(), "Run"),
                    description=v_desc.get().strip() or None,
                )
                btn.configure(text="Enregistré ✅")
                self.after(900, lambda: btn.configure(text="Enregistrer sur Strava"))
            except Exception as e:
                btn.configure(text=f"Erreur ❌")
                print("[Strava edit] ", e)
                self.after(1600, lambda: btn.configure(text="Enregistrer sur Strava"))

        btn = ctk.CTkButton(card, text="Enregistrer sur Strava", command=_save_live)
        btn.grid(row=2, column=6, padx=12)

        for i in range(7):
            card.grid_columnconfigure(i, weight=0)
        card.grid_columnconfigure(1, weight=1)
        card.grid_columnconfigure(5, weight=1)


# ───────────────────────────── Utilitaires Plans (Notion) ─────────────────────
def _fetch_month_sessions(month_ref: p.DateTime) -> List[PlanSession]:
    sessions = fetch_plan_sessions(after_days=-7, before_days=60)
    return [s for s in sessions if s.date.year == month_ref.year and s.date.month == month_ref.month]


def _sessions_grouped_by_day(sessions: Iterable[PlanSession]) -> Dict[str, List[PlanSession]]:
    out: Dict[str, List[PlanSession]] = {}
    for s in sessions:
        k = p.parse(s.date_iso).to_date_string()
        out.setdefault(k, []).append(s)
    return out


def _sessions_on_day(day_local: p.DateTime) -> List[PlanSession]:
    start = day_local.start_of("day")
    end = day_local.end_of("day")
    sessions = fetch_plan_sessions(after_days=-14, before_days=120)
    res: List[PlanSession] = []
    for s in sessions:
        dt = p.parse(s.date_iso)
        if start <= dt <= end:
            res.append(s)
    return res


def _load_type_options_from_notion() -> List[str]:
    """Lit le schéma Notion pour récupérer la liste Multi-select 'Type de séance'."""
    try:
        from notion_client import Client
    except Exception:
        return []
    token = os.getenv("NOTION_API_KEY")
    dbid = os.getenv("NOTION_DB_PLANNING") or os.getenv("NOTION_DB_PLANS")
    if not token or not dbid:
        return []
    try:
        cli = Client(auth=token)
        schema = cli.databases.retrieve(database_id=dbid)
        prop = schema["properties"].get("Type de séance")
        if not prop or "multi_select" not in prop:
            return []
        return [opt["name"] for opt in prop["multi_select"].get("options", []) if opt.get("name")]
    except Exception as e:
        print("[Notion types] ", e)
        return []


# ───────────────────────────── Fenêtre Modale (TOPLEVEL) ─────────────────────
class EventDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        date_local: p.DateTime,
        type_options: List[str],
        existing: Optional[PlanSession] = None,
        on_saved: Optional[Callable[[], None]] = None,
    ):
        super().__init__(master)
        self.configure(fg_color="#151517")
        self.title("Séance")
        self.date_local = date_local
        self.type_options = type_options
        self.existing = existing
        self.on_saved = on_saved

        # --- Modale + focus robustes ---
        self.withdraw()
        root = master.winfo_toplevel()
        self.transient(root)
        self.resizable(False, False)

        pad = 16
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=pad, pady=pad)

        title = date_local.format("dddd D MMMM YYYY", locale="fr").capitalize()
        ctk.CTkLabel(frame, text=title, font=("SF Pro Display", 18, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))

        ctk.CTkLabel(frame, text="Nom", text_color="#9A9AA2").grid(row=1, column=0, sticky="w")
        self.e_name = ctk.CTkEntry(frame, placeholder_text="Séance")
        self.e_name.grid(row=2, column=0, sticky="ew", pady=(2, 8))

        ctk.CTkLabel(frame, text="Sport", text_color="#9A9AA2").grid(row=3, column=0, sticky="w", pady=(4, 2))
        self.cb_sport = ctk.CTkOptionMenu(frame, values=SPORTS_UI)
        self.cb_sport.set("Course à pied")
        self.cb_sport.grid(row=4, column=0, sticky="ew")

        ctk.CTkLabel(frame, text="Type(s)", text_color="#9A9AA2").grid(row=5, column=0, sticky="w", pady=(10, 2))
        self._type_vars: Dict[str, ctk.BooleanVar] = {}
        types_container = ctk.CTkFrame(frame, fg_color="transparent")
        types_container.grid(row=6, column=0, sticky="ew")
        cols = 3
        for i, opt in enumerate(self.type_options):
            var = ctk.BooleanVar(value=False)
            self._type_vars[opt] = var
            ctk.CTkCheckBox(types_container, text=opt, variable=var)\
                .grid(row=i // cols, column=i % cols, sticky="w", padx=(0, 12), pady=(2, 2))
        for i in range(cols):
            types_container.grid_columnconfigure(i, weight=1)

        ctk.CTkLabel(frame, text="Durée (min)", text_color="#9A9AA2").grid(row=7, column=0, sticky="w", pady=(10, 2))
        self.e_dur = ctk.CTkEntry(frame, placeholder_text="60")
        self.e_dur.grid(row=8, column=0, sticky="w")

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.grid(row=9, column=0, sticky="ew", pady=(12, 0))
        self.btn_cancel = ctk.CTkButton(btns, text="Annuler", fg_color="#2C2C30", hover_color="#232327",
                                        command=self._cancel)
        self.btn_save = ctk.CTkButton(btns, text="Enregistrer", fg_color="#4A90E2", hover_color="#3B78BE",
                                      command=self._save)
        self.btn_cancel.pack(side="right")
        self.btn_save.pack(side="right", padx=(0, 8))

        frame.grid_columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda _e: self._cancel())

        if existing:
            self.e_name.insert(0, existing.name or "Séance")
            self.cb_sport.set(existing.sport or "Course à pied")
            try:
                self.e_dur.insert(0, str(int(existing.duration_min or 60)))
            except Exception:
                self.e_dur.insert(0, "60")
            for t in (existing.types or []):
                if t in self._type_vars:
                    self._type_vars[t].set(True)
        else:
            self.e_name.insert(0, "Séance")
            self.cb_sport.set("Course à pied")
            self.e_dur.insert(0, "60")

        # Armement anti-FocusOut immédiat (si jamais tu ajoutes un bind un jour)
        self._allow_focus_out_close = False
        self.after(200, lambda: setattr(self, "_allow_focus_out_close", True))

        self.after(0, self._center_and_show)

    def _center_and_show(self):
        self.update_idletasks()
        root = self.master.winfo_toplevel()
        rw, rh = root.winfo_width(), root.winfo_height()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        w = max(420, int(rw * 0.34));  h = 430
        x = rx + (rw - w) // 2;  y = ry + (rh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.deiconify()
        self.lift()
        try:
            self.grab_set()          # modal
            self.focus_force()
        except Exception:
            pass

    def _cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _selected_types(self) -> List[str]:
        return [k for k, v in self._type_vars.items() if bool(v.get())]

    def _save(self):
        name = (self.e_name.get() or "").strip() or "Séance"
        sport = self.cb_sport.get()
        try:
            duration = int(self.e_dur.get().strip() or "60")
        except Exception:
            duration = 60
        types = self._selected_types()

        # Assure la présence des vues Notion (mois/durée) si nécessaire
        try:
            ensure_month_and_duration(self.date_local)
        except Exception as e:
            print("[ensure_month_and_duration] ", e)

        try:
            if self.existing and getattr(self.existing, "page_id", None):
                # Mise à jour
                update_plan(
                    page_id=self.existing.page_id,
                    name=name,
                    sport=sport,
                    types=types,
                    duration_min=duration,
                    date_iso=self.date_local.to_date_string(),
                    time_h=DEFAULT_TIME_H,
                    time_m=DEFAULT_TIME_M,
                )
            else:
                # Création
                create_plan(
                    name=name,
                    sport=sport,
                    types=types,
                    duration_min=duration,
                    date_iso=self.date_local.to_date_string(),
                    time_h=DEFAULT_TIME_H,
                    time_m=DEFAULT_TIME_M,
                )
            # Optionnel: push GCal si tu veux immédiatement créer l’évènement
            try:
                push_sport_event(
                    title=name,
                    dt_local=self.date_local.replace(hour=DEFAULT_TIME_H, minute=DEFAULT_TIME_M),
                    duration_min=duration,
                    sport=sport,
                )
            except Exception as eg:
                print("[push_sport_event] ", eg)

            if self.on_saved:
                self.on_saved()
        except Exception as e:
            print("[EventDialog _save] ", e)
        finally:
            self._cancel()


# ───────────────────────────── Calendrier Plans (grille) ─────────────────────
class PlanTab(ctk.CTkFrame):
    """Calendrier mensuel. Double-clic jour → EventDialog (modale)."""
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.ref = p.now()
        self.info_var = ctk.StringVar(value="")
        self._dlg = None  # ref forte pour modale unique
        # Charger une fois les types (sinon on ralentit l’ouverture de la modale)
        self.type_options: List[str] = _load_type_options_from_notion() or [
            "Endurance", "EF", "Seuil", "VMA", "Force", "Plyo", "Côte",
            "Sortie longue", "Rando-trail", "Crossfit", "Hyrox", "Spé", "Sortie vélo"
        ]
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

        self.month_lbl.configure(
            text=self.ref.start_of("month").format("MMMM YYYY", locale="fr").capitalize()
        )

        tz = _tz()
        start = self.ref.start_of("month").in_timezone(tz)
        end   = self.ref.end_of("month").in_timezone(tz)

        monthly_sessions = _fetch_month_sessions(self.ref)
        plans_by_day = _sessions_grouped_by_day(monthly_sessions)

        work_cal_id = os.getenv("WORK_CALENDAR_ID", "")
        shifts = {}
        gcal_err = ""
        if work_cal_id:
            try:
                shifts = month_shifts(work_cal_id, start.to_iso8601_string(), end.to_iso8601_string())
            except Exception as e:
                gcal_err = str(e).splitlines()[0][:120]

        total_sessions = len(monthly_sessions)
        if gcal_err:
            info = f"{total_sessions} séance(s) · TZ {tz} · GCal err: {gcal_err}"
        elif not work_cal_id:
            info = f"{total_sessions} séance(s) · TZ {tz} · GCal off"
        elif not shifts:
            info = f"{total_sessions} séance(s) · TZ {tz} · 0 shifts"
        else:
            info = f"{total_sessions} séance(s) · TZ {tz}"
        self.info_var.set(info)

        for i, h in enumerate(["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]):
            lab = ctk.CTkLabel(self.grid, text=h, text_color="#A0A0A0", font=("SF Pro Display", 13, "bold"))
            lab.grid(row=0, column=i, padx=6, pady=(6, 10))
            self.grid.grid_columnconfigure(i, weight=1, uniform="day")

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

            border = ctk.CTkFrame(box, corner_radius=12, fg_color="#151517")
            border.pack(fill="both", expand=True, padx=1, pady=1)

            # Double-clic = ouvre la modale (dé-bouncé pour éviter FocusOut immédiat)
            border.bind("<Double-Button-1>", lambda _ev, dd=dt: self.after(10, lambda: self._open_dialog(dd)))

            head = ctk.CTkFrame(border, fg_color="transparent")
            head.pack(fill="x", padx=10, pady=6)

            num = ctk.CTkLabel(head, text=str(d), font=("SF Pro Display", 14, "bold"))
            num.pack(side="left")

            badge = _make_shift_badge(head, shifts.get(day_key))
            if badge:
                badge.pack(side="right")

            titles = [s.name for s in plans_by_day.get(day_key, [])][:3]
            for t in titles:
                tag = ctk.CTkLabel(
                    border, text="• " + t, anchor="w",
                    fg_color="gray23", corner_radius=10, padx=8
                )
                tag.pack(fill="x", padx=10, pady=(0, 4))

            c += 1
            if c >= 7:
                c = 0
                r += 1

    # ───────────── Ouverture modale centrée + modale unique ─────────────
    def _open_dialog(self, day: p.DateTime):
        # Empêche d’ouvrir 2 dialogues
        if self._dlg and self._dlg.winfo_exists():
            try:
                self._dlg.focus_set()
            except Exception:
                pass
            return

        # Pré-remplir si une séance existe déjà ce jour-là (on prend la 1ère)
        existing_list = _sessions_on_day(day)
        existing = existing_list[0] if existing_list else None

        self._dlg = EventDialog(
            master=self,
            date_local=day,
            type_options=self.type_options,
            existing=existing,
            on_saved=self._render_calendar,  # rafraîchir après save
        )
        # Bloque le flux tant que la fenêtre est ouverte → évite les fermetures fantômes
        self.wait_window(self._dlg)
        self._dlg = None


# ───────────────────────────── App ─────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Stravanotion — Import & Plans")
        self.geometry("1200x760")
        self.minsize(1080, 640)

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
        ctk.CTkLabel(header, textvariable=self.status_var, text_color="#A0A0A0").pack(side="right")

        tabs = ctk.CTkTabview(self)
        tabs.pack(fill="both", expand=True, padx=PADDING, pady=PADDING)
        ImportTab(tabs.add("Import Strava")).pack(fill="both", expand=True)
        PlanTab(tabs.add("Prévisionnel")).pack(fill="both", expand=True)

        footer = ctk.CTkFrame(self, height=28)
        footer.pack(fill="x", padx=PADDING, pady=(0, PADDING))
        ctk.CTkLabel(footer, textvariable=self.status_var, anchor="w").pack(side="left", padx=6)

    # ── helpers header (threads) ────────────────────────────────────────────
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
            # Incrémental : ajoute seulement nouveaux/édités
            return sync_routes(new_only=True)

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
