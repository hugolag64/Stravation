from __future__ import annotations
import json, sys, pathlib

def compact_and_escape(path: str) -> str:
    p = pathlib.Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    compact = json.dumps(data, separators=(",", ":"))         # compact
    escaped = compact.replace('"', r'\"')                     # escape quotes
    return escaped

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "credentials.json"
    out = compact_and_escape(src)
    print(f'GOOGLE_CREDENTIALS_JSON="{out}"')
    # Tu peux rediriger vers .env :  python tools/compact_credentials.py >> .env
