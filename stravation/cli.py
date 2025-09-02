# stravation/cli.py
from __future__ import annotations

import typer
from rich.table import Table

from .theme import print
from .log import setup_logging
from .config import MORNING_REMINDER_TIME
from .core.planner import week_plan
from .integrations.gcal_client import (
    get_or_create_calendar_id,
    push_session,
    push_morning_reminder,
)
from .features.strava_to_notion import sync_strava_to_notion
from .features.routes_to_notion import sync_strava_routes_to_notion
from .utils.envtools import write_env_example, check_env
from .storage import db  # NEW: outils cache (SQLite)

app = typer.Typer(help="Stravation — propre, minimal, extensible.")

# ──────────────────────────────────────────────────────────────────────────────
# Init / logging
# ──────────────────────────────────────────────────────────────────────────────
@app.callback()
def _init(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Logs détaillés.")
):
    import logging
    setup_logging(logging.DEBUG if verbose else logging.INFO)

# ──────────────────────────────────────────────────────────────────────────────
# ENV
# ──────────────────────────────────────────────────────────────────────────────
@app.command("env-example")
def env_example(
    force: bool = typer.Option(
        False, "--force", "-f", help="Écrase .env.example s’il existe déjà."
    )
):
    """Génère un fichier .env.example à la racine du projet."""
    path = write_env_example(overwrite=force)
    print(
        f"[ok]Fichier d’exemple généré : [bold]{path}[/] "
        "(duplique-le en .env et remplis les valeurs)."
    )


@app.command("env-check")
def env_check():
    """Vérifie la présence des variables essentielles (Notion/Strava/GCal)."""
    status, errors = check_env()

    table = Table(
        title="Vérification de l'environnement",
        show_header=True,
        header_style="accent",
    )
    table.add_column("Clé")
    table.add_column("OK ?")
    for k in [
        "NOTION_API_KEY",
        "NOTION_DB_ACTIVITIES",
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "STRAVA_REFRESH_TOKEN",
        "GOOGLE_CREDENTIALS_PATH|JSON",
    ]:
        ok = status.get(k, False)
        table.add_row(k, "✅" if ok else "❌")
    print(table)

    if errors:
        print("[err]Variables manquantes :[/]")
        for k, why in errors.items():
            print(f" • [bold]{k}[/] — {why}")
        print(
            "\n[warn]Astuce :[/] lance [bold]stravation env-example[/] puis "
            "copie-le en [.env] et complète les valeurs."
        )
    else:
        print("[ok]Environnement prêt ✔[/]")

# ──────────────────────────────────────────────────────────────────────────────
# Cache (SQLite) — stats & reset
# ──────────────────────────────────────────────────────────────────────────────
@app.command("cache-stats")
def cache_stats():
    """Affiche l’emplacement du cache SQLite et le nombre d’entrées."""
    db.ensure_schema()
    ck, seen = db.counts()
    print(f"[title]Cache local[/]")
    print(f"• Fichier : [bold]{db.db_path()}[/]")
    print(f"• Checkpoints : [bold]{ck}[/]")
    print(f"• Seen activities : [bold]{seen}[/]")


@app.command("reset-cache")
def reset_cache():
    """Vide checkpoints + seen_activities dans le cache SQLite."""
    db.ensure_schema()
    n_ck, n_seen = db.reset_all()
    print(
        f"[ok]Cache réinitialisé[/] — checkpoints supprimés: [bold]{n_ck}[/], "
        f"seen: [bold]{n_seen}[/]"
    )

# ──────────────────────────────────────────────────────────────────────────────
# Prévision → Google Calendar
# ──────────────────────────────────────────────────────────────────────────────
@app.command("plan-to-gcal")
def plan_to_gcal(
    start: str = typer.Argument(..., help="Lundi de la semaine (ex: 2025-09-01)"),
    morning: str = typer.Option(
        MORNING_REMINDER_TIME, "--morning", help="Rappel matin HH:MM"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Affiche sans pousser dans Google Calendar."
    ),
):
    cal_id = get_or_create_calendar_id()
    sessions = week_plan(start)

    table = Table(show_header=True, header_style="accent")
    table.add_column("Jour")
    table.add_column("Sport")
    table.add_column("Titre")
    table.add_column("Heure")
    for s in sessions:
        table.add_row(s.date.format("ddd DD/MM"), s.sport, s.title, s.time_hm)

    print("\n[title]Prévision — Semaine type[/]")
    print(table)

    if dry_run:
        print("[muted]Dry-run — rien envoyé à Google Calendar.[/]")
        return

    pushed = 0
    for s in sessions:
        if s.sport == "repos":
            push_morning_reminder(
                cal_id,
                title_line="Repos actif — mobilité 20’",
                date=s.date,
                morning_hm=morning,
            )
            continue

        push_session(
            cal_id,
            title=s.title,
            date=s.date,
            start_hm=s.time_hm,
            duration_min=s.minutes,
            description=f"Sport: {s.sport}\nDurée: {s.minutes} min",
            reminder_minutes_before=30,
        )
        push_morning_reminder(
            cal_id, title_line=f"{s.title} @{s.time_hm}", date=s.date, morning_hm=morning
        )
        pushed += 1

    print(
        f"[ok]Semaine envoyée sur Google Calendar 'Sport'. "
        f"Séances: {pushed} + rappels matin.[/]"
    )

# ──────────────────────────────────────────────────────────────────────────────
# Strava → Notion (Activités)
# ──────────────────────────────────────────────────────────────────────────────
@app.command("sync-strava-notion")
def cmd_sync_strava_notion(
    full: bool = typer.Option(
        False, "--full", help="Ignore le checkpoint et resynchronise tout l’historique."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Point de départ ISO (ex: 2024-01-01)."
    ),
    no_places: bool = typer.Option(
        False, "--no-places", help="Désactive les relations de lieux (plus rapide/robuste)."
    ),
):
    """
    Importe les activités Strava dans Notion (DB “Activités”).
    - Upsert par “Strava ID”
    - Checkpoint: last_sync_epoch (stocké en local)
    - Idempotent via 'seen_activities'
    """
    created, already = sync_strava_to_notion(full=full, since_iso=since, places=not no_places)
    print(
        f"[ok]Sync terminée[/] — créés/mis à jour: [bold]{created}[/], "
        f"déjà vus: [muted]{already}[/]"
    )

# ──────────────────────────────────────────────────────────────────────────────
# Backfill — depuis une date donnée (option --force)
# ──────────────────────────────────────────────────────────────────────────────
@app.command("backfill")
def cmd_backfill(
    since: str = typer.Argument(..., help="Date de départ YYYY-MM-DD (ex: 2021-01-01)"),
    force: bool = typer.Option(False, "--force", help="Ignore seen_activities pour renvoyer tout."),
    no_places: bool = typer.Option(
        False, "--no-places", help="Désactive les relations de lieux pendant le backfill."
    ),
):
    """
    Ré-importe tout l’historique Strava depuis une date donnée.
    Équivaut à:  stravation sync-strava-notion --full --since <DATE>
    """
    if force:
        db.ensure_schema()
        n = db.clear_seen()
        print(f"[warn]Force ON[/] — seen_activities vidé: {n} lignes.")

    created, already = sync_strava_to_notion(full=True, since_iso=since, places=not no_places)
    print(
        f"[ok]Backfill terminé[/] — créés/mis à jour: [bold]{created}[/], "
        f"déjà vus (skippés): [muted]{already}[/]"
    )

# ──────────────────────────────────────────────────────────────────────────────
# Strava → Notion (Mes itinéraires → 🗺️ Projets GPX)
# ──────────────────────────────────────────────────────────────────────────────
@app.command("sync-strava-routes")
def cmd_sync_strava_routes():
    """
    Importe / met à jour les itinéraires (“Mes itinéraires”) dans la DB 🗺️ Projets GPX.
    - Upsert par “Strava Route ID” si la colonne existe
    - Renseigne le type de sport, la distance, le D+, l’URL GPX, le lien Strava
    - Crée les relations Départ / Arrivée vers la DB “Lieux” quand possible
    """
    created, skipped = sync_strava_routes_to_notion()
    print(
        f"[ok]Sync Routes terminée[/] — créés/mis à jour: [bold]{created}[/], "
        f"déjà vus: [muted]{skipped}[/]"
    )

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app()
