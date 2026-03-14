#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_pwa.py – PosturoSPS PWA Server v3.1
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
# OVERRIDE ROOT → serve PWA index.html
# =========================================================
app.view_functions["index"] = lambda: send_from_directory(STATIC_DIR, "index.html")

# =========================================================
# OVERRIDE /hdmi → serve premium hdmi.html
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
            _transcode_jobs[job_id]["error"] = "ffmpeg not found – sudo apt install ffmpeg"
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
        print("[MPV] mpv not found – install with: sudo apt install mpv")
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
# EXERCISE 12 – video via Chromium (original behaviour)
# + scan videos/ directory so new files are picked up
# =========================================================
# Patch list_static_videos so ex12 also sees videos/ dir
_srv.list_static_videos = list_all_videos

# NOTE: ex12 start/stop are NOT overridden – use original Chromium-based playback.

# Patch ensure_chromium: GPU flags + NEVER THROW (critical for SOT stability)
def _ensure_chromium_safe():
    """Launch Chromium with GPU flags. Never raises – errors are logged only."""
    if _srv.opto_process is not None and _srv.opto_process.poll() is None:
        return  # already running
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    gpu_flags = [
        "--kiosk", "--noerrdialogs", "--disable-infobars",
        "--disable-restore-session-state", "--no-first-run",
        "--enable-gpu-rasterization", "--enable-zero-copy",
        "--use-gl=egl", "--ignore-gpu-blocklist",
        # NOTE: do NOT add --disable-software-rasterizer – if EGL/GPU is
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
    print("[HDMI] Chromium not found – HDMI display unavailable")

_srv.ensure_chromium = _ensure_chromium_safe

# =========================================================
# SOT – Robust dedicated logging thread
# =========================================================
# Root-cause: the control loop has TWO early-continue guards
# that silently skip the logging section:
#   1. if (not tare_ready) or (not offset_ready): continue
#   2. if total < TOTAL_MIN: continue
# Both are bypassed by our dedicated thread which reads
# directly from _srv.cop_x_f / cop_y_f / latest – completely
# independent from the control loop.
# =========================================================

_sot_orig_total_min = _srv.TOTAL_MIN
_sot_bg_stop  = threading.Event()
_sot_bg_path  = None   # path of the CSV being recorded


def _sot_bg_logger(path, stop_evt):
    """Dedicated 50 Hz SOT logger – writes directly from _srv globals.
    Completely bypasses the control-loop's logging section."""
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
                    if rows_written % 250 == 0:   # flush every 5 s
                        f.flush()
                except Exception as _e:
                    print(f"[SOT LOG] row error: {_e}")
                time.sleep(0.02)   # 50 Hz
            f.flush()
        print(f"[SOT LOG] Finished – {rows_written} rows → {path}")
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
    _srv.TOTAL_MIN = -1.0   # extra: bypass control-loop TOTAL_MIN guard too

    # Ensure calibration flags are set so the control loop also computes COP
    if not _srv.tare_ready:
        print("[SOT] tare not done – running auto-tare now")
        _srv.tare()
    if not _srv.offset_ready:
        print("[SOT] center not done – assuming (0,0) offset")
        _srv.offset_x_cm = 0.0
        _srv.offset_y_cm = 0.0
        _srv.offset_ready = True
        with _srv.lock:
            _srv.latest["offset_ready"] = True

    # Open the log file ourselves (do NOT rely on control-loop start_log)
    os.makedirs("logs", exist_ok=True)
    log_path = datetime.now().strftime("logs/sot_%Y%m%d_%H%M%S.csv")
    _srv.current_log_path = log_path   # finalize_sot_and_analyze reads this
    _srv.logging_active   = True       # must be True for finalize to proceed
    _srv.log_file         = None       # prevent control loop from writing
    _srv.log_writer       = None       # (if log_writer is None, control loop skips write)

    # Start dedicated background logger
    _sot_bg_start(log_path)

    _srv.start_condition(c)
    print(f"[SOT] Condition {c} started – logging to {log_path} "
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
    _srv.sot_condition += 1
    if _srv.sot_condition > 6:
        _srv.TOTAL_MIN = _sot_orig_total_min
        _srv.stop_condition()
        _sot_bg_finish()
        _srv.logging_active = False
        _srv.finalize_sot_and_analyze()
        return "SOT FINISHED\n"
    _srv.stop_condition()
    _srv.start_condition(_srv.sot_condition)   # updates current_condition
    return f"NEXT: CONDITION {_srv.sot_condition}\n"


def _patched_sot_restart():
    # Same condition – keep logging to the same file, just reset platform
    _srv.start_condition(_srv.sot_condition)
    return f"RESTART CONDITION {_srv.sot_condition}\n"


app.view_functions["sot_start"]   = _patched_sot_start
app.view_functions["sot_stop"]    = _patched_sot_stop
app.view_functions["sot_next"]    = _patched_sot_next
app.view_functions["sot_restart"] = _patched_sot_restart


@app.route("/sot/state")
def sot_state_debug():
    """Real-time diagnostic endpoint – shows all SOT-relevant state."""
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
        "TOTAL_MIN":        _srv.TOTAL_MIN,
        "current_condition": _srv.current_condition,
        "sot_condition":    _srv.sot_condition,
        "cop_x_f":          round(_srv.cop_x_f, 3),
        "cop_y_f":          round(_srv.cop_y_f, 3),
        "total":            round(_srv.latest.get("total", 0.0), 6),
    })

# =========================================================
# SOT PDF – Clean rebuild with proper French encoding
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


def _build_sot_pdf(pdf_path, source_csv, results_by_c, img_paths, debug_info=None):
    """
    Professional clinical SOT report — clean French encoding.
    Inspired by Framiral Multitest layout.
    """
    if not _RL_OK:
        print("[PDF] ReportLab not available")
        return

    fn, fb = _get_fonts()
    styles = getSampleStyleSheet()

    # Custom paragraph styles
    style_title = ParagraphStyle(
        "sps_title", fontName=fb, fontSize=22, textColor=colors.HexColor("#1d4ed8"),
        spaceAfter=6, alignment=TA_CENTER, leading=28
    )
    style_subtitle = ParagraphStyle(
        "sps_subtitle", fontName=fn, fontSize=11, textColor=colors.HexColor("#475569"),
        spaceAfter=4, alignment=TA_CENTER
    )
    style_h2 = ParagraphStyle(
        "sps_h2", fontName=fb, fontSize=13, textColor=colors.HexColor("#1e293b"),
        spaceBefore=10, spaceAfter=6, borderPad=4,
        borderColor=colors.HexColor("#3b82f6"), borderWidth=0,
        leftIndent=0
    )
    style_body = ParagraphStyle(
        "sps_body", fontName=fn, fontSize=10, textColor=colors.HexColor("#1e293b"),
        spaceAfter=4, leading=14
    )
    style_small = ParagraphStyle(
        "sps_small", fontName=fn, fontSize=9, textColor=colors.HexColor("#64748b"),
        spaceAfter=2
    )
    style_header_cell = ParagraphStyle(
        "sps_hcell", fontName=fb, fontSize=9, textColor=colors.white,
        alignment=TA_CENTER
    )
    style_cell = ParagraphStyle(
        "sps_cell", fontName=fn, fontSize=9, textColor=colors.HexColor("#1e293b"),
        alignment=TA_CENTER
    )

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=1.8*RL_CM, rightMargin=1.8*RL_CM,
        topMargin=1.5*RL_CM, bottomMargin=2.0*RL_CM,
        title="Rapport SOT – PosturoSPS",
        author="PosturoSPS"
    )

    story = []
    W_avail = A4[0] - 3.6*RL_CM  # usable width

    # ---- Header band ----
    logo_path = os.path.join(_HERE, "logo.png")
    if os.path.isfile(logo_path):
        story.append(RLImage(logo_path, width=2.5*RL_CM, height=2.5*RL_CM))
        story.append(Spacer(1, 0.2*RL_CM))

    # Cabinet info (clean French text)
    cabinet_lines = [
        "Cabinet de Reeducation Vestibulaire",
        "276 avenue de l'Europe – 44240 Suce sur Erdre",
        "Tel : 07.55.55.70.96  |  sylvain.fremon@masseur-kinesitherapeute.mssante.fr",
    ]
    for line in cabinet_lines:
        story.append(Paragraph(line, style_subtitle))

    story.append(HRFlowable(width="100%", thickness=2,
                             color=colors.HexColor("#3b82f6"), spaceAfter=8))

    # ---- Title ----
    story.append(Paragraph("BILAN SOT – Sensory Organization Test", style_title))
    story.append(Paragraph(
        f"Genere le {datetime.now().strftime('%d/%m/%Y a %H:%M')}  |  Fichier : {os.path.basename(source_csv)}",
        style_subtitle
    ))
    story.append(Spacer(1, 0.4*RL_CM))

    # ---- Protocol reminder ----
    story.append(Paragraph("Protocole", style_h2))
    proto_data = [
        [Paragraph("Cond.", style_header_cell),
         Paragraph("Nom", style_header_cell),
         Paragraph("Fenetre d'analyse", style_header_cell),
         Paragraph("Plateforme", style_header_cell),
         Paragraph("Vision", style_header_cell)],
        ["C1", "EO Stable",     "0 – 20 s", "Stable", "Yeux ouverts"],
        ["C2", "EC Stable",     "0 – 20 s", "Stable", "Yeux fermes"],
        ["C3", "EO Opto",       "15 – 35 s","Stable", "Optocinetique"],
        ["C4", "EO Instable",   "0 – 20 s", "Mobile", "Yeux ouverts"],
        ["C5", "EC Instable",   "0 – 20 s", "Mobile", "Yeux fermes"],
        ["C6", "Opto Instable", "15 – 35 s","Mobile", "Optocinetique"],
    ]
    col_w = [1.0*RL_CM, 3.8*RL_CM, 3.2*RL_CM, 2.5*RL_CM, 3.0*RL_CM]
    tbl_proto = Table(proto_data, colWidths=col_w)
    tbl_proto.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#1d4ed8")),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), fb),
        ("FONTNAME",    (0,1), (-1,-1), fn),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.4, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f8fafc")]),
        ("ALIGN",       (0,0), (-1,-1), "CENTER"),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
    ]))
    story.append(tbl_proto)
    story.append(Spacer(1, 0.5*RL_CM))

    # ---- Results table ----
    story.append(Paragraph("Resultats par condition", style_h2))
    res_header = [
        Paragraph("Cond.", style_header_cell),
        Paragraph("Nom", style_header_cell),
        Paragraph("N pts", style_header_cell),
        Paragraph("Stabilite %", style_header_cell),
        Paragraph("Vitesse moy. (cm/s)", style_header_cell),
        Paragraph("Surface 95% (cm2)", style_header_cell),
        Paragraph("RMS (cm)", style_header_cell),
    ]
    res_rows = [res_header]
    stab_values = {}
    for c in range(1, 7):
        if c not in results_by_c:
            res_rows.append([f"C{c}", "–", "–", "–", "–", "–", "–"])
            continue
        r = results_by_c[c]
        if "error" in r:
            res_rows.append([f"C{c}", r.get("name",""), str(r.get("n","–")), "Données insuffisantes", "–", "–", "–"])
        else:
            stab = r.get("stability_pct", 0)
            stab_values[c] = stab
            stab_str = f"{stab:.1f}"
            res_rows.append([
                f"C{c}",
                r.get("name", ""),
                str(r.get("n", "–")),
                stab_str,
                f"{r.get('mean_speed_cm_s',0):.3f}",
                f"{r.get('ellipse95_area_cm2',0):.3f}",
                f"{r.get('rms_r_cm',0):.3f}",
            ])
    col_w2 = [0.9*RL_CM, 3.2*RL_CM, 1.4*RL_CM, 2.2*RL_CM, 3.0*RL_CM, 3.0*RL_CM, 1.8*RL_CM]
    tbl_res = Table(res_rows, colWidths=col_w2)
    ts_res = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#1d4ed8")),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), fb),
        ("FONTNAME",    (0,1), (-1,-1), fn),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.4, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f8fafc")]),
        ("ALIGN",       (0,0), (-1,-1), "CENTER"),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
    ])
    # Color code stability
    for i, c in enumerate(range(1, 7)):
        row_idx = i + 1
        if c in stab_values:
            stab = stab_values[c]
            if stab >= 75:
                bg = colors.HexColor("#d1fae5")
            elif stab >= 50:
                bg = colors.HexColor("#fef9c3")
            else:
                bg = colors.HexColor("#fee2e2")
            ts_res.add("BACKGROUND", (3, row_idx), (3, row_idx), bg)
    tbl_res.setStyle(ts_res)
    story.append(tbl_res)
    story.append(Spacer(1, 0.5*RL_CM))

    # ---- Sensory ratios ----
    def _sr(c1, c2):
        s1 = stab_values.get(c1); s2 = stab_values.get(c2)
        if s1 and s2 and s1 > 1:
            return round(min(2.0, max(0.0, s2/s1)), 2)
        return None

    ratios = {
        "Somesthesie (C2/C1)": _sr(1, 2),
        "Vision (C4/C1)":       _sr(1, 4),
        "Vestibule (C5/C1)":    _sr(1, 5),
    }
    denom = (stab_values.get(2,0) + stab_values.get(5,0))
    pref_vis = None
    if denom > 1:
        num = (stab_values.get(3,0) + stab_values.get(6,0))
        pref_vis = round(min(3.0, max(0.0, num/denom)), 2)
    if pref_vis is not None:
        ratios["Pref. visuelle ((C3+C6)/(C2+C5))"] = pref_vis

    valid_ratios = {k: v for k, v in ratios.items() if v is not None}
    if valid_ratios:
        story.append(Paragraph("Ratios sensoriels", style_h2))
        r_header = [Paragraph(k, ParagraphStyle("rc", fontName=fb, fontSize=9,
                                                  textColor=colors.HexColor("#1e293b"), alignment=TA_CENTER))
                    for k in valid_ratios.keys()]
        r_vals = []
        r_bg = []
        for v in valid_ratios.values():
            r_vals.append(f"{v:.2f}")
            if v >= 0.8:
                r_bg.append(colors.HexColor("#d1fae5"))
            elif v >= 0.5:
                r_bg.append(colors.HexColor("#fef9c3"))
            else:
                r_bg.append(colors.HexColor("#fee2e2"))

        n_cols = len(valid_ratios)
        col_w_r = [W_avail / n_cols] * n_cols
        tbl_r = Table([r_header, r_vals], colWidths=col_w_r)
        ts_r = TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f1f5f9")),
            ("FONTNAME",   (0,0), (-1,0), fb),
            ("FONTNAME",   (0,1), (-1,1), fn),
            ("FONTSIZE",   (0,0), (-1,-1), 9),
            ("GRID",       (0,0), (-1,-1), 0.4, colors.HexColor("#cbd5e1")),
            ("ALIGN",      (0,0), (-1,-1), "CENTER"),
            ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0),(-1,-1), 6),
        ])
        for i, bg in enumerate(r_bg):
            ts_r.add("BACKGROUND", (i,1), (i,1), bg)
        tbl_r.setStyle(ts_r)
        story.append(tbl_r)
        story.append(Spacer(1, 0.3*RL_CM))

    # ---- Sensory Organization Diagram (horizontal bars like Framiral) ----
    if valid_ratios:
        story.append(Paragraph("Organisation sensorielle", style_h2))
        # Reference norms: SOM>0.8, VIS>0.8, VEST>0.6, PREF_VIS<1.0 (lower=better)
        sens_labels = {
            "Somesthesie (C2/C1)": ("Somatosensoriel", 0.0, 1.5, 0.8, 1.0),
            "Vision (C4/C1)":       ("Vision",           0.0, 1.5, 0.8, 1.0),
            "Vestibule (C5/C1)":    ("Vestibulaire",     0.0, 1.5, 0.6, 1.0),
            "Pref. visuelle ((C3+C6)/(C2+C5))": ("Dependance visuelle", 0.0, 2.5, 0.0, 1.0),
        }
        n_sens = len([k for k in sens_labels if k in valid_ratios])
        if n_sens:
            fig2, axes = plt.subplots(n_sens, 1, figsize=(7.5, 0.9 * n_sens + 0.4))
            if n_sens == 1:
                axes = [axes]
            ax_idx = 0
            for key, (label, vmin, vmax, norm_lo, norm_hi) in sens_labels.items():
                if key not in valid_ratios:
                    continue
                ax2 = axes[ax_idx]; ax_idx += 1
                val = valid_ratios[key]
                # green normal zone
                ax2.barh([0], [norm_hi - norm_lo], left=norm_lo, height=0.55,
                         color="#dcfce7", edgecolor="#86efac", linewidth=0.8, zorder=1)
                # patient bar
                bar_col = "#22c55e" if norm_lo <= val <= norm_hi else (
                    "#eab308" if abs(val - (norm_lo + norm_hi)/2) < 0.3 else "#ef4444")
                ax2.barh([0], [val - vmin], left=vmin, height=0.4, color=bar_col,
                         alpha=0.9, zorder=2)
                ax2.set_xlim(vmin, vmax)
                ax2.set_yticks([0]); ax2.set_yticklabels([label], fontsize=8.5)
                ax2.axvline(norm_lo, color="#16a34a", linewidth=0.8, linestyle="--", alpha=0.7)
                ax2.axvline(norm_hi, color="#16a34a", linewidth=0.8, linestyle="--", alpha=0.7)
                ax2.text(val + 0.03, 0, f"{val:.2f}", va="center", fontsize=8, fontweight="bold")
                ax2.spines["top"].set_visible(False); ax2.spines["right"].set_visible(False)
                ax2.set_facecolor("#f8fafc")
            fig2.patch.set_facecolor("#ffffff")
            plt.tight_layout(pad=0.4)
            sens_chart = os.path.join(os.path.dirname(pdf_path), "sensory_chart.png")
            plt.savefig(sens_chart, dpi=150, bbox_inches="tight")
            plt.close(fig2)
            if os.path.isfile(sens_chart):
                story.append(RLImage(sens_chart, width=W_avail, height=min(10*RL_CM, n_sens*2.4*RL_CM)))
        story.append(Spacer(1, 0.4*RL_CM))

    # ---- Empty results diagnostic ----
    if not results_by_c and debug_info:
        story.append(Paragraph("Diagnostic – aucune donnee analysee", style_h2))
        n_rows = debug_info.get("csv_rows", 0)
        cond_dist = debug_info.get("csv_conditions", {})
        story.append(Paragraph(
            f"Le fichier CSV contient {n_rows} ligne(s) de donnees. "
            f"Distribution des conditions : {cond_dist if cond_dist else 'aucune'}. "
            "Veuillez verifier que la tare ET le centrage ont ete effectues avant de demarrer le SOT, "
            "et que chaque condition a ete lancee via le bouton START.",
            style_body
        ))
        story.append(Spacer(1, 0.3*RL_CM))

    # ---- Stability bar chart ----
    if stab_values:
        story.append(Paragraph("Profil de stabilite (%)", style_h2))
        fig, ax = plt.subplots(figsize=(7, 2.2))
        conditions = [f"C{c}" for c in sorted(stab_values.keys())]
        values = [stab_values[c] for c in sorted(stab_values.keys())]
        bar_colors = []
        for v in values:
            if v >= 75: bar_colors.append("#22c55e")
            elif v >= 50: bar_colors.append("#eab308")
            else: bar_colors.append("#ef4444")
        bars = ax.bar(conditions, values, color=bar_colors, edgecolor="white", linewidth=0.8, width=0.6)
        ax.axhline(75, color="#16a34a", linestyle="--", linewidth=1, alpha=0.7, label="Seuil normal (75%)")
        ax.axhline(50, color="#dc2626", linestyle=":", linewidth=1, alpha=0.6, label="Seuil pathologique (50%)")
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                    f"{val:.0f}%", ha="center", va="bottom", fontsize=8, fontweight="bold")
        ax.set_ylim(0, 110)
        ax.set_ylabel("Stabilite (%)", fontsize=9)
        ax.legend(fontsize=8, loc="upper right")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_facecolor("#f8fafc")
        fig.patch.set_facecolor("#ffffff")
        plt.tight_layout()
        chart_path = os.path.join(os.path.dirname(pdf_path), "stability_chart.png")
        plt.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        if os.path.isfile(chart_path):
            story.append(RLImage(chart_path, width=W_avail, height=6*RL_CM))
        story.append(Spacer(1, 0.4*RL_CM))

    # ---- Statokinesigrams grid ----
    if img_paths:
        story.append(Paragraph("Statokinesiegrammes COP (X vs Y)", style_h2))
        story.append(Spacer(1, 0.15*RL_CM))
        img_w = (W_avail - 0.5*RL_CM) / 2
        grid = []
        row_buf = []
        for c in range(1, 7):
            if c in img_paths:
                row_buf.append(RLImage(img_paths[c], width=img_w, height=img_w))
            else:
                row_buf.append(Spacer(1, img_w))
            if len(row_buf) == 2:
                grid.append(row_buf)
                row_buf = []
        if row_buf:
            row_buf.append(Spacer(1, img_w))
            grid.append(row_buf)
        if grid:
            tbl_g = Table(grid, colWidths=[img_w, img_w])
            tbl_g.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                                        ("ALIGN",(0,0),(-1,-1),"CENTER")]))
            story.append(tbl_g)

    # ---- Footer ----
    story.append(Spacer(1, 0.5*RL_CM))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=colors.HexColor("#cbd5e1"), spaceBefore=4))
    story.append(Paragraph(
        "PosturoSPS – Systeme de posturographie stabilometrique  |  Rapport genere automatiquement",
        ParagraphStyle("footer", fontName=fn, fontSize=8,
                       textColor=colors.HexColor("#94a3b8"), alignment=TA_CENTER)
    ))

    doc.build(story)
    print(f"[PDF] Built: {pdf_path}")


# Monkey-patch the PDF builder in the original module
_srv.build_multitest_like_pdf = _build_sot_pdf

# =========================================================
# PATCHED analyze_sot_csv – better type handling + debug info
# =========================================================
def _patched_analyze_sot_csv(csv_path):
    """Drop-in replacement with robust condition type handling."""
    import pandas as pd
    df = pd.read_csv(csv_path)
    required = {"time", "condition", "cop_x_cm", "cop_y_cm"}
    if not required.issubset(set(df.columns)):
        raise RuntimeError("CSV colonnes manquantes")

    # Normalize condition column to int (handles float/string representations)
    df["condition"] = pd.to_numeric(df["condition"], errors="coerce").fillna(0).astype(int)

    csv_rows = len(df)
    cond_dist = {str(k): int(v) for k, v in df["condition"].value_counts().items()} if csv_rows else {}
    print(f"[ANALYZE] CSV rows={csv_rows} condition distribution={cond_dist}")

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
        if win is not None and len(win) >= 10 and "error" not in res:
            img_paths[c] = _srv.plot_statok_png(win, res, out_dir)

    json_path = os.path.join(out_dir, "results.json")
    payload = {
        "source_csv": os.path.basename(csv_path),
        "generated_at": datetime.now().isoformat(),
        "protocol": _srv.SOT_PROTOCOL,
        "results": [results_by_c[k] for k in sorted(results_by_c.keys())],
        "csv_rows": csv_rows,
        "csv_conditions": cond_dist,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

    pdf_path = os.path.join(out_dir, "report.pdf")
    _build_sot_pdf(pdf_path, csv_path, results_by_c, img_paths,
                   debug_info={"csv_rows": csv_rows, "csv_conditions": cond_dist})
    return out_dir, json_path, pdf_path

_srv.analyze_sot_csv = _patched_analyze_sot_csv

# Also expose a debug endpoint for SOT CSV inspection
@app.route("/sot/csv-debug")
def sot_csv_debug():
    try:
        import pandas as pd
        p = _srv.current_log_path
        if not p or not os.path.isfile(p):
            return _json_resp({"error": "no log file", "path": p})
        df = pd.read_csv(p)
        df["condition"] = pd.to_numeric(df["condition"], errors="coerce").fillna(0).astype(int)
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
    {"id":"vest","name":"Vestibulaire","icon":"🌀","color":"vest",
     "desc":"VOR + cible + opto 12 min",
     "sequence":[
       {"ex":"ex5","duration":120,"params":{"platform":"fixed","vor_mode":"lr","vor_interval":5}},
       {"ex":"ex8","duration":120,"params":{"platform":"fixed","target_mode":"random","difficulty":"medium"}},
     ]},
    {"id":"proprio","name":"Proprioception","icon":"⚖️","color":"proprio",
     "desc":"Sinus + impulsions 15 min",
     "sequence":[
       {"ex":"ex2","duration":120,"params":{"amplitude":"low","speed":"low"}},
       {"ex":"ex4","duration":150,"params":{"amplitude":"medium","speed":"medium"}},
       {"ex":"ex3","duration":90, "params":{"amplitude":"medium","speed":"medium"}},
     ]},
    {"id":"dual","name":"Double tache","icon":"🧠","color":"dual",
     "desc":"Citations + COP 12 min",
     "sequence":[
       {"ex":"ex7","duration":120,"params":{"platform":"sinus","amplitude":"low","speed":"low"}},
       {"ex":"ex9","duration":120,"params":{"platform":"fixed","sequence":"cross","difficulty":"medium"}},
     ]},
    {"id":"senior","name":"Senior securisee","icon":"🤝","color":"senior",
     "desc":"Doux et progressif 10 min",
     "sequence":[
       {"ex":"ex1","duration":60, "params":{"platform":"fixed"}},
       {"ex":"ex6","duration":120,"params":{"platform":"fixed","point_mode":"lr","point_speed":"low"}},
       {"ex":"ex8","duration":120,"params":{"platform":"fixed","difficulty":"low"}},
     ]},
    {"id":"sport","name":"Retour sport","icon":"🏃","color":"sport",
     "desc":"Dynamique et reactif 20 min",
     "sequence":[
       {"ex":"ex4", "duration":120,"params":{"amplitude":"high","speed":"high"}},
       {"ex":"ex11","duration":180,"params":{"platform":"sinus","difficulty":"high"}},
       {"ex":"ex10","duration":120,"params":{"platform":"auto","difficulty":"high"}},
     ]},
    {"id":"cervical","name":"Cervical","icon":"🔄","color":"cervical",
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
# EXERCISE 13 – PONG (COP-controlled paddle)
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
            # Same normalisation as ex8-11: cop_x_f / 4.0 cm→norm
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
        title=f"PONG – {diff.capitalize()}",
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
    # Start COP→cursor_x feed loop
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
