#!/usr/bin/env python3
"""
New Eden Terminal — local web UI
----------------------------------------
A browser-based front-end over eve_esi_terminal.py / eve_sso_auth.py.
Runs a local-only Flask server (binds to 127.0.0.1, never your LAN)
and serves a single page that calls the same commands as the CLI.

Requires: pip install flask requests

Usage:
    export ESI_CLIENT_ID="..."
    export ESI_CLIENT_SECRET="..."
    python eve_web.py
    # then open http://127.0.0.1:5000

Put this file in the same folder as eve_esi_terminal.py, eve_sso_auth.py,
skill_plan.py, and mining_report.py.
"""

import io
import os
import sys
import contextlib

import requests
from flask import Flask, render_template, request, jsonify

import eve_esi_terminal as esi
import eve_sso_auth as auth
import skill_plan
import mining_report
import dashboard

# When frozen by PyInstaller, templates/ is bundled alongside the extracted
# code (via --add-data) under sys._MEIPASS rather than next to this file's
# on-disk path, which Flask's default template_folder resolution can't see.
if getattr(sys, "frozen", False):
    _template_folder = os.path.join(sys._MEIPASS, "templates")
else:
    _template_folder = "templates"

app = Flask(__name__, template_folder=_template_folder)


def _capture(fn, *args):
    """Run fn(*args), returning (output_text, error_text_or_None)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(*args)
        return buf.getvalue(), None
    except SystemExit:
        return buf.getvalue(), None
    except Exception as e:
        return buf.getvalue(), str(e)


def _run_command(name, params):
    if name == "status":
        return _capture(esi.cmd_status)
    if name == "market":
        return _capture(esi.cmd_market, params.get("item", ""), params.get("region", "The Forge"))
    if name == "system":
        return _capture(esi.cmd_system, params.get("system", ""))
    if name == "whoami":
        return _capture(esi.cmd_whoami)
    if name == "wallet":
        return _capture(esi.cmd_wallet)
    if name == "skills":
        return _capture(esi.cmd_skills)
    if name == "location":
        return _capture(esi.cmd_location)
    if name == "jobs":
        return _capture(esi.cmd_jobs)
    if name == "corp-jobs":
        return _capture(esi.cmd_corp_jobs)
    if name == "mining":
        return _capture(esi.cmd_mining)
    if name == "blueprints":
        return _capture(esi.cmd_blueprints)
    if name == "corp-blueprints":
        return _capture(esi.cmd_corp_blueprints)
    if name == "orders":
        return _capture(esi.cmd_orders)
    if name == "corp-orders":
        return _capture(esi.cmd_corp_orders)
    if name == "structure-market":
        try:
            structure_id = int(str(params.get("structure_id", "0")).strip())
        except ValueError:
            return "", "Enter a valid numeric structure ID."
        return _capture(esi.cmd_structure_market, structure_id)
    if name == "mining-skills":
        return _capture(skill_plan.generate_mining_plan)
    if name == "mining-report":
        try:
            days = int(params.get("days", 30) or 30)
        except ValueError:
            days = 30
        outfile = params.get("out") or "mining_report.html"
        return _capture(mining_report.generate_report, days, outfile)
    return "", f"Unknown command: {name}"


@app.route("/")
@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


@app.route("/console")
def index():
    return render_template("index.html", regions=[r.title() for r in esi.REGIONS])


@app.route("/api/dashboard")
def api_dashboard():
    try:
        mining_days = int(request.args.get("mining_days", 7))
    except ValueError:
        mining_days = 7
    hours_per_day = request.args.get("hours_per_day")
    try:
        hours_per_day = float(hours_per_day) if hours_per_day else None
    except ValueError:
        hours_per_day = None

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            data = dashboard.get_dashboard_data(mining_days=mining_days, hours_per_day=hours_per_day)
        return jsonify({"ok": True, "data": data})
    except SystemExit:
        printed = buf.getvalue().strip()
        return jsonify({"ok": False, "error": printed or "Not logged in — click Login via EVE SSO first."})
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 401:
            return jsonify({"ok": False, "error": "401 Unauthorized — your saved login is missing a required scope. Re-run login."})
        if status == 403:
            return jsonify({"ok": False, "error": "403 Forbidden — your character lacks access to one of these endpoints."})
        return jsonify({"ok": False, "error": f"ESI request failed ({status})."})
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "error": f"ESI request failed: {e}"})


@app.route("/api/status")
def api_status():
    characters = auth.list_characters() if auth is not None else []
    active_id = auth.get_active_character_id() if characters else None
    active_name = next((c["character_name"] for c in characters if c["character_id"] == active_id), None)
    return jsonify({
        "logged_in": bool(characters),
        "active_character_id": active_id,
        "active_character_name": active_name,
    })


@app.route("/api/characters")
def api_characters():
    return jsonify({
        "characters": auth.list_characters(),
        "active_character_id": auth.get_active_character_id(),
    })


@app.route("/api/characters/switch", methods=["POST"])
def api_characters_switch():
    data = request.get_json(force=True) or {}
    try:
        auth.set_active_character(data.get("character_id"))
        return jsonify({"ok": True})
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/characters/add", methods=["POST"])
def api_characters_add():
    """Runs the same OAuth flow as /api/login, but framed for adding a
    second (or further) character rather than the first login — the
    underlying auth.add_character() call handles both cases identically."""
    output, error = _capture(auth.add_character)
    return jsonify({"ok": error is None, "output": output, "error": error})


@app.route("/api/characters/remove", methods=["POST"])
def api_characters_remove():
    data = request.get_json(force=True) or {}
    auth.remove_character(data.get("character_id"))
    return jsonify({"ok": True})


@app.route("/api/login", methods=["POST"])
def api_login():
    output, error = _capture(auth.login)
    return jsonify({"ok": error is None, "output": output, "error": error})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    auth.logout()
    return jsonify({"ok": True})


@app.route("/api/open-console", methods=["POST"])
def api_open_console():
    """Opens the command-console page as a real native app window, for the
    dashboard's "Command terminal" link — deferred import to avoid a
    circular import (eve_gui.py already imports this module)."""
    try:
        import eve_gui
        eve_gui.spawn_console_window(request.environ.get("SERVER_PORT"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True) or {}
    command = data.get("command", "")
    params = data.get("params", {}) or {}
    output, error = _run_command(command, params)
    return jsonify({"ok": error is None, "output": output, "error": error})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
