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

# =========================================================
# MPV PLAYER (for Ex12 video on the Pi screen)
# =========================================================
_mpv_proc = None
_mpv_lock = threading.Lock()

def _mpv_stop():
    global _mpv_proc
    with _mpv_lock:
        if _mpv_proc and _mpv_proc.poll() is None:
            _mpv_proc.terminate()
            try:
                _mpv_proc.wait(timeout=3)
            except Exception:
                _mpv_proc.kill()
        _mpv_proc = None
    print("[MPV] Stopped")

def _mpv_play(filepath, loop=True):
    global _mpv_proc
    _mpv_stop()
    if not filepath or not os.path.isfile(filepath):
        print(f"[MPV] File not found: {filepath}")
        return False
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    cmd = [
        "mpv",
        "--fullscreen",
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
# OVERRIDE exercise12 to use MPV for video
# =========================================================
_orig_ex12_start = _srv.exercise12_start
_orig_ex12_stop  = _srv.exercise12_stop

def _ex12_start_mpv():
    """Ex12 start: platform control same as original, video via MPV."""
    global _mpv_proc

    # Stop any running exercise
    _srv.exercise12_stop()
    _srv.exercise12_running = True

    # Build playlist (from both dirs)
    playlist = list_all_videos()
    if not playlist:
        playlist = [_srv.exercise12_mode.get("video_file", "voiture1.mp4")]
    vf = _srv.exercise12_mode.get("video_file", playlist[0])
    if vf not in playlist:
        vf = playlist[0]
    _srv.exercise12_mode["video_file"] = vf
    _srv.exercise12_video_index = playlist.index(vf) if vf in playlist else 0

    # Platform control
    platform = _srv.exercise12_mode.get("platform", "fixed")
    if platform == "fixed":
        _srv.send_to_esp = False
        _srv.control_source = "cop"
        _srv.esp_send("STOP")
    elif platform == "auto":
        _srv.send_to_esp = True
        _srv.control_source = "cop"
        _srv.esp_send("ARM:1")
        time.sleep(0.1)
        _srv.esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        _srv.send_to_esp = True
        _srv.control_source = "exercise12"
        _srv.esp_send("ARM:1")
        time.sleep(0.1)
        _srv.esp_send("AUTO:1")
        if platform == "sinus":
            threading.Thread(target=_srv.exercise12_loop_sinus, daemon=True).start()
        elif platform == "ramp":
            threading.Thread(target=_srv.exercise12_loop_ramp, daemon=True).start()
        else:
            threading.Thread(target=_srv.exercise12_loop_impulses, daemon=True).start()

    # VIDEO: use MPV if video_on == "on"
    if _srv.exercise12_mode.get("video_on", "on") == "on":
        p = video_path(vf)
        _srv.set_hdmi(mode="black", title="Ex 12 – Plateforme + Vidéo")
        if p:
            _mpv_play(p, loop=True)
            # Playlist loop in background
            if (_srv.exercise12_mode.get("video_mode", "single") == "playlist"
                    and len(playlist) > 1):
                threading.Thread(
                    target=_ex12_mpv_playlist_loop,
                    args=(playlist,), daemon=True
                ).start()
        else:
            print(f"[EX12] Video not found: {vf}")
    else:
        _srv.set_hdmi(mode="black", title="Ex 12 – Plateforme")

def _ex12_stop_mpv():
    """Ex12 stop: stop platform + MPV."""
    _srv.exercise12_running = False
    _srv.send_to_esp = False
    _srv.control_source = "cop"
    _srv.esp_send("STOP")
    _srv.set_hdmi(mode="off")
    _mpv_stop()

def _ex12_mpv_playlist_loop(playlist):
    """Cycle through playlist files via MPV."""
    try:
        interval = max(5, int(_srv.exercise12_mode.get("video_interval", 20)))
    except Exception:
        interval = 20
    idx = _srv.exercise12_video_index
    while _srv.exercise12_running:
        t0 = time.time()
        while _srv.exercise12_running and (time.time() - t0 < interval):
            time.sleep(0.3)
        if not _srv.exercise12_running:
            break
        idx = (idx + 1) % len(playlist)
        _srv.exercise12_video_index = idx
        _srv.exercise12_mode["video_file"] = playlist[idx]
        p = video_path(playlist[idx])
        if p:
            _mpv_play(p, loop=True)

# Patch the module-level functions so Flask routes call these
_srv.exercise12_start = _ex12_start_mpv
_srv.exercise12_stop  = _ex12_stop_mpv

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


def _build_sot_pdf(pdf_path, source_csv, results_by_c, img_paths):
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
