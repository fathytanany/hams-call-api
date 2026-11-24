import os
from pathlib import Path

# Dev defaults match docker-compose.yml so `make run` and `make test` work as-is.
# This is a local emulator; set real values through the environment anywhere else.
ACCOUNT_SID = os.environ.get("ACCOUNT_SID", "AC00000000000000000000000000000001")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "dev-auth-token")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://callapi:callapi@localhost:5432/callapi")
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "media"))
# Calls this server runs at once; extra calls wait in `queued`. 0 makes an API-only server.
MAX_CALLS = int(os.environ.get("MAX_CALLS", "500"))
