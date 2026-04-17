#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_pwa.py â€“ PosturoSPS PWA Server v3.1
Extends serverexercice8v3.py with:
  - PWA shell at / (static/index.html)
  - Rich HDMI display (static/hdmi.html) with premium visuals
  - Patient management API (/patients)
  - Session logging API (/sessions + export CSV/JSON)
  - Preset API (/presets)
  - Video management: dedicated videos/ directory, upload, MPV playback
  - SOT PDF: clean UTF-8 encoding, professional clinical layout
  - System info /api/info
Usage:
  python3 server_pwa.py [--uart /dev/ttyUSB0] [--port 5000] [--invert]
"""

import json
import os
import io
import csv
import math
import time
import subprocess
import threading
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe

from flask import request, Response, send_from_directory

# =========================================================
# Import the reference server (registers all Flask routes)
# =========================================================
import serverexercice8v3 as _srv
from serverexercice8v3 import app, main as _orig_main

# =========================================================
# PATHS
# =========================================================
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(_HERE, "data")
VIDEOS_DIR = os.path.join(_HERE, "videos")
STATIC_DIR = os.path.join(_HERE, "static")
os.makedirs(DATA_DIR,   exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)

PATIENTS_FILE = os.path.join(DATA_DIR, "patients.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
PRESETS_FILE  = os.path.join(DATA_DIR, "presets.json")

_data_lock = threading.Lock()

# =========================================================
# DATA HELPERS
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

def _json_resp(data, status=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        mimetype="application/json"
    )

def _body():
    try: return request.get_json(force=True) or {}
    except: return {}

# =========================================================
# OVERRIDE ROOT â†’ serve PWA index.html
# =========================================================
app.view_functions["index"] = lambda: send_from_directory(STATIC_DIR, "index.html")

# =========================================================
# OVERRIDE /hdmi â†’ serve premium hdmi.html
# =========================================================
app.view_functions["hdmi"] = lambda: send_from_directory(STATIC_DIR, "hdmi.html")

# =========================================================
# STATIC FILES
# =========================================================
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

# =========================================================
# VIDEO MANAGEMENT
# =========================================================
def list_all_videos():
    """List .mp4 files from both static/ and videos/ directories."""
    vids = set()
    for folder in [STATIC_DIR, VIDEOS_DIR]:
        try:
            for f in os.listdir(folder):
                if f.lower().endswith(".mp4") and os.path.isfile(os.path.join(folder, f)):
                    vids.add(f)
        except Exception:
            pass
    return sorted(vids)

def video_path(filename):
    """Resolve absolute path for a video filename (videos/ first, then static/)."""
    for folder in [VIDEOS_DIR, STATIC_DIR]:
        p = os.path.join(folder, os.path.basename(filename))
        if os.path.isfile(p):
            return p
    return None

# Patch list_static_videos in the original module so exercise12 picks up videos/ too
_srv.list_static_videos = list_all_videos

# Override existing /videos/list view function (already declared in serverexercice8v3)
def _videos_list_override():
    vids = list_all_videos()
    return _json_resp({"videos": vids, "count": len(vids)})
app.view_functions["videos_list_route"] = _videos_list_override

# Override Flask static file serving to also cover static/ (already handled by Flask,
# but we need send_from_directory for the /static prefix on older setups)
def _static_override(filename):
    return send_from_directory(STATIC_DIR, filename)
# Only override if a 'static' endpoint exists; Flask registers it automatically
if "static" in app.view_functions:
    app.view_functions["static"] = _static_override

@app.route("/videos/<path:filename>")
def videos_serve(filename):
    """Serve video files from the videos/ directory."""
    safe = os.path.basename(filename)
    p = video_path(safe)
    if not p:
        return "Not found", 404
    folder = os.path.dirname(p)
    return send_from_directory(folder, safe)

@app.route("/videos/upload", methods=["POST"])
def videos_upload():
    """Upload a video file to videos/ directory."""
    f = request.files.get("file")
    if not f or not f.filename:
        return _json_resp({"error": "no file"}, 400)
    safe = os.path.basename(f.filename)
    if not safe.lower().endswith(".mp4"):
        return _json_resp({"error": "only .mp4 allowed"}, 400)
    dest = os.path.join(VIDEOS_DIR, safe)
    f.save(dest)
    print(f"[VIDEO] Uploaded: {dest}")
    return _json_resp({"ok": True, "filename": safe, "videos": list_all_videos()})

@app.route("/videos/delete/<filename>", methods=["DELETE", "POST"])
def videos_delete(filename):
    """Delete a video from videos/ directory (not from static/)."""
    safe = os.path.basename(filename)
    p = os.path.join(VIDEOS_DIR, safe)
    if not os.path.isfile(p):
        return _json_resp({"error": "not found or not deletable"}, 404)
    os.remove(p)
    return _json_resp({"ok": True, "videos": list_all_videos()})

# ---- Video transcode (ffmpeg H.264 720p optimised for Pi) ----
_transcode_jobs = {}   # {job_id: {status, src, dst, progress, error}}
_transcode_lock = threading.Lock()

def _run_transcode(job_id, src_path, dst_path):
    """Background ffmpeg transcode thread."""
    with _transcode_lock:
        _transcode_jobs[job_id]["status"] = "running"
    try:
        cmd = [
            "ffmpeg", "-y", "-i", src_path,
            "-vcodec", "h264", "-profile:v", "baseline", "-level", "3.0",
            "-vf", "scale=1280:720",
            "-b:v", "1500k", "-maxrate", "1500k", "-bufsize", "3000k",
            "-acodec", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            dst_path
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            with _transcode_lock:
                _transcode_jobs[job_id]["status"] = "done"
                _transcode_jobs[job_id]["output"] = os.path.basename(dst_path)
        else:
            with _transcode_lock:
                _transcode_jobs[job_id]["status"] = "error"
                _transcode_jobs[job_id]["error"] = proc.stderr[-500:] if proc.stderr else "unknown"
    except subprocess.TimeoutExpired:
        with _transcode_lock:
            _transcode_jobs[job_id]["status"] = "error"
            _transcode_jobs[job_id]["error"] = "timeout (>10 min)"
    except FileNotFoundError:
        with _transcode_lock:
            _transcode_jobs[job_id]["status"] = "error"
            _transcode_jobs[job_id]["error"] = "ffmpeg not found â€“ sudo apt install ffmpeg"
    except Exception as e:
        with _transcode_lock:
            _transcode_jobs[job_id]["status"] = "error"
            _transcode_jobs[job_id]["error"] = str(e)

@app.route("/videos/transcode", methods=["POST"])
def videos_transcode():
    """Start ffmpeg transcode of a video. Body: {source, output_name}"""
    body = _body()
    source = os.path.basename(body.get("source", ""))
    output_name = os.path.basename(body.get("output_name", ""))
    if not source:
        return _json_resp({"error": "source required"}, 400)
    # Find source file
    src_path = video_path(source)
    if not src_path:
        return _json_resp({"error": f"source not found: {source}"}, 404)
    # Build output filename
    if not output_name:
        base = os.path.splitext(source)[0]
        output_name = f"{base}_720p.mp4"
    if not output_name.lower().endswith(".mp4"):
        output_name += ".mp4"
    dst_path = os.path.join(VIDEOS_DIR, output_name)
    # Check for already running job on same source
    with _transcode_lock:
        for jid, j in _transcode_jobs.items():
            if j.get("src") == src_path and j.get("status") == "running":
                return _json_resp({"error": "already transcoding", "job_id": jid})
    job_id = f"tc_{int(time.time()*1000)}"
    with _transcode_lock:
        _transcode_jobs[job_id] = {
            "status": "pending", "src": src_path,
            "dst": dst_path, "source": source, "output_name": output_name
        }
    threading.Thread(target=_run_transcode, args=(job_id, src_path, dst_path),
                     daemon=True).start()
    return _json_resp({"ok": True, "job_id": job_id, "output_name": output_name})

@app.route("/videos/transcode-status")
def videos_transcode_status():
    with _transcode_lock:
        return _json_resp(dict(_transcode_jobs))

# =========================================================
# MPV PLAYER (for Ex12 video on the Pi screen)
# =========================================================
_mpv_proc = None
_mpv_lock = threading.Lock()

_XDISP = {"DISPLAY": ":0"}

def _chromium_hide():
    """Minimize Chromium so MPV fullscreen is visible."""
    env = os.environ.copy(); env.update(_XDISP)
    for cmd in [
        ["xdotool", "search", "--class", "chromium", "windowminimize"],
        ["xdotool", "search", "--class", "Chromium", "windowminimize"],
        ["wmctrl", "-r", "Chromium", "-b", "add,hidden"],
    ]:
        try:
            subprocess.run(cmd, env=env, timeout=2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("[MPV] Chromium minimized"); return
        except Exception:
            continue

def _chromium_restore():
    """Raise Chromium after MPV stops."""
    env = os.environ.copy(); env.update(_XDISP)
    for cmd in [
        ["xdotool", "search", "--class", "chromium", "windowmap", "windowraise"],
        ["xdotool", "search", "--class", "Chromium", "windowmap", "windowraise"],
        ["wmctrl", "-r", "Chromium", "-b", "remove,hidden"],
        ["wmctrl", "-a", "Chromium"],
    ]:
        try:
            subprocess.run(cmd, env=env, timeout=2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("[MPV] Chromium restored"); return
        except Exception:
            continue

def _mpv_stop(restore_chromium=True):
    global _mpv_proc
    with _mpv_lock:
        if _mpv_proc and _mpv_proc.poll() is None:
            _mpv_proc.terminate()
            try:
                _mpv_proc.wait(timeout=3)
            except Exception:
                _mpv_proc.kill()
        _mpv_proc = None
    if restore_chromium:
        _chromium_restore()
    print("[MPV] Stopped")

def _mpv_play(filepath, loop=True):
    global _mpv_proc
    _mpv_stop(restore_chromium=False)   # stop old process; don't restore yet
    if not filepath or not os.path.isfile(filepath):
        print(f"[MPV] File not found: {filepath}")
        return False
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    cmd = [
        "mpv",
        "--fullscreen",
        "--ontop",          # stay above Chromium in case xdotool unavailable
        "--no-osc",
        "--no-border",
        "--quiet",
        "--really-quiet",
        "--no-terminal",
        "--video-aspect-override=16:9",
    ]
    if loop:
        cmd.append("--loop=inf")
    cmd.append(filepath)
    _chromium_hide()        # hide Chromium before launching MPV
    try:
        with _mpv_lock:
            _mpv_proc = subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        print(f"[MPV] Playing: {filepath}")
        return True
    except FileNotFoundError:
        print("[MPV] mpv not found â€“ install with: sudo apt install mpv")
        return False
    except Exception as e:
        print(f"[MPV] Error: {e}")
        return False

@app.route("/mpv/stop", methods=["GET", "POST"])
def mpv_stop_route():
    _mpv_stop()
    return _json_resp({"ok": True})

@app.route("/mpv/play")
def mpv_play_route():
    filename = request.args.get("file", "")
    loop = request.args.get("loop", "1") != "0"
    p = video_path(filename)
    if not p:
        return _json_resp({"error": "video not found"}, 404)
    ok = _mpv_play(p, loop=loop)
    return _json_resp({"ok": ok, "file": filename})

# =========================================================
# EXERCISE 12 â€“ video via Chromium (original behaviour)
# + scan videos/ directory so new files are picked up
# =========================================================
# Patch list_static_videos so ex12 also sees videos/ dir
_srv.list_static_videos = list_all_videos

# NOTE: ex12 start/stop are NOT overridden â€“ use original Chromium-based playback.

# Patch ensure_chromium: GPU flags + NEVER THROW (critical for SOT stability)
def _ensure_chromium_safe():
    """Launch Chromium with GPU flags. Never raises â€“ errors are logged only."""
    if _srv.opto_process is not None and _srv.opto_process.poll() is None:
        return  # already running
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    gpu_flags = [
        "--kiosk", "--noerrdialogs", "--disable-infobars",
        "--disable-restore-session-state", "--no-first-run",
        "--enable-gpu-rasterization", "--enable-zero-copy",
        "--use-gl=egl", "--ignore-gpu-blocklist",
        # NOTE: do NOT add --disable-software-rasterizer â€“ if EGL/GPU is
        # unavailable Chromium needs the software fallback, otherwise
        # rendering becomes broken / extremely slow.
        "--enable-accelerated-video-decode",
        "--enable-features=VaapiVideoDecoder",
        "http://localhost:5000/hdmi"
    ]
    for binary in ["chromium", "chromium-browser"]:
        try:
            _srv.opto_process = subprocess.Popen([binary] + gpu_flags, env=env)
            print(f"[HDMI] Chromium launched via '{binary}'")
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"[HDMI] {binary} launch error: {e}")
            return
    print("[HDMI] Chromium not found â€“ HDMI display unavailable")

_srv.ensure_chromium = _ensure_chromium_safe

# =========================================================
# SOT â€“ Robust dedicated logging thread
# =========================================================
# Root-cause: the control loop has TWO early-continue guards
# that silently skip the logging section:
#   1. if (not tare_ready) or (not offset_ready): continue
#   2. if total < TOTAL_MIN: continue
# Both are bypassed by our dedicated thread which reads
# directly from _srv.cop_x_f / cop_y_f / latest â€“ completely
# independent from the control loop.
# =========================================================

_sot_orig_total_min = _srv.TOTAL_MIN
_sot_bg_stop  = threading.Event()
_sot_bg_path  = None   # path of the CSV being recorded
_sot_diag_lock = threading.Lock()
_sot_rows_by_condition = {c: 0 for c in range(1, 7)}
_sot_total_rows = 0
_sot_last_row_ts = 0.0


def _reset_sot_row_counters():
    global _sot_rows_by_condition, _sot_total_rows, _sot_last_row_ts
    with _sot_diag_lock:
        _sot_rows_by_condition = {c: 0 for c in range(1, 7)}
        _sot_total_rows = 0
        _sot_last_row_ts = 0.0


def _sot_rows_for_condition(cond_id):
    try:
        c = int(cond_id)
    except Exception:
        c = 0
    with _sot_diag_lock:
        return int(_sot_rows_by_condition.get(c, 0))


def _sot_rows_snapshot():
    with _sot_diag_lock:
        return {str(c): int(n) for c, n in _sot_rows_by_condition.items() if int(n) > 0}


def _sot_expected_min_rows(cond_id):
    try:
        c = int(cond_id)
    except Exception:
        c = 0
    duration = float(_srv.SOT_CONDITIONS.get(c, {}).get("duration", 20))
    # 5 Hz minimum accepted for robust analysis (logger runs around 50 Hz nominally).
    return max(50, int(duration * 5.0))


def _sot_check_condition_ready(cond_id):
    cond = _srv.SOT_CONDITIONS.get(int(cond_id), {})
    duration = float(cond.get("duration", 0))
    elapsed = max(0.0, float(time.time() - _srv.sot_start_time)) if _srv.sot_start_time > 0 else 0.0
    remaining = max(0.0, duration - elapsed)
    rows = _sot_rows_for_condition(cond_id)
    min_rows = _sot_expected_min_rows(cond_id)
    if elapsed + 0.25 < duration:
        return False, (
            f"WAIT: CONDITION {cond_id} ({cond.get('name', '')}) - "
            f"{remaining:.1f}s restantes\n"
        )
    if rows < min_rows:
        return False, (
            f"WAIT: CONDITION {cond_id} - acquisition insuffisante "
            f"({rows}/{min_rows} echantillons)\n"
        )
    return True, ""


def _sot_bg_logger(path, stop_evt):
    """Dedicated 50 Hz SOT logger â€“ writes directly from _srv globals.
    Completely bypasses the control-loop's logging section."""
    global _sot_total_rows, _sot_last_row_ts
    import csv as _csv
    rows_written = 0
    try:
        with open(path, "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["time", "condition", "cop_x_cm", "cop_y_cm",
                        "total", "cmd", "esp_pos", "blocked"])
            while not stop_evt.is_set():
                try:
                    t    = time.time()
                    cond = _srv.current_condition
                    # Read COP directly (set even when total < TOTAL_MIN via EMA decay)
                    cx   = float(_srv.cop_x_f)
                    cy   = float(_srv.cop_y_f)
                    with _srv.lock:
                        tot = float(_srv.latest.get("total", 0.0))
                        cmd = float(_srv.latest.get("cmd",   0.0))
                    esp  = _srv.esp_pos
                    w.writerow([round(t, 4), cond,
                                round(cx, 4), round(cy, 4),
                                round(tot, 6), round(cmd, 4),
                                esp, ""])
                    rows_written += 1
                    try:
                        cond_i = int(cond)
                    except Exception:
                        cond_i = 0
                    with _sot_diag_lock:
                        _sot_total_rows += 1
                        _sot_last_row_ts = t
                        if 1 <= cond_i <= 6:
                            _sot_rows_by_condition[cond_i] = _sot_rows_by_condition.get(cond_i, 0) + 1
                    if rows_written % 250 == 0:   # flush every 5 s
                        f.flush()
                except Exception as _e:
                    print(f"[SOT LOG] row error: {_e}")
                time.sleep(0.02)   # 50 Hz
            f.flush()
        print(f"[SOT LOG] Finished â€“ {rows_written} rows â†’ {path}")
    except Exception as e:
        print(f"[SOT LOG] fatal: {e}")


def _sot_bg_start(path):
    global _sot_bg_path
    _sot_bg_path = path
    _sot_bg_stop.clear()
    t = threading.Thread(target=_sot_bg_logger,
                         args=(path, _sot_bg_stop),
                         daemon=True, name="sot-bg-log")
    t.start()


def _sot_bg_finish():
    """Stop the background logger and wait up to 3 s for it to flush."""
    _sot_bg_stop.set()
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not any(th.name == "sot-bg-log" for th in threading.enumerate()):
            break
        time.sleep(0.05)


# ---- SOT route overrides ----

def _patched_sot_start(c):
    if c not in _srv.SOT_CONDITIONS:
        return f"ERROR: invalid condition {c}\n"
    _srv.TOTAL_MIN = -1.0   # extra: bypass control-loop TOTAL_MIN guard too

    # Ensure calibration flags are set so the control loop also computes COP
    if not _srv.tare_ready:
        print("[SOT] tare not done â€“ running auto-tare now")
        _srv.tare()
    if not _srv.offset_ready:
        print("[SOT] center not done â€“ assuming (0,0) offset")
        _srv.offset_x_cm = 0.0
        _srv.offset_y_cm = 0.0
        _srv.offset_ready = True
        with _srv.lock:
            _srv.latest["offset_ready"] = True

    # Stop any previous logger thread BEFORE changing the condition
    _sot_bg_finish()
    _reset_sot_row_counters()

    # Set the condition BEFORE starting the logger so the very first row
    # already has the correct condition value (avoids condition=N-1 contamination)
    _srv.start_condition(c)

    # Open the log file ourselves (do NOT rely on control-loop start_log)
    os.makedirs("logs", exist_ok=True)
    log_path = datetime.now().strftime("logs/sot_%Y%m%d_%H%M%S.csv")
    _srv.current_log_path = log_path   # finalize_sot_and_analyze reads this
    _srv.logging_active   = True       # must be True for finalize to proceed
    _srv.log_file         = None       # prevent control loop from writing
    _srv.log_writer       = None       # (if log_writer is None, control loop skips write)

    # Start dedicated background logger (condition already correct)
    _sot_bg_start(log_path)

    print(f"[SOT] Condition {c} started â€“ logging to {log_path} "
          f"(tare_ready={_srv.tare_ready}, offset_ready={_srv.offset_ready})")
    return f"STARTED CONDITION {c}\n"


def _patched_sot_stop():
    _srv.stop_condition()
    _srv.TOTAL_MIN = _sot_orig_total_min
    _sot_bg_finish()           # wait for last rows + flush
    _srv.logging_active = False
    _srv.finalize_sot_and_analyze()
    return "STOP\n"


def _patched_sot_next():
    current = int(_srv.sot_condition)
    if current in _srv.SOT_CONDITIONS:
        ready, wait_msg = _sot_check_condition_ready(current)
        if not ready:
            return wait_msg
    _srv.sot_condition += 1
    if _srv.sot_condition > 6:
        _srv.TOTAL_MIN = _sot_orig_total_min
        _srv.stop_condition()
        _sot_bg_finish()
        _srv.logging_active = False
        _srv.finalize_sot_and_analyze()
        return "SOT FINISHED\n"
    # Set new condition BEFORE stop so logger has zero gap with wrong condition
    _srv.start_condition(_srv.sot_condition)
    return f"NEXT: CONDITION {_srv.sot_condition}\n"


def _patched_sot_restart():
    # Same condition â€“ keep logging to the same file, just reset platform
    _srv.start_condition(_srv.sot_condition)
    return f"RESTART CONDITION {_srv.sot_condition}\n"


app.view_functions["sot_start"]   = _patched_sot_start
app.view_functions["sot_stop"]    = _patched_sot_stop
app.view_functions["sot_next"]    = _patched_sot_next
app.view_functions["sot_restart"] = _patched_sot_restart

# Checks if current condition met its minimum time WITHOUT advancing or moving platform.
# Used by the foam modal so the platform stays still until after the tare.
@app.route("/sot/check_ready")
def sot_check_ready():
    current = int(_srv.sot_condition)
    if current in _srv.SOT_CONDITIONS:
        ready, wait_msg = _sot_check_condition_ready(current)
        if not ready:
            return wait_msg
    return "READY\n"

@app.route("/sot/foam_tare")
def sot_foam_tare():
    """Retare for foam transition (between C3 and C4) with safety checks.
    The tare must be performed with foam on the platform and patient OFF.
    """
    # During C3->C4 transition, current tare still corresponds to "no foam".
    # So if patient is still on the platform, total load is clearly above this threshold.
    load_guard = max(0.001, float(_sot_orig_total_min) * 5.0)
    try:
        current_total = float(_srv.latest.get("total", 0.0))
    except Exception:
        current_total = 0.0
    if current_total > load_guard:
        return (
            f"ERROR: charge detectee ({current_total:.6f}). "
            "Descendez le patient puis refaites la tare.\n"
        )
    _srv.tare()
    return "OK TARE FOAM\n"

# ---- Patient info for SOT report ----
_sot_patient = {}   # set by /sot/patient before starting

@app.route("/sot/patient", methods=["POST"])
def sot_patient_set():
    global _sot_patient
    _sot_patient = _body()
    return _json_resp({"ok": True})


@app.route("/sot/state")
def sot_state_debug():
    """Real-time diagnostic endpoint â€“ shows all SOT-relevant state."""
    try:
        log_rows = 0
        if _sot_bg_path and os.path.isfile(_sot_bg_path):
            with open(_sot_bg_path) as _f:
                log_rows = max(0, sum(1 for _ in _f) - 1)  # exclude header
    except Exception:
        log_rows = -1
    return _json_resp({
        "tare_ready":       _srv.tare_ready,
        "offset_ready":     _srv.offset_ready,
        "logging_active":   _srv.logging_active,
        "log_writer_ok":    _srv.log_writer is not None,
        "current_log_path": _srv.current_log_path,
        "bg_log_path":      _sot_bg_path,
        "bg_log_running":   any(t.name == "sot-bg-log" for t in threading.enumerate()),
        "bg_log_rows":      log_rows,
        "bg_total_rows_runtime": int(_sot_total_rows),
        "bg_last_row_ts":   round(_sot_last_row_ts, 3) if _sot_last_row_ts else 0,
        "rows_by_condition": _sot_rows_snapshot(),
        "rows_current_condition": _sot_rows_for_condition(_srv.current_condition),
        "TOTAL_MIN":        _srv.TOTAL_MIN,
        "current_condition": _srv.current_condition,
        "sot_condition":    _srv.sot_condition,
        "cop_x_f":          round(_srv.cop_x_f, 3),
        "cop_y_f":          round(_srv.cop_y_f, 3),
        "total":            round(_srv.latest.get("total", 0.0), 6),
    })

# =========================================================
# SOT PDF â€“ Clean rebuild with proper French encoding
# =========================================================
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm as RL_CM
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, Image as RLImage,
                                    HRFlowable)
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _RL_OK = True
except ImportError:
    _RL_OK = False

def _register_unicode_font():
    """Register a Unicode-capable font for French characters."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                name = os.path.splitext(os.path.basename(path))[0].replace("-", "")
                pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:
                pass
    return None  # fallback to built-in Helvetica

_FONT_NAME = None
_FONT_BOLD = None

def _get_fonts():
    global _FONT_NAME, _FONT_BOLD
    if _FONT_NAME:
        return _FONT_NAME, _FONT_BOLD
    # Try DejaVu
    reg = _register_unicode_font()
    if reg:
        _FONT_NAME = "DejaVuSans"
        _FONT_BOLD = "DejaVuSans"
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
            _FONT_NAME = "DejaVuSans"
            _FONT_BOLD = "DejaVuSans-Bold"
        except Exception:
            _FONT_NAME = "Helvetica"
            _FONT_BOLD = "Helvetica-Bold"
    else:
        _FONT_NAME = "Helvetica"
        _FONT_BOLD = "Helvetica-Bold"
    return _FONT_NAME, _FONT_BOLD


def _build_sot_pdf(pdf_path, source_csv, results_by_c, img_paths,
                   patient_info=None, ces=None, debug_info=None):
    if not _RL_OK:
        print("[PDF] ReportLab not available")
        return

    fn, fb = _get_fonts()
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=1.2 * RL_CM,
        rightMargin=1.2 * RL_CM,
        topMargin=1.0 * RL_CM,
        bottomMargin=1.0 * RL_CM,
        title="Rapport SOT",
        author="PosturoSPS",
    )
    story = []
    W_avail = A4[0] - 2.4 * RL_CM

    style_info = ParagraphStyle(
        "sot_info",
        fontName=fn,
        fontSize=9.5,
        textColor=colors.HexColor("#1e293b"),
        leading=13,
        alignment=TA_LEFT,
    )
    style_title_block = ParagraphStyle(
        "sot_title_block",
        fontName=fb,
        fontSize=15,
        textColor=colors.HexColor("#0f172a"),
        leading=19,
        alignment=TA_LEFT,
    )
    style_panel_title = ParagraphStyle(
        "sot_panel_title",
        fontName=fb,
        fontSize=16,
        textColor=colors.HexColor("#1d4ed8"),
        alignment=TA_CENTER,
        spaceAfter=0.15 * RL_CM,
    )
    style_panel_title_warn = ParagraphStyle(
        "sot_panel_title_warn",
        fontName=fb,
        fontSize=16,
        textColor=colors.HexColor("#9333ea"),
        alignment=TA_CENTER,
        spaceAfter=0.15 * RL_CM,
    )
    style_card_title = ParagraphStyle(
        "sot_card_title",
        fontName=fb,
        fontSize=9.5,
        textColor=colors.HexColor("#0f172a"),
        alignment=TA_CENTER,
        leading=11,
        spaceAfter=0.08 * RL_CM,
    )
    style_card_metric = ParagraphStyle(
        "sot_card_metric",
        fontName=fb,
        fontSize=11,
        textColor=colors.HexColor("#111827"),
        alignment=TA_CENTER,
        leading=12,
        spaceAfter=0.04 * RL_CM,
    )
    style_card_small = ParagraphStyle(
        "sot_card_small",
        fontName=fn,
        fontSize=8.3,
        textColor=colors.HexColor("#334155"),
        alignment=TA_CENTER,
        leading=10,
        spaceAfter=0.02 * RL_CM,
    )
    style_card_warn = ParagraphStyle(
        "sot_card_warn",
        fontName=fb,
        fontSize=8.4,
        textColor=colors.HexColor("#b91c1c"),
        alignment=TA_CENTER,
        leading=10,
        spaceAfter=0.02 * RL_CM,
    )
    style_h2 = ParagraphStyle(
        "sot_h2",
        fontName=fb,
        fontSize=12,
        textColor=colors.HexColor("#0f172a"),
        alignment=TA_LEFT,
        spaceBefore=0.28 * RL_CM,
        spaceAfter=0.14 * RL_CM,
    )
    style_diag = ParagraphStyle(
        "sot_diag",
        fontName=fn,
        fontSize=9,
        textColor=colors.HexColor("#7f1d1d"),
        alignment=TA_LEFT,
        leading=12,
        spaceAfter=0.12 * RL_CM,
    )
    style_diag_ok = ParagraphStyle(
        "sot_diag_ok",
        fontName=fn,
        fontSize=9,
        textColor=colors.HexColor("#14532d"),
        alignment=TA_LEFT,
        leading=12,
        spaceAfter=0.12 * RL_CM,
    )

    cond_labels = {
        1: "Yeux ouverts",
        2: "Yeux fermes",
        3: "Optocinetique",
        4: "Yeux ouverts",
        5: "Yeux fermes",
        6: "Optocinetique",
    }
    cond_letters = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F"}

    def _float_val(v, default=0.0):
        try:
            return float(v)
        except Exception:
            return default

    def _instability_index(res):
        if not res or "error" in res:
            return None
        stab = _float_val(res.get("stability_pct"), 0.0)
        idx = (100.0 - stab) / 18.0
        return max(0.0, min(6.0, idx))

    def _result_ok(cond_id):
        r = results_by_c.get(cond_id)
        return r if (r and "error" not in r) else None

    def _build_condition_card(cond_id, card_width):
        r = results_by_c.get(cond_id)
        img_path = img_paths.get(cond_id)
        img_size = min(card_width * 0.78, 3.2 * RL_CM)
        parts = [
            Paragraph(f"{cond_labels.get(cond_id, 'Condition')}<br/>C{cond_id}", style_card_title),
        ]
        if img_path and os.path.isfile(img_path):
            parts.append(RLImage(img_path, width=img_size, height=img_size))
        else:
            parts.append(Spacer(1, 0.1 * RL_CM))
            parts.append(Paragraph("Trace COP indisponible", style_card_small))
            parts.append(Spacer(1, max(0.25 * RL_CM, img_size - 0.3 * RL_CM)))

        if r and "error" not in r:
            idx = _instability_index(r)
            parts.append(Spacer(1, 0.08 * RL_CM))
            parts.append(Paragraph(f"Indice {cond_letters.get(cond_id, '')}: <b>{idx:.2f}</b>", style_card_metric))
            parts.append(Paragraph(f"Stabilite: {_float_val(r.get('stability_pct')):.1f} %", style_card_small))
            parts.append(Paragraph(f"Surface: {_float_val(r.get('ellipse95_area_cm2')):.2f} cm2", style_card_small))
            parts.append(Paragraph(f"Vitesse: {_float_val(r.get('mean_speed_cm_s')) * 10.0:.1f} mm/s", style_card_small))
        elif r and "error" in r:
            parts.append(Spacer(1, 0.1 * RL_CM))
            parts.append(Paragraph("Donnees insuffisantes", style_card_warn))
            parts.append(Paragraph(f"N = {r.get('n', '-')}", style_card_small))
        else:
            parts.append(Spacer(1, 0.1 * RL_CM))
            parts.append(Paragraph("Condition non enregistree", style_card_warn))

        card = Table([[parts]], colWidths=[card_width])
        card.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return card

    pi = patient_info or {}
    patient_name = f"{pi.get('prenom', '')} {pi.get('nom', '')}".strip() or "-"
    patient_age = f"{pi.get('age', '')} ans" if pi.get("age") else "-"
    prescriber = pi.get("medecin") or pi.get("prescripteur") or "-"
    objective = pi.get("objectif") or "-"
    session_date = pi.get("date") or datetime.now().strftime("%d/%m/%Y")
    ces_text = f"{_float_val(ces):.1f} %" if ces is not None else "-"

    left_txt = (
        "<b>Informations de test</b><br/>"
        f"Patient : {patient_name}<br/>"
        f"Age : {patient_age}<br/>"
        f"Medecin prescripteur : {prescriber}<br/>"
        f"Seance du : {session_date}<br/>"
        f"Objectif : {objective}<br/>"
        f"CSV : {os.path.basename(source_csv)}"
    )
    right_txt = (
        "<b>MULTITEST BALANCE CONTROL</b><br/>"
        "Mesures d'equilibre sur plateforme<br/>"
        "statique et dynamique<br/><br/>"
        f"<b>CES global : {ces_text}</b>"
    )
    top = Table(
        [[Paragraph(left_txt, style_info), Paragraph(right_txt, style_title_block)]],
        colWidths=[W_avail * 0.56, W_avail * 0.44],
    )
    top.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.9, colors.HexColor("#94a3b8")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(top)
    story.append(Spacer(1, 0.22 * RL_CM))

    card_w = (W_avail - 0.5 * RL_CM) / 3.0

    story.append(Paragraph("Stable", style_panel_title))
    stable_cards = [_build_condition_card(c, card_w) for c in (1, 2, 3)]
    stable_tbl = Table([stable_cards], colWidths=[card_w, card_w, card_w])
    stable_tbl.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(stable_tbl)
    story.append(Spacer(1, 0.18 * RL_CM))

    story.append(Paragraph("Instable", style_panel_title_warn))
    unstable_cards = [_build_condition_card(c, card_w) for c in (4, 5, 6)]
    unstable_tbl = Table([unstable_cards], colWidths=[card_w, card_w, card_w])
    unstable_tbl.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(unstable_tbl)

    ratios = {}
    try:
        ratios = _srv.compute_sensory_ratios(results_by_c) or {}
    except Exception:
        ratios = {}

    story.append(Paragraph("Synthese sensorielle", style_h2))
    ratio_labels = ["SOM", "VIS", "VEST", "DEP.V"]
    ratio_keys = ["SOMES", "VISIO", "VEST", "PREF_VIS"]
    ratio_vals = []
    ratio_colors = []
    for rk in ratio_keys:
        rv = ratios.get(rk)
        if rv is None:
            ratio_vals.append(np.nan)
            ratio_colors.append("#cbd5e1")
            continue
        pct = float(rv) * 100.0
        if rk == "PREF_VIS":
            pct = min(180.0, max(0.0, pct))
            color = "#16a34a" if pct <= 100 else ("#eab308" if pct <= 120 else "#ef4444")
        else:
            pct = min(120.0, max(0.0, pct))
            color = "#16a34a" if pct >= 80 else ("#eab308" if pct >= 60 else "#ef4444")
        ratio_vals.append(pct)
        ratio_colors.append(color)

    if any(not np.isnan(v) for v in ratio_vals):
        synth_img = os.path.join(os.path.dirname(pdf_path), "sot_synthese.png")
        fig, ax = plt.subplots(figsize=(6.6, 2.4))
        plot_vals = [0.0 if np.isnan(v) else v for v in ratio_vals]
        bars = ax.bar(ratio_labels, plot_vals, color=ratio_colors, edgecolor="#334155", linewidth=0.6)
        ax.set_ylim(0, 130)
        ax.axhline(80, color="#16a34a", linestyle="--", linewidth=0.9, alpha=0.8)
        ax.set_ylabel("%")
        ax.set_facecolor("#f8fafc")
        for i, (bar, val) in enumerate(zip(bars, ratio_vals)):
            if np.isnan(val):
                ax.text(i, 4, "NA", ha="center", va="bottom", fontsize=8, color="#64748b")
            else:
                ax.text(bar.get_x() + bar.get_width() / 2.0, bar.get_height() + 2.0,
                        f"{val:.0f}%", ha="center", va="bottom", fontsize=8, fontweight="bold")
        fig.patch.set_facecolor("#ffffff")
        plt.tight_layout()
        plt.savefig(synth_img, dpi=150, bbox_inches="tight")
        plt.close(fig)
        if os.path.isfile(synth_img):
            story.append(RLImage(synth_img, width=W_avail * 0.52, height=5.0 * RL_CM))

    story.append(Paragraph("Taux de stabilite (Stable vs Instable)", style_h2))
    pairs = [
        ("Yeux ouverts", 1, 4),
        ("Yeux fermes", 2, 5),
        ("Optocinetique", 3, 6),
    ]
    pair_labels = []
    pair_stable = []
    pair_unstable = []
    for lbl, c_stable, c_unstable in pairs:
        r_stable = _result_ok(c_stable)
        r_unstable = _result_ok(c_unstable)
        if not r_stable or not r_unstable:
            continue
        pair_labels.append(lbl)
        pair_stable.append(_float_val(r_stable.get("stability_pct")))
        pair_unstable.append(_float_val(r_unstable.get("stability_pct")))

    if pair_labels:
        stability_img = os.path.join(os.path.dirname(pdf_path), "sot_stability_pairs.png")
        x = np.arange(len(pair_labels))
        w = 0.34
        fig2, ax2 = plt.subplots(figsize=(6.8, 2.6))
        b1 = ax2.bar(x - w / 2, pair_stable, w, label="Stable", color="#0ea5e9")
        b2 = ax2.bar(x + w / 2, pair_unstable, w, label="Instable", color="#ef4444")
        ax2.set_ylim(0, 105)
        ax2.set_ylabel("%")
        ax2.set_xticks(x)
        ax2.set_xticklabels(pair_labels, fontsize=8)
        ax2.axhline(75, color="#16a34a", linestyle="--", linewidth=0.9, alpha=0.7)
        ax2.legend(loc="upper right", fontsize=8)
        ax2.set_facecolor("#f8fafc")
        for bars in (b1, b2):
            for bar in bars:
                h = bar.get_height()
                ax2.text(bar.get_x() + bar.get_width() / 2.0, h + 1.5,
                         f"{h:.0f}", ha="center", va="bottom", fontsize=8)
        fig2.patch.set_facecolor("#ffffff")
        plt.tight_layout()
        plt.savefig(stability_img, dpi=150, bbox_inches="tight")
        plt.close(fig2)
        if os.path.isfile(stability_img):
            story.append(RLImage(stability_img, width=W_avail * 0.75, height=4.8 * RL_CM))

    story.append(Paragraph("Surfaces / Vitesses", style_h2))
    sv_rows = [[
        Paragraph("<b>Modalite</b>", style_card_small),
        Paragraph("<b>Stable</b>", style_card_small),
        Paragraph("<b>Instable</b>", style_card_small),
    ]]
    for lbl, c_stable, c_unstable in pairs:
        rs = _result_ok(c_stable)
        ru = _result_ok(c_unstable)
        if not rs or not ru:
            continue
        left = (
            f"{_float_val(rs.get('ellipse95_area_cm2')):.1f} cm2"
            f" / {_float_val(rs.get('mean_speed_cm_s')) * 10.0:.0f} mm/s"
        )
        right = (
            f"{_float_val(ru.get('ellipse95_area_cm2')):.1f} cm2"
            f" / {_float_val(ru.get('mean_speed_cm_s')) * 10.0:.0f} mm/s"
        )
        sv_rows.append([lbl, left, right])
    if len(sv_rows) == 1:
        sv_rows.append(["Aucune paire complete", "-", "-"])
    sv_tbl = Table(sv_rows, colWidths=[W_avail * 0.30, W_avail * 0.35, W_avail * 0.35])
    sv_tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
        ("FONTNAME", (0, 0), (-1, 0), fb),
        ("FONTNAME", (0, 1), (-1, -1), fn),
        ("FONTSIZE", (0, 0), (-1, -1), 8.6),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
    ]))
    story.append(sv_tbl)

    story.append(Paragraph("Resultats detailles", style_h2))
    det_rows = [[
        Paragraph("<b>Cond.</b>", style_card_small),
        Paragraph("<b>Nom</b>", style_card_small),
        Paragraph("<b>N</b>", style_card_small),
        Paragraph("<b>Stabilite %</b>", style_card_small),
        Paragraph("<b>Surface cm2</b>", style_card_small),
        Paragraph("<b>Vitesse mm/s</b>", style_card_small),
    ]]
    for c in range(1, 7):
        r = results_by_c.get(c)
        proto_name = _srv.SOT_PROTOCOL.get(c, {}).get("name", "")
        if not r:
            det_rows.append([f"C{c}", proto_name, "-", "-", "-", "-"])
            continue
        if "error" in r:
            det_rows.append([f"C{c}", proto_name, str(r.get("n", "-")), "Donnees insuffisantes", "-", "-"])
            continue
        det_rows.append([
            f"C{c}",
            proto_name,
            str(r.get("n", "-")),
            f"{_float_val(r.get('stability_pct')):.1f}",
            f"{_float_val(r.get('ellipse95_area_cm2')):.2f}",
            f"{_float_val(r.get('mean_speed_cm_s')) * 10.0:.1f}",
        ])
    det_tbl = Table(
        det_rows,
        colWidths=[W_avail * 0.09, W_avail * 0.27, W_avail * 0.08, W_avail * 0.19, W_avail * 0.18, W_avail * 0.19],
    )
    det_tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1d4ed8")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), fb),
        ("FONTNAME", (0, 1), (-1, -1), fn),
        ("FONTSIZE", (0, 0), (-1, -1), 8.4),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
    ]))
    story.append(det_tbl)

    if debug_info:
        csv_rows = int(debug_info.get("csv_rows", 0) or 0)
        cond_dist = debug_info.get("csv_conditions", {}) or {}
        missing = debug_info.get("missing_conditions", []) or []
        runtime_rows = debug_info.get("runtime_rows_by_condition", {}) or {}
        error_count = len([1 for r in results_by_c.values() if "error" in r])
        if missing or csv_rows < 300 or error_count > 0:
            story.append(Paragraph("Diagnostic acquisition", style_h2))
            msg = (
                f"CSV lignes: {csv_rows}. "
                f"Conditions detectees: {cond_dist}. "
                f"Compteur temps reel: {runtime_rows}. "
                f"Conditions manquantes: {missing if missing else 'aucune'}."
            )
            story.append(Paragraph(msg, style_diag))
        else:
            story.append(Paragraph("Diagnostic acquisition", style_h2))
            story.append(Paragraph(
                f"Acquisition complete ({csv_rows} lignes, conditions {cond_dist}).",
                style_diag_ok,
            ))

    story.append(Spacer(1, 0.18 * RL_CM))
    story.append(HRFlowable(
        width="100%",
        thickness=1,
        color=colors.HexColor("#cbd5e1"),
        spaceBefore=3,
        spaceAfter=3,
    ))
    story.append(Paragraph(
        "PosturoSPS | Rapport SOT genere automatiquement",
        ParagraphStyle("sot_footer", fontName=fn, fontSize=8, textColor=colors.HexColor("#64748b"), alignment=TA_CENTER),
    ))

    doc.build(story)
    print(f"[PDF] Built: {pdf_path}")

# Monkey-patch the PDF builder in the original module
_srv.build_multitest_like_pdf = _build_sot_pdf

# =========================================================
# PATCHED analyze_sot_csv â€“ better type handling + debug info
# =========================================================
def _patched_analyze_sot_csv(csv_path):
    """Drop-in replacement with robust condition type handling."""
    import pandas as pd
    df = pd.read_csv(csv_path)
    required = {"time", "condition", "cop_x_cm", "cop_y_cm"}
    if not required.issubset(set(df.columns)):
        raise RuntimeError("CSV colonnes manquantes")

    # Normalize condition column to int:
    # supports numeric values, floats-as-strings, and labels like "C6".
    cond_num = pd.to_numeric(df["condition"], errors="coerce")
    if cond_num.isna().any():
        extracted = df["condition"].astype(str).str.extract(r"(\d+)")[0]
        cond_num = cond_num.fillna(pd.to_numeric(extracted, errors="coerce"))
    df["condition"] = cond_num.fillna(0).astype(int)

    csv_rows = len(df)
    cond_dist = {str(k): int(v) for k, v in df["condition"].value_counts().items()} if csv_rows else {}
    cond_present = sorted({int(c) for c in df["condition"].unique() if 1 <= int(c) <= 6})
    missing_conditions = [c for c in range(1, 7) if c not in cond_present]
    runtime_rows = _sot_rows_snapshot()
    print(f"[ANALYZE] CSV rows={csv_rows} condition distribution={cond_dist}")
    if missing_conditions:
        print(f"[ANALYZE] WARNING missing conditions: {missing_conditions}")

    base = os.path.splitext(os.path.basename(csv_path))[0]
    out_dir = os.path.join(os.path.dirname(csv_path), base + "_results")
    os.makedirs(out_dir, exist_ok=True)

    results_by_c = {}
    img_paths = {}
    for c in range(1, 7):
        dfc = df[df["condition"] == c]
        if dfc.empty:
            continue
        res, win = _srv.analyze_one_condition(dfc, c)
        if res is None:
            continue
        results_by_c[c] = res
        if win is not None and len(win) >= 5 and "error" not in res:
            img_paths[c] = _srv.plot_statok_png(win, res, out_dir)

    # Composite Equilibrium Score: mean of valid stability scores
    valid_stabs = [r["stability_pct"] for r in results_by_c.values() if "error" not in r]
    ces = round(sum(valid_stabs) / len(valid_stabs), 1) if valid_stabs else None

    json_path = os.path.join(out_dir, "results.json")
    payload = {
        "source_csv": os.path.basename(csv_path),
        "generated_at": datetime.now().isoformat(),
        "protocol": _srv.SOT_PROTOCOL,
        "results": [results_by_c[k] for k in sorted(results_by_c.keys())],
        "ces": ces,
        "patient": dict(_sot_patient),
        "csv_rows": csv_rows,
        "csv_conditions": cond_dist,
        "missing_conditions": missing_conditions,
        "runtime_rows_by_condition": runtime_rows,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

    pdf_path = os.path.join(out_dir, "report.pdf")
    _build_sot_pdf(pdf_path, csv_path, results_by_c, img_paths,
                   patient_info=dict(_sot_patient), ces=ces,
                   debug_info={
                       "csv_rows": csv_rows,
                       "csv_conditions": cond_dist,
                       "missing_conditions": missing_conditions,
                       "runtime_rows_by_condition": runtime_rows,
                   })
    return out_dir, json_path, pdf_path

_srv.analyze_sot_csv = _patched_analyze_sot_csv

# =========================================================
# PATCHED analyze_one_condition â€“ lenient window + fallback
# =========================================================
def _patched_analyze_one_condition(dfc, cond_id):
    """Replacement with:
    - No strict analysis_end cutoff (use all data from analysis_start)
    - Progressive fallback if strict window is too short
    - Lower MIN_SAMPLES (5 instead of 10)
    """
    import numpy as _np, math as _math
    cfg = _srv.SOT_PROTOCOL[cond_id]
    if dfc.empty:
        return None, None
    dfc = dfc.sort_values("time").reset_index(drop=True)
    t0    = float(dfc["time"].iloc[0])
    t_rel = dfc["time"] - t0
    start = float(cfg.get("analysis_start", 0))
    end   = float(cfg.get("analysis_end", 9999))

    # Primary window: analysis_start â€¦ analysis_end
    win = dfc[(t_rel >= start) & (t_rel <= end)].copy()
    # Fallback 1: remove strict end cutoff
    if len(win) < 5:
        win = dfc[t_rel >= start].copy()
    # Fallback 2: use everything
    if len(win) < 5:
        win = dfc.copy()

    MIN_SAMPLES = 5
    if len(win) < MIN_SAMPLES:
        return {
            "condition": cond_id, "name": cfg["name"],
            "analysis_window_s": [start, end],
            "n": int(len(win)),
            "error": f"Seulement {len(win)} echantillons (min {MIN_SAMPLES})"
        }, win

    x = win["cop_x_cm"].astype(float).to_numpy()
    y = win["cop_y_cm"].astype(float).to_numpy()
    duration_s = float(win["time"].iloc[-1] - win["time"].iloc[0])
    if duration_s <= 0:
        duration_s = float((len(win) - 1) * 0.02)
    dx = _np.diff(x); dy = _np.diff(y)
    seg = _np.sqrt(dx*dx + dy*dy)
    path_length_cm  = float(_np.sum(seg))
    mean_speed_cm_s = float(path_length_cm / duration_s) if duration_s > 0 else float("nan")
    rms_r = float(_np.sqrt(_np.mean(x*x + y*y)))
    cov = _np.cov(_np.vstack([x, y]))
    eig = _np.linalg.eigvalsh(cov)
    eig = _np.maximum(eig, 0.0)
    CHI2_95_2DOF = 5.991
    ellipse95_area = float(_math.pi * CHI2_95_2DOF * _math.sqrt(eig[0] * eig[1]))
    stability_pct  = 100.0 * (1.0 - (rms_r / _srv.STAB_LIMIT_CM))
    stability_pct  = float(max(0.0, min(100.0, stability_pct)))
    actual_end = float(t_rel.iloc[-1]) if len(t_rel) > 0 else end
    print(f"[ANALYZE] C{cond_id}: {len(win)} pts, "
          f"t={start:.0f}..{actual_end:.1f}s, stability={stability_pct:.1f}%")
    return {
        "condition": cond_id, "name": cfg["name"],
        "analysis_window_s": [start, min(end, actual_end)],
        "n": int(len(win)), "duration_s": duration_s, "rms_r_cm": rms_r,
        "path_length_cm": path_length_cm, "mean_speed_cm_s": mean_speed_cm_s,
        "ellipse95_area_cm2": ellipse95_area, "stability_pct": stability_pct,
    }, win

_srv.analyze_one_condition = _patched_analyze_one_condition

# Also expose a debug endpoint for SOT CSV inspection
@app.route("/sot/csv-debug")
def sot_csv_debug():
    try:
        import pandas as pd
        p = _srv.current_log_path
        if not p or not os.path.isfile(p):
            return _json_resp({"error": "no log file", "path": p})
        df = pd.read_csv(p)
        cond_num = pd.to_numeric(df["condition"], errors="coerce")
        if cond_num.isna().any():
            extracted = df["condition"].astype(str).str.extract(r"(\d+)")[0]
            cond_num = cond_num.fillna(pd.to_numeric(extracted, errors="coerce"))
        df["condition"] = cond_num.fillna(0).astype(int)
        dist = {str(k): int(v) for k, v in df["condition"].value_counts().items()}
        return _json_resp({
            "path": p, "rows": len(df), "columns": list(df.columns),
            "condition_distribution": dist,
            "sample_first": df.head(3).to_dict(orient="records") if len(df) > 0 else []
        })
    except Exception as e:
        return _json_resp({"error": str(e)})

# =========================================================
# PATIENTS API
# =========================================================
@app.route("/patients", methods=["GET"])
def patients_get():
    with _data_lock:
        data = _load_json(PATIENTS_FILE)
    return _json_resp(data)

@app.route("/patients", methods=["POST"])
def patients_create():
    body = _body()
    if not body.get("nom") or not body.get("prenom"):
        return _json_resp({"error": "nom and prenom required"}, 400)
    if not body.get("id"):
        body["id"] = f"pat_{int(time.time()*1000)}"
    body.setdefault("createdAt", datetime.now().isoformat())
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
        patients.append(body)
        _save_json(PATIENTS_FILE, patients)
    return _json_resp(body, 201)

@app.route("/patients/<patient_id>", methods=["GET"])
def patients_get_one(patient_id):
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
    p = next((x for x in patients if x.get("id") == patient_id), None)
    return _json_resp(p) if p else _json_resp({"error": "not found"}, 404)

@app.route("/patients/<patient_id>", methods=["PUT"])
def patients_update(patient_id):
    body = _body()
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
        for i, p in enumerate(patients):
            if p.get("id") == patient_id:
                patients[i] = {**p, **body, "id": patient_id}
                _save_json(PATIENTS_FILE, patients)
                return _json_resp(patients[i])
    return _json_resp({"error": "not found"}, 404)

@app.route("/patients/<patient_id>", methods=["DELETE"])
def patients_delete(patient_id):
    with _data_lock:
        patients = _load_json(PATIENTS_FILE)
        _save_json(PATIENTS_FILE, [p for p in patients if p.get("id") != patient_id])
    return _json_resp({"ok": True})

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
    return _json_resp(list(reversed(sessions[-200:])))

@app.route("/sessions", methods=["POST"])
def sessions_create():
    body = _body()
    if not body.get("id"):
        body["id"] = f"ses_{int(time.time()*1000)}"
    body.setdefault("createdAt", datetime.now().isoformat())
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
        sessions.append(body)
        if len(sessions) > 1000:
            sessions = sessions[-1000:]
        _save_json(SESSIONS_FILE, sessions)
    return _json_resp(body, 201)

@app.route("/sessions/export.csv")
def sessions_export_csv():
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","patient","exercice","preset","debut","fin","parametres","score"])
    for s in sessions:
        w.writerow([s.get("id",""), s.get("patient",""), s.get("exId",""),
                    s.get("preset",""), s.get("startTime",""), s.get("endTime",""),
                    json.dumps(s.get("params",{})), json.dumps(s.get("score",{}))])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=posturosps_{datetime.now().strftime('%Y%m%d')}.csv"}
    )

@app.route("/sessions/export.json")
def sessions_export_json():
    with _data_lock:
        sessions = _load_json(SESSIONS_FILE)
    return Response(
        json.dumps(sessions, indent=2, ensure_ascii=False), mimetype="application/json",
        headers={"Content-Disposition":
                 f"attachment; filename=posturosps_{datetime.now().strftime('%Y%m%d')}.json"}
    )

# =========================================================
# PRESETS API
# =========================================================
DEFAULT_PRESETS = [
    {"id":"vest","name":"Vestibulaire","icon":"ðŸŒ€","color":"vest",
     "desc":"VOR + cible + opto 12 min",
     "sequence":[
       {"ex":"ex5","duration":120,"params":{"platform":"fixed","vor_mode":"lr","vor_interval":5}},
       {"ex":"ex8","duration":120,"params":{"platform":"fixed","target_mode":"random","difficulty":"medium"}},
     ]},
    {"id":"proprio","name":"Proprioception","icon":"âš–ï¸","color":"proprio",
     "desc":"Sinus + impulsions 15 min",
     "sequence":[
       {"ex":"ex2","duration":120,"params":{"amplitude":"low","speed":"low"}},
       {"ex":"ex4","duration":150,"params":{"amplitude":"medium","speed":"medium"}},
       {"ex":"ex3","duration":90, "params":{"amplitude":"medium","speed":"medium"}},
     ]},
    {"id":"dual","name":"Double tache","icon":"ðŸ§ ","color":"dual",
     "desc":"Citations + COP 12 min",
     "sequence":[
       {"ex":"ex7","duration":120,"params":{"platform":"sinus","amplitude":"low","speed":"low"}},
       {"ex":"ex9","duration":120,"params":{"platform":"fixed","sequence":"cross","difficulty":"medium"}},
     ]},
    {"id":"senior","name":"Senior securisee","icon":"ðŸ¤","color":"senior",
     "desc":"Doux et progressif 10 min",
     "sequence":[
       {"ex":"ex1","duration":60, "params":{"platform":"fixed"}},
       {"ex":"ex6","duration":120,"params":{"platform":"fixed","point_mode":"lr","point_speed":"low"}},
       {"ex":"ex8","duration":120,"params":{"platform":"fixed","difficulty":"low"}},
     ]},
    {"id":"sport","name":"Retour sport","icon":"ðŸƒ","color":"sport",
     "desc":"Dynamique et reactif 20 min",
     "sequence":[
       {"ex":"ex4", "duration":120,"params":{"amplitude":"high","speed":"high"}},
       {"ex":"ex11","duration":180,"params":{"platform":"sinus","difficulty":"high"}},
       {"ex":"ex10","duration":120,"params":{"platform":"auto","difficulty":"high"}},
     ]},
    {"id":"cervical","name":"Cervical","icon":"ðŸ”„","color":"cervical",
     "desc":"VOR + parcours 15 min",
     "sequence":[
       {"ex":"ex5", "duration":120,"params":{"platform":"fixed","vor_mode":"random"}},
       {"ex":"ex10","duration":120,"params":{"platform":"fixed","path":"infinity"}},
     ]},
]

@app.route("/presets", methods=["GET"])
def presets_get():
    with _data_lock:
        custom = _load_json(PRESETS_FILE)
    return _json_resp(DEFAULT_PRESETS + custom)

@app.route("/presets", methods=["POST"])
def presets_create():
    body = _body()
    if not body.get("name"):
        return _json_resp({"error": "name required"}, 400)
    if not body.get("id"):
        body["id"] = f"preset_{int(time.time()*1000)}"
    body["custom"] = True
    with _data_lock:
        presets = _load_json(PRESETS_FILE)
        presets.append(body)
        _save_json(PRESETS_FILE, presets)
    return _json_resp(body, 201)

@app.route("/presets/<preset_id>", methods=["DELETE"])
def presets_delete(preset_id):
    with _data_lock:
        presets = _load_json(PRESETS_FILE)
        _save_json(PRESETS_FILE, [p for p in presets if p.get("id") != preset_id])
    return _json_resp({"ok": True})

# =========================================================
# EXERCISE 13 â€“ PONG (COP-controlled paddle)
# =========================================================
_ex13_running = False
_ex13_mode = {
    "difficulty": "medium",   # beginner / medium / hard
    "platform":   "fixed",
    "score_player": 0,
    "score_ai":     0,
}
_ex13_lock = threading.Lock()

def _ex13_cop_loop():
    """Continuously push COP X (normalized -1..1) into hdmi_state cursor_x for Pong."""
    while _ex13_running:
        try:
            # Same normalisation as ex8-11: cop_x_f / 4.0 cmâ†’norm
            cx = max(-1.0, min(1.0, _srv.cop_x_f / 4.0))
            _srv.hdmi_state["cursor_x"] = cx
        except Exception:
            pass
        time.sleep(0.02)   # 50 Hz

PONG_DIFFICULTY = {
    "beginner": {"paddle_w": 0.30, "ball_speed": 0.012, "ai_speed": 0.010, "ai_error": 0.08},
    "medium":   {"paddle_w": 0.18, "ball_speed": 0.020, "ai_speed": 0.016, "ai_error": 0.04},
    "hard":     {"paddle_w": 0.10, "ball_speed": 0.030, "ai_speed": 0.024, "ai_error": 0.01},
}

@app.route("/exercise13/set", methods=["POST"])
def ex13_set():
    body = _body()
    with _ex13_lock:
        _ex13_mode.update({k: v for k, v in body.items() if k in _ex13_mode})
    return _json_resp({"ok": True, "mode": _ex13_mode})

@app.route("/exercise13/start", methods=["GET", "POST"])
def ex13_start():
    global _ex13_running
    with _ex13_lock:
        diff = _ex13_mode.get("difficulty", "medium")
        plat = _ex13_mode.get("platform", "fixed")
        _ex13_mode["score_player"] = 0
        _ex13_mode["score_ai"]     = 0
        _ex13_running = True
    cfg = PONG_DIFFICULTY.get(diff, PONG_DIFFICULTY["medium"])
    # Platform control
    if plat == "auto":
        _srv.send_to_esp = True
        _srv.esp_send("ARM:1"); time.sleep(0.05); _srv.esp_send("AUTO:1")
    else:
        _srv.send_to_esp = False
        _srv.esp_send("STOP")
    # Update HDMI state for pong rendering
    _srv.set_hdmi(
        mode="pong",
        title=f"PONG â€“ {diff.capitalize()}",
    )
    _srv.hdmi_state.update({
        "pong_difficulty": diff,
        "pong_paddle_w":   cfg["paddle_w"],
        "pong_ball_speed": cfg["ball_speed"],
        "pong_ai_speed":   cfg["ai_speed"],
        "pong_ai_error":   cfg["ai_error"],
        "pong_score_player": 0,
        "pong_score_ai":     0,
    })
    # Start COPâ†’cursor_x feed loop
    threading.Thread(target=_ex13_cop_loop, daemon=True).start()
    return _json_resp({"ok": True, "difficulty": diff})

@app.route("/exercise13/stop", methods=["GET", "POST"])
def ex13_stop():
    global _ex13_running
    with _ex13_lock:
        _ex13_running = False
    _srv.send_to_esp = False
    _srv.esp_send("STOP")
    _srv.set_hdmi(mode="off")
    return _json_resp({"ok": True, "score_player": _ex13_mode["score_player"],
                       "score_ai": _ex13_mode["score_ai"]})

@app.route("/exercise13/status")
def ex13_status():
    with _ex13_lock:
        mode_copy = dict(_ex13_mode)
    return _json_resp({"running": _ex13_running, **mode_copy})

@app.route("/exercise13/score", methods=["POST"])
def ex13_score():
    """Called by HDMI canvas when a point is scored."""
    body = _body()
    with _ex13_lock:
        if "score_player" in body:
            _ex13_mode["score_player"] = int(body["score_player"])
        if "score_ai" in body:
            _ex13_mode["score_ai"] = int(body["score_ai"])
        _srv.hdmi_state["pong_score_player"] = _ex13_mode["score_player"]
        _srv.hdmi_state["pong_score_ai"]     = _ex13_mode["score_ai"]
    return _json_resp({"ok": True})

# =========================================================
# EXERCISE 14 â€“ DOLPHIN / WII PLAY
# =========================================================
_ex14_running = False
_ex14_dolphin = None   # subprocess.Popen handle
_ex14_mode = {
    "platform":  "fixed",
    "amplitude": "medium",
    "speed":     "medium",
}
_ex14_lock = threading.Lock()

_DOLPHIN_GAME = "/home/sylvain/wii/wiiplay.rvz"


def _ex14_platform_loop():
    """Platform motion thread for exercise14 (sinus / ramp / impulses)."""
    import math, random as _rnd
    with _ex14_lock:
        plat = _ex14_mode.get("platform", "fixed")
        akey = _ex14_mode.get("amplitude", "medium")
        skey = _ex14_mode.get("speed", "medium")

    amp  = _srv.exercise2_amp_value(akey)
    freq = _srv.exercise2_freq_value(skey)
    slew = _srv.exercise_slew_per_s(skey)

    t0      = time.time()
    cmd_now = 0.0
    last_t  = t0

    if plat == "impulses":
        while _ex14_running:
            wait_s = _rnd.uniform(1.0, 3.0)
            t_wait = time.time()
            while _ex14_running and (time.time() - t_wait < wait_s):
                if _srv.uart:
                    try: _srv.uart.write(b"COP:Y:0.0000\n")
                    except: pass
                time.sleep(0.02)
            if not _ex14_running:
                break
            sign = _rnd.choice([-1.0, 1.0])
            amp4 = _srv.exercise4_amp_value(akey)
            pd   = _srv.exercise_pulse_duration(skey)
            t0p  = time.time()
            while _ex14_running:
                phase = (time.time() - t0p) / pd
                if phase >= 1.0:
                    break
                cmd = sign * amp4 * _srv.exercise4_pulse_shape(phase)
                cmd = _srv.ex2_apply_soft_limit(cmd)
                cmd = max(-_srv.CMD_MAX, min(_srv.CMD_MAX, cmd))
                if _srv.uart:
                    try: _srv.uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
                    except: pass
                time.sleep(0.02)
        return

    # Sinus or ramp
    while _ex14_running:
        now     = time.time()
        dt      = max(0.001, now - last_t)
        last_t  = now
        elapsed = now - t0

        if plat == "sinus":
            cmd_target = amp * math.sin(2 * math.pi * freq * elapsed)
        else:   # ramp
            phase_env  = (elapsed % 24.0) / 24.0
            env        = _srv.exercise3_envelope(phase_env)
            cmd_target = (amp * env) * math.sin(2 * math.pi * freq * elapsed)

        cmd_target = _srv.ex2_apply_soft_limit(cmd_target)
        max_step   = slew * dt
        delta      = cmd_target - cmd_now
        delta      = max(-max_step, min(max_step, delta))
        cmd_now   += delta

        if _srv.uart:
            try: _srv.uart.write(f"COP:Y:{cmd_now:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)


def _ex14_stop_dolphin():
    global _ex14_dolphin
    # 1. flatpak kill sends SIGTERM inside the sandbox (kills the real process)
    try:
        subprocess.call(["flatpak", "kill", "org.DolphinEmu.dolphin-emu"],
                        timeout=3)
    except Exception as e:
        print(f"[EX14] flatpak kill error: {e}")
    # 2. Also terminate the wrapper Popen we hold
    if _ex14_dolphin is not None:
        try:
            _ex14_dolphin.terminate()
            try:    _ex14_dolphin.wait(timeout=3)
            except subprocess.TimeoutExpired: _ex14_dolphin.kill()
        except Exception as e:
            print(f"[EX14] Dolphin wrapper stop error: {e}")
        _ex14_dolphin = None


def _ex14_build_env():
    """Build an env dict suitable for launching Dolphin (flatpak GUI app)."""
    env = os.environ.copy()

    # X display
    if not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"

    # XDG_RUNTIME_DIR is mandatory for flatpak D-Bus/portal access.
    # Auto-detect from the UID of the game file owner.
    if not env.get("XDG_RUNTIME_DIR"):
        try:
            import pwd as _pwd
            uid  = os.stat(_DOLPHIN_GAME).st_uid
            xdg  = f"/run/user/{uid}"
        except Exception:
            uid  = 1000
            xdg  = "/run/user/1000"
        env["XDG_RUNTIME_DIR"] = xdg
        print(f"[EX14] XDG_RUNTIME_DIR set to {xdg}")

    # XAUTHORITY â€“ X11 auth cookie (needed when running from a service/daemon)
    if not env.get("XAUTHORITY"):
        candidates = []
        try:
            import pwd as _pwd
            uid  = os.stat(_DOLPHIN_GAME).st_uid
            home = _pwd.getpwuid(uid).pw_dir
            candidates.append(os.path.join(home, ".Xauthority"))
        except Exception:
            pass
        candidates.append(os.path.expanduser("~/.Xauthority"))
        candidates.append("/tmp/.Xauthority")
        for c in candidates:
            if os.path.isfile(c):
                env["XAUTHORITY"] = c
                print(f"[EX14] XAUTHORITY set to {c}")
                break

    return env


_dolphin_log_path = "/tmp/dolphin_ex14.log"


def _ex14_set_dolphin_fullscreen():
    """Write Fullscreen=True into Dolphin's Dolphin.ini before launch.
    The -f / --fullscreen CLI flag does not exist in the flatpak build;
    fullscreen must be set via the config file."""
    import configparser, pwd as _pwd
    try:
        uid  = os.stat(_DOLPHIN_GAME).st_uid
        home = _pwd.getpwuid(uid).pw_dir
    except Exception:
        home = os.path.expanduser("~")
    cfg_path = os.path.join(home, ".var", "app",
                            "org.DolphinEmu.dolphin-emu",
                            "config", "dolphin-emu", "Dolphin.ini")
    cfg = configparser.RawConfigParser()
    cfg.optionxform = str   # preserve key case (Dolphin is case-sensitive)
    if os.path.isfile(cfg_path):
        cfg.read(cfg_path)
    if not cfg.has_section("Display"):
        cfg.add_section("Display")
    cfg.set("Display", "Fullscreen", "True")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w") as f:
        cfg.write(f)
    print(f"[EX14] Dolphin.ini updated: {cfg_path}")


@app.route("/exercise14/set", methods=["GET", "POST"])
def ex14_set():
    if request.method == "POST":
        body = _body()
    else:
        body = request.args.to_dict()
    with _ex14_lock:
        for k in ("platform", "amplitude", "speed"):
            if k in body:
                _ex14_mode[k] = body[k]
    return _json_resp({"ok": True, "mode": _ex14_mode})


@app.route("/exercise14/start", methods=["GET", "POST"])
def ex14_start():
    global _ex14_running, _ex14_dolphin
    # Stop any previous instance first
    with _ex14_lock:
        _ex14_running = False
    _ex14_stop_dolphin()
    time.sleep(0.1)

    with _ex14_lock:
        plat = _ex14_mode.get("platform", "fixed")
        _ex14_running = True

    # Platform control
    if plat == "auto":
        _srv.send_to_esp = True
        _srv.esp_send("ARM:1"); time.sleep(0.05); _srv.esp_send("AUTO:1")
    elif plat in ("sinus", "ramp", "impulses"):
        _srv.send_to_esp = True
        _srv.esp_send("ARM:1"); time.sleep(0.05); _srv.esp_send("AUTO:1")
        threading.Thread(target=_ex14_platform_loop, daemon=True).start()
    else:  # fixed
        _srv.send_to_esp = False
        _srv.esp_send("STOP")

    # Launch Dolphin fullscreen
    # (-f / --fullscreen CLI flag does not exist in this flatpak build;
    #  fullscreen is set via Dolphin.ini instead)
    _ex14_set_dolphin_fullscreen()
    env  = _ex14_build_env()
    cmd  = ["flatpak", "run", "org.DolphinEmu.dolphin-emu",
            "-b", "-e", _DOLPHIN_GAME]
    print(f"[EX14] Launching: {' '.join(cmd)}")
    print(f"[EX14] DISPLAY={env.get('DISPLAY')}  XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR')}")
    try:
        logf = open(_dolphin_log_path, "w")
        _ex14_dolphin = subprocess.Popen(cmd, env=env, stdout=logf, stderr=logf)
        pid = _ex14_dolphin.pid
        print(f"[EX14] Dolphin PID={pid}")
    except Exception as e:
        print(f"[EX14] Dolphin launch failed: {e}")
        _ex14_dolphin = None
        pid = None

    return _json_resp({"ok": True, "platform": plat, "dolphin_pid": pid})


@app.route("/exercise14/stop", methods=["GET", "POST"])
def ex14_stop():
    global _ex14_running
    with _ex14_lock:
        _ex14_running = False
    _srv.send_to_esp = False
    _srv.esp_send("STOP")
    _ex14_stop_dolphin()
    _srv.set_hdmi(mode="off")
    return _json_resp({"ok": True})


@app.route("/exercise14/status")
def ex14_status():
    with _ex14_lock:
        mode_copy = dict(_ex14_mode)
    dolphin_alive = _ex14_dolphin is not None and _ex14_dolphin.poll() is None
    returncode = _ex14_dolphin.returncode if _ex14_dolphin is not None else None
    return _json_resp({"running": _ex14_running, "dolphin_alive": dolphin_alive,
                       "returncode": returncode, **mode_copy})


@app.route("/exercise14/log")
def ex14_log():
    """Return last 100 lines of Dolphin launch log for debugging."""
    try:
        with open(_dolphin_log_path, "r") as f:
            lines = f.readlines()
        return "<pre style='font-size:13px'>" + "".join(lines[-100:]) + "</pre>"
    except FileNotFoundError:
        return "No log yet â€“ start exercise14 first.", 404


# =========================================================
# SYSTEM INFO
# =========================================================
@app.route("/api/info")
def api_info():
    with _srv.lock:
        s = dict(_srv.latest)
    return _json_resp({
        "version": "3.1-pwa",
        "timestamp": datetime.now().isoformat(),
        "platform": {
            "tare_ready": s.get("tare_ready", False),
            "offset_ready": s.get("offset_ready", False),
            "send_to_esp": s.get("send_to_esp", False),
            "cop_x_cm": round(float(s.get("cop_x_cm", 0.0)), 3),
            "cop_y_cm": round(float(s.get("cop_y_cm", 0.0)), 3),
            "cmd": round(float(s.get("cmd", 0.0)), 3),
            "total": round(float(s.get("total", 0.0)), 6),
        },
        "videos": list_all_videos(),
        "mpv_running": _mpv_proc is not None and _mpv_proc.poll() is None,
    })

# =========================================================
# ENTRYPOINT
# =========================================================
if __name__ == "__main__":
    print("=" * 62)
    print("  PosturoSPS PWA Server v3.1")
    print("  PWA      : http://0.0.0.0:5000/")
    print("  API info : http://0.0.0.0:5000/api/info")
    print("  Videos   : " + VIDEOS_DIR)
    print("  Data     : " + DATA_DIR)
    print("=" * 62)
    _orig_main()
