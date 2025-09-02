from __future__ import annotations
import os
from dotenv import load_dotenv

# Charge automatiquement .env (à la racine du projet)
load_dotenv(override=False)


def _bool(envval: str | None, default: bool = False) -> bool:
    if envval is None:
        return default
    return envval.strip().lower() in {"1", "true", "yes", "on", "y"}


# ----- Timezone & horaires par défaut -----
SPORT_TZ = os.getenv("SPORT_TZ", "Indian/Reunion")       # ✅ alias attendu partout
TZ = SPORT_TZ                                           # compatibilité ancien code

MORNING_REMINDER_TIME = os.getenv("SPORT_MORNING_TIME", "06:30")  # HH:MM
DEFAULT_SESSION_TIME = os.getenv("SPORT_SESSION_TIME", "17:30")   # HH:MM


# ----- Google OAuth -----
# Recommandé: GOOGLE_CREDENTIALS_PATH vers un fichier 'credentials.json'
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
# Optionnel: JSON inline sur une seule ligne (moins pratique sous Windows)
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", ".gcal_token.json")
SPORT_CALENDAR_NAME = os.getenv("SPORT_CAL_NAME", "Sport")


# ----- Notion -----
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DB_ACTIVITIES = os.getenv("NOTION_DB_ACTIVITIES")  # DB pour import Strava
NOTION_DB_PLANNING = os.getenv("NOTION_DB_PLANNING")      # DB pour planning
NOTION_DB_PLACES = os.getenv("NOTION_DB_PLACES")


# ----- Strava -----
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")
STRAVA_REDIRECT_URI = os.getenv("STRAVA_REDIRECT_URI", "http://localhost")


# ----- Storage -----
DB_PATH = os.getenv("SPORT_DB_PATH", "stravation.sqlite3")
RATE_SAFETY = float(os.getenv("RATE_SAFETY", "0.15"))


# ----- Flags -----
DOWNLOAD_GPX = _bool(os.getenv("DOWNLOAD_GPX"), default=False)
GPX_DIR = os.getenv("GPX_DIR", "gpx")
GPX_MAX_PER_RUN = int(os.getenv("GPX_MAX_PER_RUN", "10"))
