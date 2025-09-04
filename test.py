import os, json, pathlib
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials as UserCreds
from google.oauth2.service_account import Credentials as ServiceCreds
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _cred_path():
    return os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")

def load_credentials():
    p = pathlib.Path(_cred_path())
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("type") == "service_account":
        creds = ServiceCreds.from_service_account_info(data, scopes=SCOPES)
        return creds, "service", data.get("client_email")
    # OAuth utilisateur
    token_file = pathlib.Path("token.json")
    if token_file.exists():
        creds = UserCreds.from_authorized_user_file(str(token_file), SCOPES)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(p), SCOPES)
        creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    # Récupère l'email via l'API oauth2
    oauth2 = build("oauth2", "v2", credentials=creds)
    email = oauth2.userinfo().get().execute().get("email")
    return creds, "oauth", email

def main():
    creds, kind, email = load_credentials()
    print(f"Type: {kind} — Email: {email}")
    cal = build("calendar", "v3", credentials=creds)
    # Montre quelques agendas visibles
    lst = cal.calendarList().list(maxResults=5).execute().get("items", [])
    print("Calendriers visibles (extrait):", [it.get("id") for it in lst])
    cal_id = os.getenv("WORK_CALENDAR_ID")
    if cal_id:
        try:
            meta = cal.calendarList().get(calendarId=cal_id).execute()
            print("Accès OK au WORK_CALENDAR_ID →", meta.get("summary"))
        except Exception as e:
            print("⚠️  Pas d'accès au WORK_CALENDAR_ID:", e)

if __name__ == "__main__":
    main()
