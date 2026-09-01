#!/usr/bin/env python3
"""
EVE SSO OAuth2 helper
----------------------
Handles the authorization-code flow against EVE's SSO, stores the
refresh token locally, and hands back a valid access token on demand
(refreshing automatically when expired).

Setup (one-time):
    1. Register an app at https://developers.eveonline.com/applications
       - Callback URL: http://localhost:8765/callback
       - Pick the scopes you want (see README notes in eve_esi_terminal.py)
    2. Set your credentials as environment variables (recommended) or edit
       the fallback constants below:

         export ESI_CLIENT_ID="your-client-id"
         export ESI_CLIENT_SECRET="your-secret-key"

    3. Run: python eve_sso_auth.py login
       This opens your browser, you log in via EVE SSO, and a token file
       (esi_token.json) is saved locally.

esi_token.json contains a refresh token — treat it like a password.
Do not commit it to git or share it. Add it to .gitignore.
"""

import os
import sys
import json
import base64
import secrets
import webbrowser
import http.server
import urllib.parse
import threading
import requests

def _app_data_dir():
    """Where local state (token, saved credentials) gets persisted. Uses
    the script's own folder when running from source, but a real per-user
    AppData folder when frozen into an .exe — a PyInstaller onefile build
    unpacks to a temp directory that's deleted when the process exits, so
    writing next to __file__ there would silently lose everything on the
    next launch."""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "NewEdenTerminal")
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.dirname(os.path.abspath(__file__))


CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize/"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

TOKEN_FILE = os.path.join(_app_data_dir(), "esi_token.json")
CREDENTIALS_FILE = os.path.join(_app_data_dir(), "eve_credentials.json")
CHARACTERS_FILE = os.path.join(_app_data_dir(), "characters.json")


def _load_saved_credentials():
    """Client ID/Secret saved locally by the GUI's first-run screen, used
    as a fallback when the ESI_CLIENT_ID / ESI_CLIENT_SECRET env vars
    aren't set (the normal case for a packaged .exe, which has no shell
    to export env vars into)."""
    if not os.path.exists(CREDENTIALS_FILE):
        return "", ""
    try:
        with open(CREDENTIALS_FILE) as f:
            data = json.load(f)
        return data.get("client_id", ""), data.get("client_secret", "")
    except (OSError, json.JSONDecodeError):
        return "", ""


def save_credentials(client_id, client_secret):
    """Persist the user's own EVE developer app credentials locally and
    make them active for the rest of this process."""
    global CLIENT_ID, CLIENT_SECRET
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret}, f, indent=2)
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass
    CLIENT_ID, CLIENT_SECRET = client_id, client_secret


def has_credentials():
    return bool(CLIENT_ID and CLIENT_SECRET)


CLIENT_ID = os.environ.get("ESI_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ESI_CLIENT_SECRET", "")
if not CLIENT_ID or not CLIENT_SECRET:
    _saved_id, _saved_secret = _load_saved_credentials()
    CLIENT_ID = CLIENT_ID or _saved_id
    CLIENT_SECRET = CLIENT_SECRET or _saved_secret

# Scoped to exactly what eve_esi_terminal.py's authenticated commands use —
# keep this list in sync if you add commands that need more.
DEFAULT_SCOPES = [
    "publicData",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-planets.manage_planets.v1",
    "esi-assets.read_assets.v1",
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-industry.read_character_mining.v1",
    "esi-industry.read_corporation_mining.v1",
    "esi-characters.read_blueprints.v1",
    "esi-corporations.read_blueprints.v1",
    "esi-markets.read_character_orders.v1",
    "esi-markets.read_corporation_orders.v1",
    "esi-markets.structure_markets.v1",
]


def _require_credentials():
    if not CLIENT_ID or not CLIENT_SECRET:
        print(
            "Missing ESI_CLIENT_ID / ESI_CLIENT_SECRET.\n"
            "Set them as environment variables before running login, e.g.:\n"
            '  export ESI_CLIENT_ID="..."\n'
            '  export ESI_CLIENT_SECRET="..."\n'
        )
        sys.exit(1)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the ?code=...&state=... redirect from EVE SSO."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [None])[0]
        self.server.returned_state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;padding:40px;"
            b"background:#05070c;color:#d7e1f2;'>"
            b"<h2>Login received.</h2>"
            b"<p>You can close this tab and go back to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, format, *args):
        pass  # silence default request logging


def _run_oauth_flow(scopes):
    """Runs the interactive browser OAuth flow and returns the raw token
    response dict. Shared by add_character()/login() — what happens to
    the result afterward is the only difference between them."""
    _require_credentials()
    scopes = scopes or DEFAULT_SCOPES
    state = secrets.token_urlsafe(24)

    query = urllib.parse.urlencode({
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "scope": " ".join(scopes),
        "state": state,
    })
    auth_url = f"{AUTHORIZE_URL}?{query}"

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    server.auth_code = None
    server.returned_state = None

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"Opening browser for EVE SSO login...\nIf it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=180)

    if not server.auth_code:
        print("Login timed out or was cancelled — no authorization code received.")
        sys.exit(1)
    if server.returned_state != state:
        print("State mismatch — possible CSRF issue, aborting.")
        sys.exit(1)

    return _exchange_code(server.auth_code)


_login_lock = threading.Lock()


def add_character(scopes=None):
    """Runs the OAuth flow and adds the resulting character to the
    multi-character store, activating it — used both for the very first
    login and for adding a second (or further) character, from any EVE
    account (EVE's own SSO login screen decides which account/character
    gets authorized; this app doesn't restrict it). Guarded against a
    second concurrent invocation with a non-blocking lock, since the
    localhost callback server can only bind CALLBACK_PORT once at a
    time — a second click while one login is already in progress would
    otherwise raise an ugly "address already in use" error."""
    if not _login_lock.acquire(blocking=False):
        print("A login is already in progress — finish or cancel it first.")
        sys.exit(1)
    try:
        tokens = _run_oauth_flow(scopes)
        info = verify_token(tokens["access_token"])
        char_id = info["CharacterID"]
        with _token_lock:
            store = _load_store()
            store["characters"][str(char_id)] = {**tokens, "character_name": info["CharacterName"]}
            store["active_character_id"] = char_id
            _save_store(store)
        print(f"\nLogged in as {info['CharacterName']} (character_id {char_id})")
        print(f"Token saved to {CHARACTERS_FILE}\n")
        return info
    finally:
        _login_lock.release()


def login(scopes=None):
    """Back-compat entry point (CLI `python eve_sso_auth.py login`,
    eve_web.py's original /api/login) — same as add_character()."""
    add_character(scopes)


def _exchange_code(code):
    return _token_request({"grant_type": "authorization_code", "code": code})


def _refresh(refresh_token):
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


def _token_request(body):
    _require_credentials()
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "login.eveonline.com",
    }
    r = requests.post(TOKEN_URL, headers=headers, data=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _save_store(store):
    """Writes the WHOLE multi-character store to a temp file then renames
    it into place — os.replace() is atomic on both POSIX and Windows, so
    a concurrent _load_store() call (get_access_token() is called from
    many threads in parallel by dashboard.get_dashboard_data()) can never
    observe a half-written or truncated file the way a plain
    open(CHARACTERS_FILE, "w") could. Always the FULL store, never just
    one character's entry — writing a partial store here would silently
    destroy every other saved character's login."""
    tmp_path = f"{CHARACTERS_FILE}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(store, f, indent=2)
    try:
        os.chmod(tmp_path, 0o600)  # owner read/write only, where the OS supports it
    except OSError:
        pass
    os.replace(tmp_path, CHARACTERS_FILE)


def _load_store():
    """Loads the multi-character store, {"active_character_id": int or
    None, "characters": {str(character_id): {..raw token fields..,
    "character_name": str}}}. Auto-migrates a legacy single-character
    esi_token.json the first time this runs after upgrading (a pure
    local JWT decode, no ESI call — works whether the access token is
    expired or not, since verify_token() never checks expiry), so an
    already-saved login survives with no forced re-authentication. Only
    triggers once: after migration, CHARACTERS_FILE exists, so this
    branch is never reached again."""
    if os.path.exists(CHARACTERS_FILE):
        with open(CHARACTERS_FILE) as f:
            return json.load(f)

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            legacy_tokens = json.load(f)
        info = verify_token(legacy_tokens["access_token"])
        char_id = info["CharacterID"]
        store = {
            "active_character_id": char_id,
            "characters": {str(char_id): {**legacy_tokens, "character_name": info["CharacterName"]}},
        }
        _save_store(store)
        return store

    return {"active_character_id": None, "characters": {}}


def list_characters():
    """Every saved character's id/name — reads the store directly, no ESI
    call, so a character switcher can populate instantly."""
    store = _load_store()
    return [
        {"character_id": int(cid), "character_name": data.get("character_name") or f"Character {cid}"}
        for cid, data in store["characters"].items()
    ]


def get_active_character_id():
    return _load_store().get("active_character_id")


def set_active_character(character_id):
    """Switches which saved character get_access_token()/get_character_id()
    resolve to. Raises KeyError if character_id isn't a saved character."""
    with _token_lock:
        store = _load_store()
        if str(character_id) not in store["characters"]:
            raise KeyError(f"No saved character with id {character_id}")
        store["active_character_id"] = int(character_id)
        _save_store(store)


def remove_character(character_id):
    """Removes one saved character. If it was the active one, auto-
    activates another remaining character if any exist, else clears
    active_character_id. No-op if character_id isn't saved."""
    with _token_lock:
        store = _load_store()
        cid_str = str(character_id)
        if cid_str not in store["characters"]:
            return
        del store["characters"][cid_str]
        if store.get("active_character_id") == int(character_id):
            remaining = list(store["characters"].keys())
            store["active_character_id"] = int(remaining[0]) if remaining else None
        _save_store(store)


def logout():
    """Removes the currently active character. Local-only — EVE's SSO has
    no server-side session to revoke for this flow; logging back in just
    starts a fresh authorization from scratch."""
    active_id = get_active_character_id()
    if active_id is not None:
        remove_character(active_id)


def verify_token(access_token):
    """Extract character identity from the SSO v2 JWT access token.

    ESI's old /verify/ endpoint is deprecated (404s) — access tokens are
    now JWTs whose payload already carries the character's identity.
    """
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    character_id = int(payload["sub"].split(":")[-1])
    return {"CharacterID": character_id, "CharacterName": payload["name"]}


_token_lock = threading.Lock()


def get_access_token():
    """Returns a valid access token for the currently ACTIVE character,
    refreshing via its stored refresh token if needed. Serialized with a
    lock — dashboard.get_dashboard_data() calls this from many threads in
    parallel (every ESI-backed section fetches its own access token), and
    without the lock they'd all hit EVE's SSO token endpoint at once for
    the same refresh token, and race writing the shared multi-character
    store. Every other caller in the codebase still calls this with zero
    arguments and gets back exactly what it always has — "the" token now
    just means "the active character's" underneath."""
    with _token_lock:
        store = _load_store()
        active_id = store.get("active_character_id")
        if not active_id or str(active_id) not in store["characters"]:
            print("No saved login found. Run: python eve_sso_auth.py login")
            sys.exit(1)
        tokens = store["characters"][str(active_id)]

        # Always attempt a refresh — access tokens are short-lived (~20 min),
        # and refresh is cheap/fast, so this avoids tracking expiry manually.
        try:
            new_tokens = _refresh(tokens["refresh_token"])
        except requests.RequestException as e:
            print(f"Token refresh failed: {e}\nTry logging in again: python eve_sso_auth.py login")
            sys.exit(1)

        # Read-merge-write: update only the active character's entry within
        # the full store, so every other saved character's login survives.
        store["characters"][str(active_id)] = {**new_tokens, "character_name": tokens.get("character_name")}
        _save_store(store)
        return new_tokens["access_token"]


def get_character_id():
    token = get_access_token()
    info = verify_token(token)
    return info["CharacterID"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    elif len(sys.argv) > 1 and sys.argv[1] == "whoami":
        info = verify_token(get_access_token())
        print(json.dumps(info, indent=2))
    else:
        print("Usage:\n  python eve_sso_auth.py login   # first-time login\n  python eve_sso_auth.py whoami  # confirm current token works")
