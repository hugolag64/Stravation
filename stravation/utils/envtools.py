from __future__ import annotations
import os
from typing import Dict, Tuple

ENV_GROUPS = {
    "Core": {
        "SPORT_TZ": "Europe/Paris",
        "SPORT_MORNING_TIME": "06:30",
        "SPORT_SESSION_TIME": "17:30",
        "SPORT_DB_PATH": "stravanotion.sqlite3",
    },
    "Notion (requis pour import)": {
        "NOTION_API_KEY": "",
        "NOTION_DB_ACTIVITIES": "",   # ID DB Notion activités
        "NOTION_DB_PLANNING": "",     # optionnel pour planning
    },
    "Strava (requis pour import)": {
        "STRAVA_CLIENT_ID": "",
        "STRAVA_CLIENT_SECRET": "",
        "STRAVA_REFRESH_TOKEN": "",
    },
    "Google Calendar (requis pour plan)": {
        "GOOGLE_CREDENTIALS_PATH": "credentials.json",
        # ou alternative (déconseillé sous Windows) :
        # "GOOGLE_CREDENTIALS_JSON": "",
        "GOOGLE_TOKEN_PATH": ".gcal_token.json",
        "SPORT_CAL_NAME": "Sport",
    },
    "Options": {
        "RATE_SAFETY": "0.15",
        "DOWNLOAD_GPX": "0",
        "GPX_DIR": "gpx",
        "GPX_MAX_PER_RUN": "10",
    },
}

REQUIRED_KEYS = {
    # Import Strava → Notion
    "NOTION_API_KEY",
    "NOTION_DB_ACTIVITIES",
    "STRAVA_CLIENT_ID",
    "STRAVA_CLIENT_SECRET",
    "STRAVA_REFRESH_TOKEN",
    # Planning → Google Calendar
    # Une de ces deux clés est attendue pour GCal:
    # GOOGLE_CREDENTIALS_PATH ou GOOGLE_CREDENTIALS_JSON
}

def generate_env_example() -> str:
    lines = [
        "# Stravanotion — .env.example",
        "# Duplique ce fichier en .env et remplis les valeurs nécessaires.",
        "",
        "### Core",
        f'SPORT_TZ="{ENV_GROUPS["Core"]["SPORT_TZ"]}"',
        f'SPORT_MORNING_TIME="{ENV_GROUPS["Core"]["SPORT_MORNING_TIME"]}"',
        f'SPORT_SESSION_TIME="{ENV_GROUPS["Core"]["SPORT_SESSION_TIME"]}"',
        f'SPORT_DB_PATH="{ENV_GROUPS["Core"]["SPORT_DB_PATH"]}"',
        "",
        "### Notion (requis pour import)",
        'NOTION_API_KEY=""',
        'NOTION_DB_ACTIVITIES=""',
        'NOTION_DB_PLANNING=""',
        "",
        "### Strava (requis pour import)",
        'STRAVA_CLIENT_ID=""',
        'STRAVA_CLIENT_SECRET=""',
        'STRAVA_REFRESH_TOKEN=""',
        "",
        "### Google Calendar (requis pour plan)",
        '# Recommandé: pointer vers un fichier credentials.json',
        'GOOGLE_CREDENTIALS_PATH="credentials.json"',
        '# Alternative (moins pratique) : coller le JSON compact sur une seule ligne',
        '# GOOGLE_CREDENTIALS_JSON=""',
        'GOOGLE_TOKEN_PATH=".gcal_token.json"',
        'SPORT_CAL_NAME="Sport"',
        "",
        "### Options",
        'RATE_SAFETY="0.15"',
        'DOWNLOAD_GPX="0"',
        'GPX_DIR="gpx"',
        'GPX_MAX_PER_RUN="10"',
        "",
    ]
    return "\n".join(lines)

def write_env_example(path: str = ".env.example", overwrite: bool = False) -> str:
    if os.path.exists(path) and not overwrite:
        return path
    with open(path, "w", encoding="utf-8") as f:
        f.write(generate_env_example())
    return path

def check_env() -> Tuple[Dict[str, bool], Dict[str, str]]:
    """
    Retourne (status_par_clef, erreurs_par_clef).
    Règle spéciale GCal : on accepte PATH OU JSON.
    """
    status: Dict[str, bool] = {}
    errors: Dict[str, str] = {}

    # Notion & Strava requis pour import
    for k in ["NOTION_API_KEY", "NOTION_DB_ACTIVITIES", "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"]:
        ok = bool(os.getenv(k))
        status[k] = ok
        if not ok:
            errors[k] = "manquant"

    # GCal: au moins l’un des deux
    has_path = bool(os.getenv("GOOGLE_CREDENTIALS_PATH"))
    has_json = bool(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    status["GOOGLE_CREDENTIALS_PATH|JSON"] = has_path or has_json
    if not (has_path or has_json):
        errors["GOOGLE_CREDENTIALS_PATH|JSON"] = "spécifie un fichier ou un JSON compact"

    # Optionnels: juste pour info
    for k in ["NOTION_DB_PLANNING", "SPORT_TZ", "SPORT_MORNING_TIME", "SPORT_SESSION_TIME",
              "SPORT_DB_PATH", "GOOGLE_TOKEN_PATH", "SPORT_CAL_NAME",
              "RATE_SAFETY", "DOWNLOAD_GPX", "GPX_DIR", "GPX_MAX_PER_RUN"]:
        status[k] = bool(os.getenv(k, "")) or True  # considéré ok même si vide/non requis

    return status, errors
