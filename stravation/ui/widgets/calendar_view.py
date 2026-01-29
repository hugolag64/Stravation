# stravation/ui/widgets/calendar_view.py
from __future__ import annotations
import calendar
import pendulum as p
import tkinter as tk
import customtkinter as ctk
from typing import Callable, Optional, Dict

# ───────────────────────── Style "Apple-like" ─────────────────────────
APPLE_BG = "#0B0B0C"
APPLE_CARD = "#151517"
APPLE_STROKE = "#2A2A2E"
APPLE_TEXT = "#EDEDED"
APPLE_MUTED = "#9A9AA2"
APPLE_ACCENT = "#4A90E2"

# ───────────────────────── Fenêtre modale ─────────────────────────
class EventDialog(ctk.CTkToplevel):
    """
    Fenêtre modale centrée pour créer/éditer une séance.
    Appelle on_submit(payload: dict) au clic sur Enregistrer.
    AUCUN appel réseau ici -> ouverture instantanée.
    """
    def __init__(self, master, date_obj: p.Date, on_submit: Optional[Callable[[dict], None]] = None):
        super().__init__(master)
        self.configure(fg_color=APPLE_CARD)
        self.title("Séance")
        self.on_submit = on_submit
        self.date = date_obj

        # Modale + focus
        self.withdraw()              # évite le flash avant centrage
        self.transient(self.winfo_toplevel())
        self.grab_set()
        self.resizable(False, False)

        pad = 16
        frm = ctk.CTkFrame(self, fg_color="transparent")
        frm.pack(fill="both", expand=True, padx=pad, pady=pad)

        # Titre
        title = date_obj.format("dddd D MMMM YYYY", locale="fr").capitalize()
        ctk.CTkLabel(frm, text=title, text_color=APPLE_TEXT, font=("SF Pro Display", 18, "bold"))\
            .grid(row=0, column=0, sticky="w", pady=(0, 8))

        # Nom
        self._label(frm, "Nom", row=1)
        self.entry_name = ctk.CTkEntry(frm, placeholder_text="Séance")
        self.entry_name.grid(row=2, column=0, sticky="ew")

        # Sport
        self._label(frm, "Sport", row=3)
        self.sport_opt = ctk.CTkOptionMenu(frm,
            values=["Course à pied", "Trail", "Vélo", "CrossFit", "Hyrox"],
            fg_color="#242427", button_color="#242427", button_hover_color="#1E1E21"
        )
        self.sport_opt.set("Course à pied")
        self.sport_opt.grid(row=4, column=0, sticky="ew")

        # Types
        self._label(frm, "Type(s)", row=5)
        row_types = ctk.CTkFrame(frm, fg_color="transparent")
        row_types.grid(row=6, column=0, sticky="ew")
        self.chk_velo = ctk.CTkCheckBox(row_types, text="Sortie vélo")
        self.chk_long = ctk.CTkCheckBox(row_types, text="Sortie longue")
        self.chk_foot = ctk.CTkCheckBox(row_types, text="Footing")
        self.chk_pma  = ctk.CTkCheckBox(row_types, text="PMA")
        for i, w in enumerate([self.chk_velo, self.chk_long, self.chk_foot, self.chk_pma]):
            w.grid(row=0, column=i, padx=(0, 12), pady=(2, 0))

        # Durée
        self._label(frm, "Durée (min)", row=7)
        self.entry_dur = ctk.CTkEntry(frm, placeholder_text="60")
        self.entry_dur.grid(row=8, column=0, sticky="ew")

        # Boutons
        btns = ctk.CTkFrame(frm, fg_color="transparent")
        btns.grid(row=9, column=0, sticky="ew", pady=(12, 0))
        ctk.CTkButton(btns, text="Annuler", fg_color="#2C2C30", hover_color="#232327",
                      command=self._cancel).pack(side="right")
        ctk.CTkButton(btns, text="Enregistrer", fg_color=APPLE_ACCENT, hover_color="#3B78BE",
                      command=self._save).pack(side="right", padx=(0, 8))

        frm.grid_columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda _e: self._cancel())

        # Centre parfaitement sur le parent
        self.after(0, self._center_and_show)

    def _label(self, parent, text: str, row: int):
        ctk.CTkLabel(parent, text=text, text_color=APPLE_MUTED, anchor="w",
                     font=("SF Pro Text", 12, "bold")).grid(row=row, column=0, sticky="ew", pady=(10, 2))

    def _center_and_show(self):
        self.update_idletasks()
        root = self.winfo_toplevel()
        rw, rh = root.winfo_width(), root.winfo_height()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()

        w = max(420, int(rw * 0.34))
        h = 430
        x = rx + (rw - w) // 2
        y = ry + (rh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.focus_force()

    def _save(self):
        text_dur = (self.entry_dur.get() or "0").strip()
        duration = int(text_dur) if text_dur.isdigit() else 0
        payload = {
            "date": self.date.to_date_string(),
            "name": self.entry_name.get().strip() or "Séance",
            "sport": self.sport_opt.get(),
            "types": {
                "sortie_velo": bool(self.chk_velo.get()),
                "sortie_longue": bool(self.chk_long.get()),
                "footing": bool(self.chk_foot.get()),
                "pma": bool(self.chk_pma.get()),
            },
            "duration_min": duration,
        }
        if self.on_submit:
            self.on_submit(payload)
        self.destroy()

    def _cancel(self):
        self.destroy()


# ───────────────────────── Grille mensuelle performante ─────────────────────────
class MonthCalendar(ctk.CTkFrame):
    """
    Canvas mensuel (traits nets). Double-clic sur un jour -> EventDialog.
    """
    def __init__(self, master, year: int, month: int,
                 on_day_dblclick: Optional[Callable[[p.Date], None]] = None,
                 events_count: Optional[Dict[str, int]] = None, **kwargs):
        super().__init__(master, fg_color=APPLE_BG, **kwargs)
        self.year = year
        self.month = month
        self.on_day_dblclick = on_day_dblclick
        self.events_count = events_count or {}
        self._build()

    def _build(self):
        cal = calendar.Calendar(firstweekday=0)  # Lundi
        weeks = cal.monthdatescalendar(self.year, self.month)

        # En-tête
        header = ctk.CTkFrame(self, fg_color=APPLE_BG)
        header.pack(fill="x", padx=12, pady=(8, 0))
        month_name = p.date(self.year, self.month, 1).format("MMMM YYYY", locale="fr")
        ctk.CTkLabel(header, text=month_name.capitalize(),
                     font=("SF Pro Display", 20, "bold"), text_color=APPLE_TEXT).pack(side="left")

        # Noms jours
        days = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        head = ctk.CTkFrame(self, fg_color=APPLE_BG)
        head.pack(fill="x", padx=12, pady=(8, 4))
        for i, d in enumerate(days):
            ctk.CTkLabel(head, text=d, font=("SF Pro Text", 12, "bold"),
                         text_color=APPLE_MUTED).grid(row=0, column=i, sticky="nsew", padx=(2, 2))
            head.grid_columnconfigure(i, weight=1)

        # Grille
        grid_container = ctk.CTkFrame(self, fg_color=APPLE_BG)
        grid_container.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        rows = len(weeks); cols = 7
        cell_h = 92; cell_w = 128

        canvas = tk.Canvas(grid_container, bg=APPLE_BG, highlightthickness=0, bd=0, relief="flat",
                           width=cols * cell_w, height=rows * cell_h)
        canvas.pack()

        # Délimitations
        for c in range(cols + 1):
            x = c * cell_w
            canvas.create_line(x, 0, x, rows * cell_h, fill=APPLE_STROKE, width=1)
        for r in range(rows + 1):
            y = r * cell_h
            canvas.create_line(0, y, cols * cell_w, y, fill=APPLE_STROKE, width=1)

        today = p.now().date()

        # Cases
        for r, week in enumerate(weeks):
            for c, day in enumerate(week):
                x0, y0 = c * cell_w, r * cell_h
                x1, y1 = x0 + cell_w, y0 + cell_h

                in_month = (day.month == self.month)
                date_str = p.date(day.year, day.month, day.day).to_date_string()
                ev_count = self.events_count.get(date_str, 0)

                # Numéro + today
                if day == today:
                    canvas.create_oval(x0 + 8, y0 + 6, x0 + 34, y0 + 32, fill=APPLE_ACCENT, outline="")
                    canvas.create_text(x0 + 21, y0 + 19, text=str(day.day),
                                       fill="white", font=("SF Pro Text", 12, "bold"))
                else:
                    canvas.create_text(x0 + 16, y0 + 18, text=str(day.day),
                                       fill=APPLE_TEXT if in_month else APPLE_MUTED,
                                       font=("SF Pro Text", 12, "bold"))

                # Badge évènements
                if ev_count > 0:
                    bx, by = x1 - 28, y1 - 22
                    canvas.create_rectangle(bx, by, bx + 20, by + 16, fill=APPLE_CARD, outline=APPLE_STROKE)
                    canvas.create_text(bx + 10, by + 8, text=str(ev_count), fill=APPLE_TEXT, font=("SF Pro Text", 10))

                # Zone interactive (double-clic)
                zone = canvas.create_rectangle(x0, y0, x1, y1, outline="", fill="")
                def _dbl(d):
                    return lambda _e: self.on_day_dblclick(d) if self.on_day_dblclick else None
                canvas.tag_bind(zone, "<Double-Button-1>", _dbl(day))

                # Hover léger
                hover_rect = canvas.create_rectangle(x0 + 1, y0 + 1, x1 - 1, y1 - 1, outline="", fill="", width=0)
                def _h_in(_e, rect=hover_rect):  canvas.itemconfig(rect, fill="#FFFFFF10")
                def _h_out(_e, rect=hover_rect): canvas.itemconfig(rect, fill="")
                canvas.tag_bind(hover_rect, "<Enter>", _h_in)
                canvas.tag_bind(hover_rect, "<Leave>", _h_out)


# ───────────────────────── Vue intégrée ─────────────────────────
class CalendarView(ctk.CTkFrame):
    """
    Vue : calendrier + ouverture modale au double-clic.
    Passer on_submit pour brancher la sauvegarde (Notion/GCal/Strava).
    """
    def __init__(self, master, year: int, month: int,
                 events_count: Optional[Dict[str, int]] = None,
                 on_submit: Optional[Callable[[dict], None]] = None, **kwargs):
        super().__init__(master, fg_color=APPLE_BG, **kwargs)
        self.on_submit = on_submit
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.calendar = MonthCalendar(
            self, year=year, month=month,
            on_day_dblclick=self._open_dialog, events_count=events_count
        )
        self.calendar.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

    def _open_dialog(self, date_py: p.Date):
        date_p = p.date(date_py.year, date_py.month, date_py.day)
        EventDialog(self, date_p, on_submit=self.on_submit or self._default_submit)

    def _default_submit(self, payload: dict):
        print("[CalendarView] submit:", payload)


# ───────────────────────── Démo locale ─────────────────────────
if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    app = ctk.CTk()
    app.title("Stravation — Calendrier (démo)")
    today = p.now()
    demo = CalendarView(app, year=today.year, month=today.month, events_count={today.to_date_string(): 2})
    demo.pack(fill="both", expand=True)
    app.geometry("1180x720")
    app.mainloop()
