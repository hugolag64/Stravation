# tools/list_cals.py
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly",
          "openid", "https://www.googleapis.com/auth/userinfo.email"]

def creds():
    return Credentials.from_authorized_user_file("token.json", SCOPES)

def main():
    svc = build("calendar", "v3", credentials=creds())
    items = []
    page = None
    while True:
        resp = svc.calendarList().list(maxResults=250, pageToken=page).execute()
        items += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            break
    for it in items:
        print(f"{it.get('summary'):<35}  {it.get('id')}")
    print(f"\nTotal: {len(items)}")

if __name__ == "__main__":
    main()
