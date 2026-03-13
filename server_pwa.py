#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_pwa.py – PosturoSPS PWA Server
Extends serverexercice8v3.py with:
  - PWA-ready routes (serve static/index.html at /)
  - Patient management API (/patients)
  - Session logging API (/sessions)
  - Preset API (/presets)
  - System info route (/api/info)
Usage:
  python3 server_pwa.py [--uart /dev/ttyUSB0] [--port 5000] [--invert]
"""

import json
import os
import threading
import time
from datetime import datetime

from flask import request, Response, send_from_directory, send_file

# =========================================================
# Import all backend logic from the reference server.
# This registers all existing routes (SOT, exercises, HDMI…)
# =========================================================
from serverexercice8v3 import app, main as _orig_main, latest, lock

# =========================================================
# DATA DIRECTORIES
# =========================================================
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

PATIENTS_FILE = os.path.join(DATA_DIR, "patients.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
PRESETS_FILE  = os.path.join(DATA_DIR, "presets.json")

_data_lock = threading.Lock()

# =========================================================
# HELPERS
# =========================================================
def _load_json(path, default=None):
    if default is None:
        default = []
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[DATA] load error {path}: {e}")
    return default

def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[DATA] save error {path}: {e}")

def _json_response(data, status=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        mimetype="application/json"
    )

def _get_body():
    try:
        return request.get_json(force=True) or {}
    except Exception:
        return {}

# =========================================================
# OVERRIDE ROOT: serve PWA index.html
# =========================================================
def _pwa_index():
    """Serve the PWA shell."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "static"),
        "index.html"
    )

# Replace the legacy HTML root with the PWA index
app.view_functions["index"] = _pwa_index


# =========================================================
# PATIENTS API
# =========================================================
@app.route("/patients", methods=["GET"])
def patients_get():
    with _data_lock:
        data = _load_json(PATIENTS_FILE)
    return _json_response(data)

@app.route("/patients", methods=["POST"])
def patients_create():
    body = _get_body()
    if not body.get("nom") or not body.get("prenom"):
        return _json_response({"error": "nom and prenom required"}, 400)
    if not body.get("id"):
        body["id"] = f"pat_{int(time.time()*1000)}"
    body.setdefault("createdAt", datetime.now().isoformat())
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
        patients.append(body)
        _save_json(PATIENTS_FILE, patients)
    return _json_response(body, 201)

@app.route("/patients/<patient_id>", methods=["GET"])
def patients_get_one(patient_id):
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
    p = next((x for x in patients if x.get("id") == patient_id), None)
    if not p:
        return _json_response({"error": "not found"}, 404)
    return _json_response(p)

@app.route("/patients/<patient_id>", methods=["PUT"])
def patients_update(patient_id):
    body = _get_body()
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
        for i, p in enumerate(patients):
            if p.get("id") == patient_id:
                patients[i] = {**p, **body, "id": patient_id}
                _save_json(PATIENTS_FILE, patients)
                return _json_response(patients[i])
    return _json_response({"error": "not found"}, 404)

@app.route("/patients/<patient_id>", methods=["DELETE"])
def patients_delete(patient_id):
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
        new_list = [p for p in patients if p.get("id") != patient_id]
        _save_json(PATIENTS_FILE, new_list)
    return _json_response({"ok": True})


# =========================================================
# SESSIONS API
# =========================================================
@app.route("/sessions", methods=["GET"])
def sessions_get():
    patient_id = request.args.get("patient")
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
    if patient_id:
        sessions = [s for s in sessions if s.get("patient") == patient_id]
    # Return latest first, limited to 200
    return _json_response(list(reversed(sessions[-200:])))

@app.route("/sessions", methods=["POST"])
def sessions_create():
    body = _get_body()
    if not body.get("id"):
        body["id"] = f"ses_{int(time.time()*1000)}"
    body.setdefault("createdAt", datetime.now().isoformat())
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
        sessions.append(body)
        # Keep last 1000 sessions
        if len(sessions) > 1000:
            sessions = sessions[-1000:]
        _save_json(SESSIONS_FILE, sessions)
    return _json_response(body, 201)

@app.route("/sessions/export.csv", methods=["GET"])
def sessions_export_csv():
    """Export all sessions as CSV."""
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
    import io
    import csv
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "patient", "exercice", "preset", "debut", "fin",
                     "parametres", "score", "evenements"])
    for s in sessions:
        writer.writerow([
            s.get("id", ""),
            s.get("patient", ""),
            s.get("exId", ""),
            s.get("preset", ""),
            s.get("startTime", ""),
            s.get("endTime", ""),
            json.dumps(s.get("params", {})),
            json.dumps(s.get("score", {})),
            json.dumps(s.get("events", [])),
        ])
    csv_data = buf.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=posturosps_sessions_{datetime.now().strftime('%Y%m%d')}.csv"}
    )

@app.route("/sessions/export.json", methods=["GET"])
def sessions_export_json():
    """Export all sessions as JSON."""
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
    return Response(
        json.dumps(sessions, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition":
                 f"attachment; filename=posturosps_sessions_{datetime.now().strftime('%Y%m%d')}.json"}
    )


# =========================================================
# PRESETS API
# =========================================================
DEFAULT_PRESETS = [
    {
        "id": "vest",
        "name": "Vestibulaire",
        "icon": "🌀",
        "color": "vest",
        "desc": "VOR + cible + opto 12 min",
        "sequence": [
            {"ex": "ex5", "duration": 120, "params": {"platform": "fixed", "vor_mode": "lr", "vor_interval": 5}},
            {"ex": "ex8", "duration": 120, "params": {"platform": "fixed", "target_mode": "random", "difficulty": "medium"}},
        ]
    },
    {
        "id": "proprio",
        "name": "Proprioception",
        "icon": "⚖️",
        "color": "proprio",
        "desc": "Sinus + impulsions 15 min",
        "sequence": [
            {"ex": "ex2", "duration": 120, "params": {"amplitude": "low", "speed": "low"}},
            {"ex": "ex4", "duration": 150, "params": {"amplitude": "medium", "speed": "medium"}},
            {"ex": "ex3", "duration": 90,  "params": {"amplitude": "medium", "speed": "medium"}},
        ]
    },
    {
        "id": "dual",
        "name": "Double tâche",
        "icon": "🧠",
        "color": "dual",
        "desc": "Citations + COP 12 min",
        "sequence": [
            {"ex": "ex7", "duration": 120, "params": {"platform": "sinus", "amplitude": "low", "speed": "low"}},
            {"ex": "ex9", "duration": 120, "params": {"platform": "fixed", "sequence": "cross", "difficulty": "medium"}},
        ]
    },
    {
        "id": "senior",
        "name": "Senior sécurisée",
        "icon": "🤝",
        "color": "senior",
        "desc": "Doux et progressif 10 min",
        "sequence": [
            {"ex": "ex1", "duration": 60,  "params": {"platform": "fixed"}},
            {"ex": "ex6", "duration": 120, "params": {"platform": "fixed", "point_mode": "lr", "point_speed": "low"}},
            {"ex": "ex8", "duration": 120, "params": {"platform": "fixed", "difficulty": "low"}},
        ]
    },
    {
        "id": "sport",
        "name": "Retour sport",
        "icon": "🏃",
        "color": "sport",
        "desc": "Dynamique et réactif 20 min",
        "sequence": [
            {"ex": "ex4",  "duration": 120, "params": {"amplitude": "high", "speed": "high"}},
            {"ex": "ex11", "duration": 180, "params": {"platform": "sinus", "difficulty": "high"}},
            {"ex": "ex10", "duration": 120, "params": {"platform": "auto", "difficulty": "high"}},
        ]
    },
    {
        "id": "cervical",
        "name": "Cervical",
        "icon": "🔄",
        "color": "cervical",
        "desc": "VOR + parcours 15 min",
        "sequence": [
            {"ex": "ex5",  "duration": 120, "params": {"platform": "fixed", "vor_mode": "random"}},
            {"ex": "ex10", "duration": 120, "params": {"platform": "fixed", "path": "infinity"}},
        ]
    },
]

@app.route("/presets", methods=["GET"])
def presets_get():
    with _data_lock:
        custom = _load_json(PRESETS_FILE)
    return _json_response(DEFAULT_PRESETS + custom)

@app.route("/presets", methods=["POST"])
def presets_create():
    body = _get_body()
    if not body.get("name"):
        return _json_response({"error": "name required"}, 400)
    if not body.get("id"):
        body["id"] = f"preset_{int(time.time()*1000)}"
    body["custom"] = True
    with _data_lock:
        presets = _load_json(PRESETS_FILE)
        presets.append(body)
        _save_json(PRESETS_FILE, presets)
    return _json_response(body, 201)

@app.route("/presets/<preset_id>", methods=["DELETE"])
def presets_delete(preset_id):
    # Only allow deleting custom presets
    with _data_lock:
        presets = _load_json(PRESETS_FILE)
        new_list = [p for p in presets if p.get("id") != preset_id]
        _save_json(PRESETS_FILE, new_list)
    return _json_response({"ok": True})


# =========================================================
# SYSTEM INFO API
# =========================================================
@app.route("/api/info")
def api_info():
    with lock:
        s = dict(latest)
    return _json_response({
        "version": "3.0-pwa",
        "timestamp": datetime.now().isoformat(),
        "platform": {
            "tare_ready": s.get("tare_ready", False),
            "offset_ready": s.get("offset_ready", False),
            "send_to_esp": s.get("send_to_esp", False),
            "cop_x_cm": round(s.get("cop_x_cm", 0.0), 3),
            "cop_y_cm": round(s.get("cop_y_cm", 0.0), 3),
            "cmd": round(s.get("cmd", 0.0), 3),
            "total": round(s.get("total", 0.0), 6),
        }
    })


# =========================================================
# ADAPTIVE DIFFICULTY HINT
# =========================================================
@app.route("/sessions/<session_id>/score", methods=["POST"])
def session_score_update(session_id):
    """Update live score for a session (used for adaptive difficulty)."""
    body = _get_body()
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
        for i, s in enumerate(sessions):
            if s.get("id") == session_id:
                sessions[i]["score"] = {**(s.get("score") or {}), **body}
                sessions[i]["updatedAt"] = datetime.now().isoformat()
                _save_json(SESSIONS_FILE, sessions)
                return _json_response(sessions[i])
    return _json_response({"error": "not found"}, 404)


# =========================================================
# SERVE STATIC FILES (ensure manifest/sw.js are accessible)
# =========================================================
@app.route("/static/<path:filename>")
def static_files(filename):
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return send_from_directory(static_dir, filename)


# =========================================================
# ENTRYPOINT
# =========================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  PosturoSPS PWA Server v3.0")
    print("  Routes ajoutées: /patients, /sessions, /presets, /api/info")
    print("  PWA: http://0.0.0.0:5000/")
    print("=" * 60)
    _orig_main()
