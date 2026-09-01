# New Eden Terminal

A personal companion toolkit for EVE Online, built on CCP's public ESI
API. It comes in three front ends over the same core logic — a CLI, a
local web dashboard, and a desktop app that opens that same dashboard
as an ordinary page in your regular browser (not a wrapped window) —
plus a few standalone reports:

- **Character/corp status** — location, ship, skills & skill queue, wallet, assets, blueprints, industry jobs
- **Combined dashboard** — one view of all of the above, plus mining throughput and ISK/hour, loaded concurrently so a full refresh takes seconds rather than the sum of every section fetched one at a time
- **Skill group browser** — pick any of EVE's own ~21 real skill groups (Drones, Gunnery, Navigation, Spaceship Command, ...) from a dropdown and see every skill in it as a row of 5 pips — light for trained, dark for untrained — the same shape as the in-game Character Sheet
- **Mining ISK report** — pulls the corp mining ledger, prices it against Jita market averages, outputs an HTML report
- **Tips** — every tab (Mining, Industry, PVP, Skills, Corporation) gets a handful of concrete, computed tips, with an Overview summary pulling the top one from each, and a "Learn why" on each one that expands to the real reasoning: skill-training suggestions grounded in your own recent activity (do you actually mine what this skill boosts, do you have an active job it applies to), ship-eligibility checks with real ship-to-ship stat comparisons, PVP skill tips that compute a real before/after number on your current ship's hull stats, and Industry tips comparing your own open market orders against the live regional order book (no external calls, no API key)
- **Missing-permission banners** — if your saved login is missing a scope some section needs, that one section shows what's missing and how to fix it instead of the whole dashboard failing to load
- **Historical logging** — a local SQLite database that accumulates market prices and mining ledger entries over time (ESI's own history windows are short)
- **zKillboard integration** — recent kills/losses and simple stats for a character

Public endpoints (market prices, system info) need no authentication.
Character/corp-specific commands authenticate via EVE SSO (OAuth2).

## Setup

1. **Install dependencies**

   ```
   pip install -r requirements.txt
   ```

   (`tkinter`, used by the GUI, ships with most Python installs — on
   some Linux distros you may need to install `python3-tk` separately.)

   The desktop app's "Open Console" window uses `pywebview`, which on
   Windows needs the **WebView2 Runtime** to render — it ships
   pre-installed with Windows 10/11 in the vast majority of cases via
   Windows Update/Edge, so this usually needs nothing extra, but it's
   the one real system-level dependency beyond the `pip install` above.

2. **Register an application** at https://developers.eveonline.com/applications
   - Callback URL: `http://localhost:8765/callback`
   - Scopes: see `DEFAULT_SCOPES` in `eve_sso_auth.py` for the exact list this project uses

3. **Set your credentials** as environment variables — never hardcode them:

   ```
   export ESI_CLIENT_ID="your-client-id"
   export ESI_CLIENT_SECRET="your-secret-key"
   ```

   (If you're using the desktop GUI or the Windows `.exe`, skip this —
   it asks for these on first launch instead and saves them locally.)

4. **Log in once**

   ```
   python eve_sso_auth.py login
   ```

   This opens your browser, walks you through EVE SSO, and saves a
   token file (`esi_token.json`) locally. That file holds your refresh
   token — treat it like a password. It's already listed in
   `.gitignore` and should never be committed or shared.

## Usage

Pick whichever front end you prefer — they share the same underlying code:

```
# CLI — run with no arguments for an interactive menu
python eve_esi_terminal.py status
python eve_esi_terminal.py market "Tritanium" --region "The Forge"
python eve_esi_terminal.py dashboard --mining-days 7

# Local web dashboard (binds to 127.0.0.1 only)
python eve_web.py
# then open http://127.0.0.1:5000

# Desktop app — runs the same eve_web.py dashboard internally (no
# separate server to start), opens it in your regular default browser,
# and runs a system tray icon in the background (Open Dashboard /
# Open Console / Quit) — its Quit item is what actually stops the app
python eve_gui.py
```

The tray icon's "Open Console" item — and the dashboard's own
"Command terminal" link at the bottom of the page — open the old
CLI-style command page as its own separate native app window, distinct
from the browser-tab dashboard.

Standalone reports:

```
python eve_esi_terminal.py mining-report --days 30 --out report.html
python eve_esi_terminal.py mining-skills
```

## Windows: no-install .exe

For sharing with people who don't have Python installed, the desktop
app can be packaged into a single portable `New-Eden-Terminal.exe` —
no install step, no admin rights, just download and double-click. It
opens the live dashboard in your regular default browser — same tabs,
same data, as `eve_web.py`, just not wrapped in a special window — and
runs a system tray icon in the background so there's something to
click if you want to reopen the dashboard tab, open the
command-console page as its own native window, or fully quit the app.
Closing your browser tab does *not* stop the app; the tray icon's Quit
item does.

First launch shows a setup screen that walks through registering a
free EVE developer app (same one-time step as above, just done inside
the app instead of a terminal) and saves the Client ID/Secret locally.
Everything the `.exe` writes — that config and the login token — goes
to `%APPDATA%\NewEdenTerminal\`, not next to the `.exe` itself, so it
persists across launches and updates.

Nobody's login or character data passes through anyone else's
machine or account — each person who runs it registers their own app
and logs into their own EVE account.

**A freshly-built `.exe` is unsigned**, so antivirus software commonly
holds or blocks it the first time it's run purely because the file
itself is new and has no reputation history yet — this isn't a sign
anything's actually wrong. On Avast specifically, this shows up as
**CyberCapture**; add the `.exe` (or its folder) under
**Avast → Settings → General → Exceptions**. Windows SmartScreen may
also warn — "More info" → "Run anyway" clears it.

### Building it yourself

```
pip install pyinstaller
pyinstaller --onefile --windowed --name New-Eden-Terminal --add-data "templates;templates" --collect-all webview eve_gui.py
```

`--add-data` bundles `templates/` (both `dashboard.html` and the
console's `index.html`) into the `.exe` (Flask can't find it
otherwise — PyInstaller only auto-detects Python code, not data
files). `--collect-all webview` bundles pywebview's own internal
resource files, which its own PyInstaller hook doesn't fully cover.
The tray icon's `pystray` doesn't need an equivalent flag — its
PyInstaller hook (shipped by `pyinstaller-hooks-contrib`, already a
PyInstaller dependency) auto-discovers the Windows backend module — but
if the tray icon doesn't appear in a build, add
`--hidden-import pystray._win32` as a fallback. Opening the console
window re-invokes this same `.exe` with a hidden
`--console-window` flag as a separate process — a onefile build
re-extracts itself briefly on every launch, so that window takes a
moment longer to appear than the browser tab does; this is normal.
The finished `.exe` lands in `dist/`. `build/` and the generated
`.spec` file are safe to delete (and are already `.gitignore`d) —
rerun the command above any time the source changes.

## A note on data & privacy

Everything this project generates about *your* character or corp
(`esi_token.json`, `dashboard_data.json`, `corp_overview_data.json`,
`skill_plans_with_desc.json`, `portrait_data_uri.txt`, `eve_history.db`,
generated mining reports) is written to your local disk and is already
excluded via `.gitignore`. If you fork or share this project, double-check
those files aren't sitting in your working copy before you push.

## Credits

Built against CCP Games' ESI API. Kill/loss data via
[zKillboard](https://zkillboard.com), a third-party community site —
not affiliated with CCP.
