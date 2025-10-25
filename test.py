# Test.py  — backfill Semaine ISO (format YYYY-Www) pour la DB Plans
import os
from notion_client import Client
from stravation.services.notion_plans import backfill_iso_week_for_plans
from stravation.utils.envtools import load_dotenv_if_exists

if __name__ == "__main__":
    load_dotenv_if_exists()
    api_key = os.getenv("NOTION_API_KEY")
    if not api_key:
        raise RuntimeError("NOTION_API_KEY manquant dans l'environnement (.env).")
    client = Client(auth=api_key)

    stats = backfill_iso_week_for_plans(notion=client, dry_run=False)
    print("[Backfill Semaine ISO] ->", stats)
