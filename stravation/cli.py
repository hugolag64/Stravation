# stravation/cli.py
from __future__ import annotations

import os
import typer
from rich.table import Table
from notion_client import Client as Notion

from .theme import print
from .log import setup_logging
from .config import MORNING_REMINDER_TIME
from .core.planner import week_plan
from .integrations.gcal_client import (   # conserve les commandes existantes
    get_or_create_calendar_id,
    push_session,
    push_morning_reminder,
)
from .features.strava_to_notion import sync_strava_to_notion
from .features.routes_to_notion import (
    sync_strava_routes_to_notion,
    _iter_strava_routes,             # pour routes-count
    list_notion_routes_index,        # ➜ nouvel index Notion
)
from .features.plan_to_calendar import push_plans_window  # ⬅️ NOUVEAU
from .utils.envtools import write_env_example, check_env
from .storage import db  # outils cache (SQLite)

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
# Prévision (semaine type) → Google Calendar  [conserve l’existant]
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
# ➜ NOUVEAU : Programmation depuis Notion (Plan) → Google Calendar
# ──────────────────────────────────────────────────────────────────────────────
@app.command("plan-push")
def plan_push(
    past_days: int = typer.Option(-1, "--past-days", help="Fenêtre passée (J-1 par défaut)."),
    next_days: int = typer.Option(30, "--next-days", help="Fenêtre future (J+30 par défaut)."),
):
    """
    Exporte les séances Notion (DB 'Plan') vers Google Calendar (agenda Sport).
    - Lit: Nom de la séance, Date prévue, Sport (select), Type de séance (multi-select), Durée prévue (min)
    - Crée/maj l'event GCal (couleur par sport, description enrichie)
    - Met à jour Notion: 'Mois' (select) + 'Durée prévue (min)' si absente
    """
    count = push_plans_window(after_days=past_days, before_days=next_days)
    print(f"[ok]Poussé [bold]{count}[/] séance(s) vers Google Calendar + mise à jour Notion.[/]")

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
def cmd_sync_strava_routes(
    force: bool = typer.Option(False, "--force", help="Force la resynchronisation de toutes les routes")
):
    """
    Importe / met à jour les itinéraires (“Mes itinéraires”) dans la DB 🗺️ Projets GPX.
    - Incrémental par défaut (ne traite que les nouvelles / modifiées)
    - Option --force pour tout retravailler
    """
    created, skipped = sync_strava_routes_to_notion(force=force)
    total = created + skipped
    print(
        f"[ok]Sync Routes terminée[/] — créés/mis à jour: [bold]{created}[/], "
        f"skippés (inchangés): [muted]{skipped}[/], total (compte local): [bold]{total}[/]"
    )

# ──────────────────────────────────────────────────────────────────────────────
# Lecture seule : compter les routes Strava (sans écrire)
# ──────────────────────────────────────────────────────────────────────────────
@app.command("routes-count")
def routes_count(
    sample: int = typer.Option(0, "--sample", "-n", help="Affiche les N premiers titres en échantillon.")
):
    """Compte les routes présentes dans 'Mes itinéraires' Strava (sans toucher Notion)."""
    count = 0
    titles: list[str] = []
    for rt in _iter_strava_routes():
        count += 1
        if sample and len(titles) < sample:
            name = (rt.get("name") or f"Route {rt.get('id')}")
            titles.append(name)
    print(f"[title]Routes Strava détectées[/]")
    print(f"• Total: [bold]{count}[/]")
    if titles:
        print(f"• Échantillon ({len(titles)}):")
        for t in titles:
            print(f"  - {t}")

# ──────────────────────────────────────────────────────────────────────────────
# Lecture seule : compter les pages réellement en DB Notion
# ──────────────────────────────────────────────────────────────────────────────
@app.command("routes-db-count")
def routes_db_count():
    """Compte les pages dans la DB Notion pointée par NOTION_DB_GPX (toutes vues confondues)."""
    notion_token = os.getenv("NOTION_API_KEY")
    db_id = os.getenv("NOTION_DB_GPX")
    if not notion_token or not db_id:
        print("[err]NOTION_API_KEY ou NOTION_DB_GPX manquant(e). Lance 'stravation env-check'.[/]")
        raise typer.Exit(code=1)

    notion = Notion(auth=notion_token)
    index = list_notion_routes_index(notion, db_id)
    print(f"[title]Pages en DB Notion[/]")
    print(f"• Database ID : [bold]{db_id}[/]")
    print(f"• Total (API) : [bold]{len(index)}[/]")

# ──────────────────────────────────────────────────────────────────────────────
# ➜ NOUVEAU : Diff Strava ↔ Notion
# ──────────────────────────────────────────────────────────────────────────────
@app.command("routes-diff")
def routes_diff(
    show: bool = typer.Option(False, "--show", help="Affiche les listes complètes (sinon résumé)."),
    sample: int = typer.Option(20, "--sample", "-n", help="Si --show n'est pas passé, nombre d’éléments à lister.")
):
    """
    Compare la liste Strava (Mes itinéraires) et la DB Notion (NOTION_DB_GPX) par ID de route.
    Affiche:
      • Manquantes dans Notion
      • Orphelines dans Notion (absentes de Strava)
    """
    notion_token = os.getenv("NOTION_API_KEY")
    db_id = os.getenv("NOTION_DB_GPX")
    if not notion_token or not db_id:
        print("[err]NOTION_API_KEY ou NOTION_DB_GPX manquant(e).[/]")
        raise typer.Exit(code=1)

    # Strava → {id: name}
    strava = {}
    for rt in _iter_strava_routes():
        strava[str(rt["id"])] = (rt.get("name") or f"Route {rt['id']}")

    # Notion → {id: {page_id, title}}
    notion = Notion(auth=notion_token)
    notion_index = list_notion_routes_index(notion, db_id)

    s_ids = set(strava.keys())
    n_ids = set(notion_index.keys())

    missing_in_notion = sorted(s_ids - n_ids, key=int)
    orphan_in_notion  = sorted(n_ids - s_ids, key=int)

    print("[title]Diff Strava ↔ Notion[/]")
    print(f"• Strava total : [bold]{len(s_ids)}[/]")
    print(f"• Notion total : [bold]{len(n_ids)}[/]")
    print(f"• Manquantes dans Notion : [bold]{len(missing_in_notion)}[/]")
    print(f"• Orphelines dans Notion : [bold]{len(orphan_in_notion)}[/]")

    def _print_list(title: str, ids: list[str]):
        if not ids:
            print(f"  {title}: rien à signaler.")
            return
        if show:
            print(f"\n{title} ({len(ids)}):")
            for rid in ids:
                left = strava.get(rid, "")
                right = notion_index.get(rid, {}).get("title", "")
                label = left or right or rid
                print(f"  - {rid} — {label}")
        else:
            head = ids[:sample]
            print(f"\n{title} (aperçu {len(head)}/{len(ids)}):")
            for rid in head:
                left = strava.get(rid, "")
                right = notion_index.get(rid, {}).get("title", "")
                label = left or right or rid
                print(f"  - {rid} — {label}")
            if len(ids) > len(head):
                print(f"  … (+{len(ids)-len(head)} autres)")

    _print_list("➤ Manquantes dans Notion", missing_in_notion)
    _print_list("➤ Orphelines dans Notion", orphan_in_notion)

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app()
