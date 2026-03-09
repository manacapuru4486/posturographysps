#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, Response, send_file, request
from Phidget22.Phidget import *
from Phidget22.Devices.VoltageRatioInput import *
import math
import threading
import time
import json
import serial
import argparse
import csv
import os
import subprocess
from datetime import datetime
import random

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm as RL_CM
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet

app = Flask(__name__, static_folder="static")

# ==========================================================
# GEOMETRIE 3 CAPTEURS (cm)
# Distances:
# C1-C2 = 40
# C1-C3 = 40.5
# C2-C3 = 40.5
# On place C1 (-20,0), C2 (+20,0), C3 (0,h)
# ==========================================================
BASE_CM = 40.0
SIDE_CM = 40.5
H_CM = math.sqrt(SIDE_CM**2 - (BASE_CM/2)**2)  # ~ 35.22 cm

SENSORS = {
    "C1": {"x": -BASE_CM/2, "y": 0.0},
    "C2": {"x": +BASE_CM/2, "y": 0.0},
    "C3": {"x": 0.0,        "y": H_CM},
}

# ==========================================================
# PARAMS CONTROLE "ULTRA STABLE"
# ==========================================================
# Filtrage COP (0..1). Plus petit = plus stable mais plus lent.
COP_ALPHA = 0.18

# Deadzone en cm (autour du centre)
DEADZONE_CM = 0.40

# Proportionnel doux (cmd ~= KP * y_cm)
KP = 0.05  # 0.025..0.055 (plus grand = plus rÃ©actif)

# Limite cmd (format ESP : COP:Y: +/-0.40)
CMD_MAX = 0.40

# Slew-rate sur cmd (variation max par seconde)
CMD_SLEW_PER_S = 1.20  # ultra stable : 0.5..1.2

# Si total de "poids" trop faible => personne dessus => cmd=0
TOTAL_MIN = 0.00020

# Normalisation Y -> cmd : cmd = KP * y_cm / Y_SCALE
Y_SCALE_CM = 12.0  # plus grand => moins agressif

# Inversion sens si besoin (si Ã§a part Ã  l'envers)
INVERT_Y_CMD = True

# ==========================================================
# CONDITIONS SOT
# ==========================================================
SOT_CONDITIONS = {
    1: {"name": "EO STABLE", "duration": 20, "plateau": "stable", "vision": "EO", "opto": False},
    2: {"name": "EC STABLE", "duration": 20, "plateau": "stable", "vision": "EC", "opto": False},
    3: {"name": "EO OPTO", "duration": 35, "plateau": "stable", "vision": "EO", "opto": True, "analysis_start": 15},
    4: {"name": "EO INSTABLE", "duration": 20, "plateau": "auto", "vision": "EO", "opto": False},
    5: {"name": "EC INSTABLE", "duration": 20, "plateau": "auto", "vision": "EC", "opto": False},
    6: {"name": "OPTO INSTABLE", "duration": 35, "plateau": "auto", "vision": "OPTO", "opto": True, "analysis_start": 15},
}

# ==========================================================
# ETAT GLOBAL
# ==========================================================
channels = []
tare_raw = [0.0, 0.0, 0.0]
tare_ready = False

offset_x_cm = 0.0
offset_y_cm = 0.0
offset_ready = False

cop_x_f = 0.0
cop_y_f = 0.0
cmd_f = 0.0

latest = {
    "raw": [0.0, 0.0, 0.0],
    "w":   [0.0, 0.0, 0.0],
    "total": 0.0,
    "cop_x_cm": 0.0,
    "cop_y_cm": 0.0,
    "cmd": 0.0,
    "tare_ready": False,
    "offset_ready": False,
    "send_to_esp": False,
}

lock = threading.Lock()

# UART ESP32
uart = None
send_to_esp = False

# LOGGING
logging_active = False
log_file = None
log_writer = None
current_condition = 0
esp_pos = -1
esp_pos_min_safe = -1
esp_pos_max_safe = -1
esp_front_is_min = 0

# SOT
sot_running = False
sot_condition = 1
sot_start_time = 0

# OPTO
opto_process = None

# EXERCICE
exercise_running = False
exercise_mode = {
    "platform": "fixed",
    "screen": "black",
    "direction": "right",
    "speed": 6
}

# EXERCICE 2 - SINUS
control_source = "cop"   # cop / exercise2 / exercise3
exercise2_running = False
exercise2_mode = {
    "amplitude": "medium",
    "speed": "medium",
    "screen": "black",
    "direction": "right",
    "opto_speed": 6
}

# EXERCICE 3 - PETIT GRAND PETIT
exercise3_running = False
exercise3_mode = {
    "amplitude": "medium",
    "speed": "medium",
    "screen": "black",
    "direction": "right",
    "opto_speed": 6,
    "duration": 30
}

# EXERCICE 4 - IMPULSIONS ALEATOIRES
exercise4_running = False
exercise4_mode = {
    "amplitude": "medium",
    "speed": "medium",
    "screen": "black",
    "direction": "right",
    "opto_speed": 6,
    "gap_min": 1.5,
    "gap_max": 4.0,
    "pulse_ms": 900
}

# Analyse auto (SOT -> PDF)
current_log_path = None
latest_results_dir = None
latest_pdf_path = None
latest_json_path = None

# ==========================================================
# PHIDGETS
# ==========================================================
def init_phidgets():
    # IMPORTANT: ici on prend channels 1,2,3 comme dans tes scripts.
    for i in [1, 2, 3]:
        ch = VoltageRatioInput()
        ch.setChannel(i)
        ch.openWaitForAttachment(5000)
        ch.setBridgeEnabled(True)
        ch.setBridgeGain(BridgeGain.BRIDGE_GAIN_8)
        ch.setDataInterval(20)  # 50 Hz
        channels.append(ch)

def read_raw():
    r1 = channels[0].getVoltageRatio()
    r2 = channels[1].getVoltageRatio()
    r3 = channels[2].getVoltageRatio()
    return [r1, r2, r3]

def tare():
    """Tare = valeur Ã  vide (ou centre immobile, selon ton protocole).
       Ici : on mesure la rÃ©fÃ©rence, puis w = max(tare - raw, 0) car raw diminue sous charge.
    """
    global tare_raw, tare_ready
    time.sleep(0.2)

    sums = [0.0, 0.0, 0.0]
    n = 40
    for _ in range(n):
        r = read_raw()
        sums[0] += r[0]
        sums[1] += r[1]
        sums[2] += r[2]
        time.sleep(0.01)
    tare_raw = [s / n for s in sums]
    tare_ready = True

    with lock:
        latest["tare_ready"] = True

def get_weights():
    """Convertit raw -> poids positifs proportionnels.
       D'aprÃ¨s ton diagnostic: C1,C2,C3 diminuent sous charge.
       Donc delta = tare - raw (positif si charge), puis clamp >=0.
    """
    r = read_raw()
    if not tare_ready:
        return r, [0.0, 0.0, 0.0], 0.0

    w = [max(tare_raw[i] - r[i], 0.0) for i in range(3)]
    total = w[0] + w[1] + w[2]
    return r, w, total

def compute_cop_cm(w, total):
    if total <= 1e-12:
        return 0.0, 0.0
    x = (w[0]*SENSORS["C1"]["x"] + w[1]*SENSORS["C2"]["x"] + w[2]*SENSORS["C3"]["x"]) / total
    y = (w[0]*SENSORS["C1"]["y"] + w[1]*SENSORS["C2"]["y"] + w[2]*SENSORS["C3"]["y"]) / total
    return x, y

# ==========================================================
# OFFSET CENTRE
# ==========================================================
def set_center_offset():
    """Fixe offset_x_cm/offset_y_cm pour que le centre devienne (0,0)."""
    global offset_x_cm, offset_y_cm, offset_ready, cop_x_f, cop_y_f, cmd_f
    time.sleep(0.2)

    sums_x = 0.0
    sums_y = 0.0
    n = 40
    valid = 0

    for _ in range(n):
        r, w, total = get_weights()
        if total > TOTAL_MIN:
            x, y = compute_cop_cm(w, total)
            sums_x += x
            sums_y += y
            valid += 1
        time.sleep(0.01)

    if valid == 0:
        # pas de charge, on ne peut pas centrer
        return False

    offset_x_cm = sums_x / valid
    offset_y_cm = sums_y / valid
    offset_ready = True

    # reset filtres
    cop_x_f = 0.0
    cop_y_f = 0.0
    cmd_f = 0.0

    with lock:
        latest["offset_ready"] = True
    return True

# ==========================================================
# CONTROLE ULTRA STABLE
# ==========================================================
def clamp(x, a, b):
    return a if x < a else (b if x > b else x)

def update_control_loop():
    """Boucle: calcule COP filtrÃ© + commande cmd_f filtrÃ©e/slewrate et envoie COP:Y:x Ã  l'ESP."""
    global cop_x_f, cop_y_f, cmd_f

    last_t = time.time()
    last_pos_req = 0.0

    while True:
        now = time.time()
        dt = now - last_t
        if dt <= 0.0:
            dt = 0.01
        last_t = now

        # Demander position toutes les 200ms
        if send_to_esp and uart:
            if now - last_pos_req > 0.2:
                last_pos_req = now
                try:
                    uart.write(b"POS?\n")
                except:
                    pass

        r, w, total = get_weights()

        # Si pas de tare ou pas d'offset : on ne pilote pas
        if (not tare_ready) or (not offset_ready):
            with lock:
                latest["raw"] = r
                latest["w"] = w
                latest["total"] = total
                latest["cop_x_cm"] = 0.0
                latest["cop_y_cm"] = 0.0
                latest["cmd"] = 0.0
                latest["send_to_esp"] = send_to_esp
            time.sleep(0.02)
            continue

        # SÃ©curitÃ© "personne dessus"
        if total < TOTAL_MIN:
            # on ramÃ¨ne cmd_f doucement vers 0 (anti-jerk)
            # slew vers 0
            max_step = CMD_SLEW_PER_S * dt
            if cmd_f > 0:
                cmd_f = max(0.0, cmd_f - max_step)
            elif cmd_f < 0:
                cmd_f = min(0.0, cmd_f + max_step)

            cop_x_f = (1 - COP_ALPHA) * cop_x_f
            cop_y_f = (1 - COP_ALPHA) * cop_y_f

            if send_to_esp and uart:
                uart.write(b"COP:Y:0.000\n")

            with lock:
                latest["raw"] = r
                latest["w"] = w
                latest["total"] = total
                latest["cop_x_cm"] = 0.0
                latest["cop_y_cm"] = 0.0
                latest["cmd"] = 0.0
                latest["send_to_esp"] = send_to_esp
            time.sleep(0.02)
            continue

        # COP brut en cm
        x_cm, y_cm = compute_cop_cm(w, total)

        # recentrage
        x_cm -= offset_x_cm
        y_cm -= offset_y_cm

        # filtre EMA
        cop_x_f = COP_ALPHA * x_cm + (1 - COP_ALPHA) * cop_x_f
        cop_y_f = COP_ALPHA * y_cm + (1 - COP_ALPHA) * cop_y_f

        # DEADZONE en cm
        y_eff = cop_y_f
        if abs(y_eff) < DEADZONE_CM:
            y_eff = 0.0

        # Loi P douce
        cmd_target = (KP * (y_eff / Y_SCALE_CM))  # -> approx [-0.4..+0.4]
        if INVERT_Y_CMD:
            cmd_target = -cmd_target

        # clamp
        cmd_target = clamp(cmd_target, -CMD_MAX, +CMD_MAX)

        # Slew-rate (limite variation cmd par seconde)
        max_step = CMD_SLEW_PER_S * dt
        delta = cmd_target - cmd_f
        if delta > max_step:
            delta = max_step
        elif delta < -max_step:
            delta = -max_step
        cmd_f += delta

        # clamp final
        cmd_f = clamp(cmd_f, -CMD_MAX, +CMD_MAX)

        # Normalisation COP vers [-1 .. 1]
        cop_norm = cop_y_f / Y_SCALE_CM
        cop_norm = max(-1.0, min(1.0, cop_norm))
        if abs(cop_norm) < 0.02:
            cop_norm = 0.0

        # Envoi ESP (seulement si source = cop, pas pendant exercice sinus)
        if send_to_esp and uart and control_source == "cop":
            uart.write(f"COP:Y:{cop_norm:.3f}\n".encode("ascii"))

        with lock:
            latest["raw"] = r
            latest["w"] = w
            latest["total"] = total
            latest["cop_x_cm"] = float(cop_x_f)
            latest["cop_y_cm"] = float(cop_y_f)
            latest["cmd"] = float(cop_norm)
            latest["send_to_esp"] = send_to_esp

        # LOGGING SOT
        if logging_active and log_writer:
            try:
                log_writer.writerow([
                    time.time(), current_condition,
                    cop_x_f, cop_y_f, total,
                    cop_norm, esp_pos, ""
                ])
            except:
                pass

        time.sleep(0.02)  # ~50 Hz

# ==========================================================
# UART READER (parse position ESP32)
# ==========================================================
def uart_reader():
    global esp_pos, esp_pos_min_safe, esp_pos_max_safe, esp_front_is_min
    while True:
        if uart and uart.in_waiting:
            try:
                line = uart.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                print(f"[ESP32] {line}")
                if line.startswith("POS_RAW="):
                    try: esp_pos = int(line.split("=")[1])
                    except: pass
                if "pos=" in line:
                    for part in line.split():
                        if part.startswith("pos="):
                            try: esp_pos = int(part.split("=")[1])
                            except: pass
                        elif part.startswith("minS="):
                            try: esp_pos_min_safe = int(part.split("=")[1])
                            except: pass
                        elif part.startswith("maxS="):
                            try: esp_pos_max_safe = int(part.split("=")[1])
                            except: pass
                        elif part.startswith("frontIsMin="):
                            try: esp_front_is_min = int(part.split("=")[1])
                            except: pass
            except:
                pass
        time.sleep(0.005)

# ==========================================================
# UART helpers
# ==========================================================
def esp_send(line: str):
    if uart:
        uart.write((line.strip() + "\n").encode("ascii", errors="ignore"))

# ==========================================================
# LOGGING SOT
# ==========================================================`r`ndef start_log():
    global logging_active, log_file, log_writer, current_log_path
    os.makedirs("logs", exist_ok=True)
    fname = datetime.now().strftime("logs/sot_%Y%m%d_%H%M%S.csv")
    current_log_path = fname
    log_file = open(fname, "w", newline="")
    log_writer = csv.writer(log_file)
    log_writer.writerow([
        "time", "condition", "cop_x_cm", "cop_y_cm",
        "total", "cmd", "esp_pos", "blocked"
    ])
    logging_active = True
    print(f"[LOG] started: {fname}")

def stop_log():
    global logging_active, log_file
    logging_active = False
    if log_file:
        log_file.close()
        log_file = None
    print("[LOG] stopped")

# ==========================================================
# OPTOCINETIC HDMI (Chromium lancÃ© une seule fois)
# ==========================================================
opto_process = None
hdmi_state = {
    "mode": "off",
    "direction": "right",
    "speed": 6,
    "stripe": 80,
    "vor_mode": "lr",
    "vor_interval": 2,
    "vor_pair": "lr",
    "point_mode": "lr",
    "point_speed": "medium",
    "quote": "",
    "target_x": 0.0,
    "target_y": 0.0,
    "cursor_x": 0.0,
    "cursor_y": 0.0,
    "target_r": 0.18,
    "score_percent": 0.0,
    "hold_time": 0.0,
    "goal_s": 5.0,
    "show_badge": 0,
    "seq_points": [],
    "seq_index": 0,
    "path_points": [],
    "path_index": 0,
    "maze_points": [],
    "maze_index": 0,
    "maze_width": 0.14,
    "maze_offtrack": 0,
    "title": "",
    "video_file": "voiture1.mp4",
    "video_playlist": [],
    "video_index": 0
}


def list_static_videos():
    try:
        folder = os.path.join(app.root_path, "static")
        if not os.path.isdir(folder):
            return []
        vids = []
        for n in os.listdir(folder):
            if n.lower().endswith(".mp4") and os.path.isfile(os.path.join(folder, n)):
                vids.append(n)
        vids.sort()
        return vids
    except:
        return []
def ensure_chromium():
    """Lance Chromium une seule fois sur /hdmi, il reste ouvert."""
    global opto_process
    if opto_process is not None:
        # VÃ©rifier s'il tourne encore
        if opto_process.poll() is None:
            return  # dÃ©jÃ  lancÃ©
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    opto_process = subprocess.Popen([
        "chromium", "--kiosk", "--noerrdialogs", "--disable-infobars",
        "--disable-restore-session-state", "--no-first-run",
        "http://localhost:5000/hdmi"
    ], env=env)

def set_hdmi(mode=None, direction=None, speed=None, stripe=None,
             vor_mode=None, vor_interval=None, vor_pair=None,
             point_mode=None, point_speed=None, quote=None,
             target_x=None, target_y=None, cursor_x=None, cursor_y=None, target_r=None,
             score_percent=None,
             hold_time=None, goal_s=None, show_badge=None,
             seq_points=None, seq_index=None,
             path_points=None, path_index=None,
             maze_points=None, maze_index=None, maze_width=None, maze_offtrack=None,
             title=None, video_file=None, video_playlist=None, video_index=None):
    global hdmi_state
    if mode is not None:
        hdmi_state["mode"] = mode
    if direction is not None:
        hdmi_state["direction"] = direction
    if speed is not None:
        try: hdmi_state["speed"] = int(speed)
        except: pass
    if stripe is not None:
        try: hdmi_state["stripe"] = int(stripe)
        except: pass
    if vor_mode is not None:
        hdmi_state["vor_mode"] = vor_mode
    if vor_interval is not None:
        try: hdmi_state["vor_interval"] = int(vor_interval)
        except: pass
    if vor_pair is not None:
        hdmi_state["vor_pair"] = vor_pair
    if point_mode is not None:
        hdmi_state["point_mode"] = point_mode
    if point_speed is not None:
        hdmi_state["point_speed"] = point_speed
    if quote is not None:
        hdmi_state["quote"] = quote
    if target_x is not None:
        hdmi_state["target_x"] = float(target_x)
    if target_y is not None:
        hdmi_state["target_y"] = float(target_y)
    if cursor_x is not None:
        hdmi_state["cursor_x"] = float(cursor_x)
    if cursor_y is not None:
        hdmi_state["cursor_y"] = float(cursor_y)
    if target_r is not None:
        hdmi_state["target_r"] = float(target_r)
    if score_percent is not None:
        hdmi_state["score_percent"] = float(score_percent)
    if hold_time is not None:
        hdmi_state["hold_time"] = float(hold_time)
    if goal_s is not None:
        hdmi_state["goal_s"] = float(goal_s)
    if show_badge is not None:
        hdmi_state["show_badge"] = int(show_badge)
    if seq_points is not None:
        hdmi_state["seq_points"] = seq_points
    if seq_index is not None:
        hdmi_state["seq_index"] = int(seq_index)
    if path_points is not None:
        hdmi_state["path_points"] = path_points
    if path_index is not None:
        hdmi_state["path_index"] = int(path_index)
    if maze_points is not None:
        hdmi_state["maze_points"] = maze_points
    if maze_index is not None:
        hdmi_state["maze_index"] = int(maze_index)
    if maze_width is not None:
        hdmi_state["maze_width"] = float(maze_width)
    if maze_offtrack is not None:
        hdmi_state["maze_offtrack"] = int(maze_offtrack)
    if title is not None:
        hdmi_state["title"] = str(title)
    if video_file is not None:
        hdmi_state["video_file"] = str(video_file)
    if video_playlist is not None:
        hdmi_state["video_playlist"] = list(video_playlist)
    if video_index is not None:
        hdmi_state["video_index"] = int(video_index)

def start_black_screen():
    ensure_chromium()
    set_hdmi(mode="black")

def start_opto(direction="right", speed=6):
    ensure_chromium()
    set_hdmi(mode="opto", direction=direction, speed=speed)

def stop_opto():
    set_hdmi(mode="off")

# ==========================================================
# SOT CONDITIONS
# ==========================================================
def start_condition(c):
    global current_condition, sot_condition, sot_start_time, send_to_esp
    cond = SOT_CONDITIONS[c]
    sot_condition = c
    current_condition = c
    sot_start_time = time.time()
    print(f"[SOT] START CONDITION {c}: {cond['name']}")
    if cond["plateau"] == "auto":
        send_to_esp = True
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("AUTO:1")
    else:
        send_to_esp = False
        esp_send("AUTO:0")
    if cond["opto"]:
        start_opto()
    else:
        start_black_screen()

def stop_condition():
    global send_to_esp
    send_to_esp = False
    esp_send("STOP")
    set_hdmi("off")

def kill_chromium():
    """Tue Chromium Ã  la fin complÃ¨te du SOT."""
    global opto_process
    if opto_process:
        opto_process.kill()
        opto_process = None

# ==========================================================
# WEB UI MINIMALE
# ==========================================================
HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>COG Ultra Stable</title>
  <style>
    body { font-family: Arial, sans-serif; padding: 14px; }
    button { padding: 10px 12px; margin: 6px 6px 6px 0; }
    .box { padding: 12px; border: 1px solid #ddd; border-radius: 8px; margin-top: 10px; }
    .row { display:flex; gap: 14px; flex-wrap: wrap; }
    .k { color:#666; }
    pre { background:#f7f7f7; padding:10px; border-radius:8px; overflow:auto; }
  </style>
</head>
<body>
  <h2>COG Ultra Stable</h2>

  <div class="row">
    <button onclick="call('/tare')">1) TARE</button>
    <button onclick="call('/center')">2) SET CENTER OFFSET</button>
    <button onclick="call('/esp/home')">ESP: HOME</button>
    <button onclick="call('/esp/center')">ESP: CENTER</button>
  </div>

  <div class="row">
    <button onclick="call('/esp/start')">START ASSERV</button>
    <button onclick="call('/esp/stop')">STOP</button>
  </div>

  <div class="box">
    <div><span class="k">COP (cm):</span> X=<b id="x">0</b> Y=<b id="y">0</b></div>
    <div><span class="k">CMD:</span> <b id="cmd">0</b> <span class="k"> | total:</span> <b id="total">0</b></div>
    <div><span class="k">tare_ready:</span> <b id="tr">0</b> <span class="k">offset_ready:</span> <b id="or">0</b> <span class="k">send_to_esp:</span> <b id="se">0</b></div>
  </div>

  <div class="box">
    <div class="k">Debug:</div>
    <pre id="dbg"></pre>
  </div>

<script>
async function call(url){
  const r = await fetch(url);
  const t = await r.text();
  console.log(t);
}
async function tick(){
  const r = await fetch('/status');
  const s = await r.json();
  document.getElementById('x').textContent = s.cop_x_cm.toFixed(2);
  document.getElementById('y').textContent = s.cop_y_cm.toFixed(2);
  document.getElementById('cmd').textContent = s.cmd.toFixed(3);
  document.getElementById('total').textContent = s.total.toFixed(6);
  document.getElementById('tr').textContent = s.tare_ready ? "1" : "0";
  document.getElementById('or').textContent = s.offset_ready ? "1" : "0";
  document.getElementById('se').textContent = s.send_to_esp ? "1" : "0";
  document.getElementById('dbg').textContent =
    "raw=" + JSON.stringify(s.raw) + "\\n" +
    "w=" + JSON.stringify(s.w);
}
setInterval(tick, 200);
tick();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/status")
def status():
    with lock:
        return Response(json.dumps({
            "raw": latest["raw"],
            "w": latest["w"],
            "total": latest["total"],
            "cop_x_cm": latest["cop_x_cm"],
            "cop_y_cm": latest["cop_y_cm"],
            "cmd": latest["cmd"],
            "tare_ready": latest["tare_ready"],
            "offset_ready": latest["offset_ready"],
            "send_to_esp": latest["send_to_esp"],
        }), mimetype="application/json")

@app.route("/tare")
def route_tare():
    tare()
    return "OK TARE\n"

@app.route("/center")
def route_center():
    ok = set_center_offset()
    return ("OK CENTER OFFSET\n" if ok else "ERROR: no load detected (total too low)\n")

@app.route("/esp/start")
def route_esp_start():
    global send_to_esp
    send_to_esp = True
    esp_send("ARM:1")
    time.sleep(0.1)
    esp_send("AUTO:1")
    return "OK ESP START (ARM + AUTO)\n"

@app.route("/esp/stop")
def route_esp_stop():
    global send_to_esp
    send_to_esp = False
    esp_send("STOP")
    return "OK ESP STOP\n"

@app.route("/esp/home")
def route_esp_home():
    esp_send("ARM:1")
    time.sleep(0.1)
    esp_send("HOME")
    return "OK ESP HOME\n"

@app.route("/esp/center")
def route_esp_center():
    esp_send("ARM:1")
    time.sleep(0.1)
    esp_send("CENTER")
    return "OK ESP CENTER\n"

@app.route("/log/start")
def route_log_start():
    start_log()
    return "LOG STARTED\n"

@app.route("/log/stop")
def route_log_stop():
    stop_log()
    return "LOG STOPPED\n"

@app.route("/sot/tare")
def sot_tare():
    tare()
    return "OK TARE\n"

@app.route("/sot/center")
def sot_center():
    ok = set_center_offset()
    if ok:
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("HOME")
        time.sleep(5)
        esp_send("CENTER")
        return "OK CENTER + HOME + CENTER\n"
    return "ERROR: no load\n"

@app.route("/sot/start/<int:c>")
def sot_start(c):
    ensure_chromium()  # lance Chromium une seule fois
    start_log()
    start_condition(c)
    return f"STARTED CONDITION {c}\n"

@app.route("/sot/stop")
def sot_stop():
    stop_condition()
    finalize_sot_and_analyze()
    return "STOP\n"

@app.route("/sot/next")
def sot_next():
    global sot_condition
    stop_condition()
    sot_condition += 1
    if sot_condition > 6:
        finalize_sot_and_analyze()
        return "SOT FINISHED\n"
    start_condition(sot_condition)
    return f"NEXT: CONDITION {sot_condition}\n"

@app.route("/sot/restart")
def sot_restart():
    start_condition(sot_condition)
    return f"RESTART CONDITION {sot_condition}\n"

@app.route("/hdmi/mode")
def hdmi_mode_route():
    return Response(json.dumps(hdmi_state), mimetype="application/json")

@app.route("/hdmi")
def hdmi():
    return """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/><title>HDMI</title></head>
<body style="margin:0;padding:0;overflow:hidden;background:#000;cursor:none">
<video id="vbg" playsinline muted autoplay loop style="position:fixed;inset:0;width:100vw;height:100vh;object-fit:cover;background:#000;display:none"></video>
<canvas id="c" style="position:fixed;inset:0;display:block"></canvas>
<script>
var canvas=document.getElementById("c");
var ctx=canvas.getContext("2d");
var vbg=document.getElementById("vbg");
function resize(){canvas.width=window.innerWidth;canvas.height=window.innerHeight;}
resize();window.onresize=resize;
var offset=0,t0=Date.now();
var mode="off",direction="right",speed=6,stripe=80;
var vor_pair="lr",point_mode="lr",point_speed="medium";
var qt="",qtY=0,lastQt="";
var tgx=0,tgy=0,crx=0,cry=0,tgr=0.18;
var hold_time=0,goal_s=5,show_badge=0,title="";
var seq_points=[],seq_index=0,path_points=[],path_index=0,maze_points=[],maze_index=0,maze_width=0.14,maze_offtrack=0;
var video_file="voiture1.mp4",active_video_src="";
var vaultImg=new Image();vaultImg.src="/static/vaultboy.png";
function drawPt(x,y,r,col){ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fillStyle=col||"#FFFFFF";ctx.fill();}
function n2s(nx,ny,sc){return [canvas.width/2+nx*sc,canvas.height/2-ny*sc];}
function drawVor(p){var w=canvas.width,h=canvas.height,mx=w*0.22,my=h*0.22;var r=Math.max(26,Math.min(w,h)*0.035);if(p==="lr"){drawPt(mx,h/2,r);drawPt(w-mx,h/2,r);}else if(p==="ud"){drawPt(w/2,my,r);drawPt(w/2,h-my,r);}else if(p==="diag1"){drawPt(mx,my,r);drawPt(w-mx,h-my,r);}else if(p==="diag2"){drawPt(w-mx,my,r);drawPt(mx,h-my,r);}}
function ptSpd(){if(point_speed==="low")return 0.6;if(point_speed==="high")return 1.8;return 1.0;}
function drawMovPt(){var w=canvas.width,h=canvas.height,r=Math.max(18,Math.min(w,h)*0.025);var mx=w*0.22,my=h*0.22,a=(Date.now()-t0)/1000.0,k=ptSpd();var x=w/2,y=h/2;if(point_mode==="lr"){x=w/2+(w/2-mx)*Math.sin(a*k);y=h/2;}else if(point_mode==="ud"){x=w/2;y=h/2+(h/2-my)*Math.sin(a*k);}else if(point_mode==="circle"){x=w/2+w*0.28*Math.cos(a*k);y=h/2+h*0.28*Math.sin(a*k);}else if(point_mode==="infinity"){x=w/2+w*0.22*Math.sin(a*k);y=h/2+h*0.18*Math.sin(2*a*k)/1.4;}drawPt(x,y,r);} 
function drawHud(){if(!title)return;ctx.fillStyle="rgba(0,0,0,0.30)";ctx.fillRect(24,20,560,48);ctx.fillStyle="#FFFFFF";ctx.font="bold 28px Arial";ctx.textAlign="left";ctx.fillText(title,34,53);} 
function drawBaseAxes(){var w=canvas.width,h=canvas.height;ctx.strokeStyle="rgba(255,255,255,0.20)";ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(w/2,h*0.15);ctx.lineTo(w/2,h*0.85);ctx.moveTo(w*0.15,h/2);ctx.lineTo(w*0.85,h/2);ctx.stroke();}
function drawTargetCore(){var w=canvas.width,h=canvas.height,sc=Math.min(w,h)*0.28;var c=n2s(crx,cry,sc),t=n2s(tgx,tgy,sc),rr=Math.max(20,tgr*sc);var dd=Math.sqrt((crx-tgx)*(crx-tgx)+(cry-tgy)*(cry-tgy));var ins=(dd<=tgr);drawBaseAxes();ctx.beginPath();ctx.arc(t[0],t[1],rr*1.35,0,Math.PI*2);ctx.fillStyle=ins?"rgba(0,255,120,0.14)":"rgba(0,255,120,0.05)";ctx.fill();ctx.beginPath();ctx.arc(t[0],t[1],rr,0,Math.PI*2);ctx.strokeStyle=ins?"#00FF88":"#52ffa6";ctx.lineWidth=5;ctx.stroke();drawPt(c[0],c[1],Math.max(14,rr*0.45),"#FFFFFF");if(show_badge&&vaultImg.complete){ctx.drawImage(vaultImg,w-180,26,150,150);} }
function drawSequence(){var sc=Math.min(canvas.width,canvas.height)*0.31;drawBaseAxes();for(var i=0;i<seq_points.length;i++){var p=seq_points[i],s=n2s(p[0],p[1],sc);drawPt(s[0],s[1],10,i===seq_index?"#ffeb3b":"rgba(255,255,255,0.35)");if(i<seq_points.length-1){var q=n2s(seq_points[i+1][0],seq_points[i+1][1],sc);ctx.strokeStyle="rgba(255,255,255,0.12)";ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(s[0],s[1]);ctx.lineTo(q[0],q[1]);ctx.stroke();}}drawTargetCore();}
function drawPath(){var sc=Math.min(canvas.width,canvas.height)*0.31;ctx.strokeStyle="rgba(70,190,255,0.65)";ctx.lineWidth=8;ctx.beginPath();for(var i=0;i<path_points.length;i++){var s=n2s(path_points[i][0],path_points[i][1],sc);if(i===0)ctx.moveTo(s[0],s[1]);else ctx.lineTo(s[0],s[1]);}ctx.stroke();for(var j=0;j<path_points.length;j++){var p=n2s(path_points[j][0],path_points[j][1],sc);drawPt(p[0],p[1],j===path_index?11:7,j===path_index?"#ffee58":"rgba(255,255,255,0.45)");}drawTargetCore();}
function drawMaze(){var sc=Math.min(canvas.width,canvas.height)*0.34;var ww=Math.max(16,maze_width*sc*2.2);ctx.lineCap="round";ctx.strokeStyle=maze_offtrack?"rgba(255,70,70,0.95)":"rgba(120,255,210,0.95)";ctx.lineWidth=ww;ctx.beginPath();for(var i=0;i<maze_points.length;i++){var s=n2s(maze_points[i][0],maze_points[i][1],sc);if(i===0)ctx.moveTo(s[0],s[1]);else ctx.lineTo(s[0],s[1]);}ctx.stroke();ctx.strokeStyle="rgba(0,0,0,0.60)";ctx.lineWidth=Math.max(2,ww*0.35);ctx.stroke();for(var k=0;k<maze_points.length;k++){var p=n2s(maze_points[k][0],maze_points[k][1],sc);if(k===0)drawPt(p[0],p[1],12,"#29b6f6");if(k===maze_points.length-1)drawPt(p[0],p[1],12,"#66ff66");if(k===maze_index)drawPt(p[0],p[1],10,"#ffee58");}drawTargetCore();}
function drawScoreBar(){var pct=Math.max(0,Math.min(1,hold_time/Math.max(0.01,goal_s)));ctx.fillStyle="rgba(255,255,255,0.12)";ctx.fillRect(40,38,320,28);ctx.fillStyle="#00FF88";ctx.fillRect(40,38,320*pct,28);ctx.strokeStyle="#FFFFFF";ctx.lineWidth=2;ctx.strokeRect(40,38,320,28);} 
function updateVideoPlayback(){
  if(mode!=="video"){vbg.style.display="none";return;}
  vbg.style.display="block";
  var src="/static/"+(video_file||"voiture1.mp4");
  if(src!==active_video_src){active_video_src=src;vbg.src=src;try{vbg.load();}catch(e){}}
  if(vbg.paused){var p=vbg.play();if(p&&p.catch)p.catch(function(){});} 
}
function draw(){
if(mode==="video"){ctx.clearRect(0,0,canvas.width,canvas.height);drawHud();requestAnimationFrame(draw);return;}
ctx.fillStyle="#000";ctx.fillRect(0,0,canvas.width,canvas.height);drawHud();
if(mode==="opto"){ctx.fillStyle="#FFF";if(direction==="right"||direction==="left"){for(var x=-stripe*2;x<canvas.width+stripe*2;x+=stripe*2){ctx.fillRect(x+offset,0,stripe,canvas.height);}if(direction==="right")offset+=speed;if(direction==="left")offset-=speed;offset=offset%(stripe*2);}if(direction==="down"||direction==="up"){for(var y=-stripe*2;y<canvas.height+stripe*2;y+=stripe*2){ctx.fillRect(0,y+offset,canvas.width,stripe);}if(direction==="down")offset+=speed;if(direction==="up")offset-=speed;offset=offset%(stripe*2);}}
if(mode==="vor")drawVor(vor_pair);if(mode==="point")drawMovPt();
if(mode==="quote"){ctx.fillStyle="#FFF";ctx.font="bold 54px Arial";ctx.textAlign="center";var mxW=canvas.width*0.75,lh=66;function wrapTxt(t,mw){var w=t.split(" "),ls=[],l="";for(var n=0;n<w.length;n++){var tl=l+w[n]+" ";if(ctx.measureText(tl).width>mw&&n>0){ls.push(l);l=w[n]+" ";}else{l=tl;}}ls.push(l);return ls;}var wl=wrapTxt(qt,mxW);for(var i=0;i<wl.length;i++){ctx.fillText(wl[i],canvas.width/2,qtY+i*lh);}}
if(mode==="target"){drawTargetCore();drawScoreBar();}
if(mode==="sequence"){drawSequence();drawScoreBar();}
if(mode==="path"){drawPath();}
if(mode==="maze"){drawMaze();}
requestAnimationFrame(draw);
}
draw();
setInterval(function(){fetch("/hdmi/mode").then(function(r){return r.json()}).then(function(d){mode=d.mode||"off";direction=d.direction||"right";speed=parseInt(d.speed||6);stripe=parseInt(d.stripe||80);vor_pair=d.vor_pair||"lr";point_mode=d.point_mode||"lr";point_speed=d.point_speed||"medium";tgx=parseFloat(d.target_x||0);tgy=parseFloat(d.target_y||0);crx=parseFloat(d.cursor_x||0);cry=parseFloat(d.cursor_y||0);tgr=parseFloat(d.target_r||0.18);hold_time=parseFloat(d.hold_time||0);goal_s=parseFloat(d.goal_s||5);show_badge=parseInt(d.show_badge||0);seq_points=d.seq_points||[];seq_index=parseInt(d.seq_index||0);path_points=d.path_points||[];path_index=parseInt(d.path_index||0);maze_points=d.maze_points||[];maze_index=parseInt(d.maze_index||0);maze_width=parseFloat(d.maze_width||0.14);maze_offtrack=parseInt(d.maze_offtrack||0);title=d.title||"";video_file=d.video_file||"voiture1.mp4";updateVideoPlayback();var nq=d.quote||"";if(nq!==lastQt){lastQt=nq;qt=nq;qtY=Math.random()*(canvas.height*0.5)+canvas.height*0.25;}}).catch(function(){});},180);
</script>
</body>
</html>"""

@app.route("/opto")
def opto():
    return """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/><title>OPTO</title></head>
<body style="margin:0;padding:0;overflow:hidden;background:#000">
<canvas id="c" style="display:block;background:#000"></canvas>
<script>
var canvas=document.getElementById("c");
var ctx=canvas.getContext("2d");
canvas.width=window.innerWidth;
canvas.height=window.innerHeight;
window.onresize=function(){
  canvas.width=window.innerWidth;
  canvas.height=window.innerHeight;
};
var offset=0;
var stripe=80;
var speed=6;
function draw(){
  ctx.fillStyle="#000000";
  ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle="#FFFFFF";
  for(var x=0;x<canvas.width+stripe*2;x+=stripe*2){
    ctx.fillRect(x+offset-stripe*2,0,stripe,canvas.height);
  }
  offset=(offset+speed)%(stripe*2);
  requestAnimationFrame(draw);
}
draw();
</script>
</body>
</html>"""

@app.route("/sot")
def sot():
    return """
<html>
<head>
<meta charset="utf-8"/>
<title>SOT Test</title>
<style>
  body { font-family: Arial, sans-serif; padding: 20px; }
  button { padding: 14px 20px; margin: 8px 4px; font-size: 16px; cursor: pointer; border: none; border-radius: 8px; color: white; }
  .step { padding: 16px; margin: 10px 0; border: 2px solid #ddd; border-radius: 10px; }
  .active { border-color: #2196F3; background: #e3f2fd; }
  .done { border-color: #4CAF50; background: #e8f5e9; }
  .btn-blue { background: #2196F3; }
  .btn-green { background: #4CAF50; }
  .btn-orange { background: #FF9800; }
  .btn-red { background: #f44336; }
  .btn-gray { background: #888; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  #timer { font-size: 48px; font-weight: bold; margin: 20px 0; text-align: center; }
  #condname { font-size: 22px; text-align: center; margin: 10px 0; color: #333; }
  .timer-running { color: #2196F3; }
  .timer-done { color: #4CAF50; }
  .timer-idle { color: #999; }
  #progress { width: 100%; height: 20px; border-radius: 10px; background: #eee; margin: 10px 0; }
  #progress-bar { height: 100%; border-radius: 10px; background: #2196F3; transition: width 0.5s; }
</style>
</head>
<body>
<h1>SOT - Sensory Organization Test</h1>

<div class="step active" id="step1">
  <b>1. TARE (plateforme VIDE, patient PAS dessus)</b><br><br>
  <button class="btn-blue" onclick="doTare()">TARE A VIDE</button>
  <span id="tare_status"></span>
</div>

<div class="step" id="step2">
  <b>2. SET CENTER (patient DEBOUT IMMOBILE au centre)</b><br><br>
  <button class="btn-blue" onclick="doCenter()" id="btn_center" disabled>SET CENTER + HOME</button>
  <span id="center_status"></span>
</div>

<div class="step" id="step3">
  <b>3. Test SOT</b><br><br>
  <div id="condname" class="timer-idle">-</div>
  <div id="timer" class="timer-idle">--</div>
  <div id="progress"><div id="progress-bar" style="width:0%"></div></div>
  <button class="btn-green" onclick="startSOT()" id="btn_start" disabled>START SOT</button>
  <button class="btn-orange" onclick="nextCond()" id="btn_next" disabled>NEXT CONDITION</button>
  <button class="btn-gray" onclick="restartCond()" id="btn_restart" disabled>RESTART</button>
  <button class="btn-red" onclick="stopSOT()" id="btn_stop" disabled>STOP</button>
  <br><br>
  <button class="btn-blue" onclick="window.open('/sot/report.pdf','_blank')" id="btn_pdf" style="display:none">DOWNLOAD PDF</button>
  <button class="btn-blue" onclick="window.open('/sot/results.json','_blank')" id="btn_json" style="display:none">DOWNLOAD JSON</button>
</div>

<script>
let sotActive = false;
let pollTimer = null;

async function doTare() {
  document.getElementById('tare_status').textContent = '...';
  await fetch('/sot/tare');
  document.getElementById('tare_status').textContent = 'OK';
  document.getElementById('step1').className = 'step done';
  document.getElementById('step2').className = 'step active';
  document.getElementById('btn_center').disabled = false;
}

async function doCenter() {
  document.getElementById('center_status').textContent = 'HOME en cours...';
  document.getElementById('btn_center').disabled = true;
  const r = await fetch('/sot/center');
  const t = await r.text();
  if (t.includes('ERROR')) {
    document.getElementById('center_status').textContent = 'ERREUR: pas de charge detectee';
    document.getElementById('btn_center').disabled = false;
    return;
  }
  document.getElementById('center_status').textContent = 'OK';
  document.getElementById('step2').className = 'step done';
  document.getElementById('step3').className = 'step active';
  document.getElementById('btn_start').disabled = false;
}

async function startSOT() {
  await fetch('/sot/start/1');
  sotActive = true;
  document.getElementById('btn_start').disabled = true;
  document.getElementById('btn_next').disabled = false;
  document.getElementById('btn_restart').disabled = false;
  document.getElementById('btn_stop').disabled = false;
  startPolling();
}

async function nextCond() {
  const r = await fetch('/sot/next');
  const t = await r.text();
  if (t.includes('FINISHED')) {
    sotActive = false;
    stopPolling();
    document.getElementById('timer').textContent = 'SOT TERMINE - Analyse en cours...';
    document.getElementById('timer').className = 'timer-done';
    document.getElementById('condname').textContent = 'Rapport genere';
    document.getElementById('btn_next').disabled = true;
    document.getElementById('btn_restart').disabled = true;
    document.getElementById('btn_stop').disabled = true;
    document.getElementById('progress-bar').style.width = '100%';
    document.getElementById('progress-bar').style.background = '#4CAF50';
    document.getElementById('btn_pdf').style.display = 'inline-block';
    document.getElementById('btn_json').style.display = 'inline-block';
    document.getElementById('timer').textContent = 'SOT TERMINE';
  }
}

async function restartCond() {
  await fetch('/sot/restart');
}

async function stopSOT() {
  await fetch('/sot/stop');
  sotActive = false;
  stopPolling();
  document.getElementById('timer').textContent = 'ARRETE - Analyse en cours...';
  document.getElementById('timer').className = 'timer-idle';
  document.getElementById('condname').textContent = '-';
  document.getElementById('btn_next').disabled = true;
  document.getElementById('btn_restart').disabled = true;
  document.getElementById('btn_stop').disabled = true;
  document.getElementById('btn_start').disabled = false;
  document.getElementById('progress-bar').style.width = '0%';
  document.getElementById('btn_pdf').style.display = 'inline-block';
  document.getElementById('btn_json').style.display = 'inline-block';
  document.getElementById('timer').textContent = 'ARRETE';
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(updateInfo, 500);
  updateInfo();
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function updateInfo() {
  try {
    const r = await fetch('/sot/info');
    const info = await r.json();
    const c = info.condition;
    const rem = Math.ceil(info.remaining);
    const dur = info.duration;
    const elapsed = info.elapsed;
    const pct = dur > 0 ? Math.min(100, (elapsed / dur) * 100) : 0;

    document.getElementById('condname').textContent = 'C' + c + ' : ' + info.name;
    document.getElementById('progress-bar').style.width = pct + '%';

    if (info.finished) {
      document.getElementById('timer').textContent = 'TERMINE';
      document.getElementById('timer').className = 'timer-done';
      document.getElementById('condname').className = '';
      document.getElementById('progress-bar').style.background = '#4CAF50';
    } else {
      document.getElementById('timer').textContent = rem + 's';
      document.getElementById('timer').className = 'timer-running';
      document.getElementById('progress-bar').style.background = '#2196F3';
    }
  } catch(e) {}
}
</script>
</body>
</html>
"""

# ==========================================================
# ANALYSE SOT (mÃ©triques + PDF)
# ==========================================================
CHI2_95_2DOF = 5.991

LOGO_PATH = "logo.png"
CABINET_FOOTER = (
    "Cabinet de RÃ©Ã©ducation Vestibulaire â€“ 276 avenue de l'Europe â€“ 44240 SucÃ© sur Erdre\n"
    "Tel : 07.55.55.70.96 - Mail : sylvain.fremon@masseur-kinesitherapeute.mssante.fr"
)

SOT_PROTOCOL = {
    1: {"name": "EO STABLE",     "analysis_start": 0,  "analysis_end": 20},
    2: {"name": "EC STABLE",     "analysis_start": 0,  "analysis_end": 20},
    3: {"name": "EO OPTO",       "analysis_start": 15, "analysis_end": 35},
    4: {"name": "EO INSTABLE",   "analysis_start": 0,  "analysis_end": 20},
    5: {"name": "EC INSTABLE",   "analysis_start": 0,  "analysis_end": 20},
    6: {"name": "OPTO INSTABLE", "analysis_start": 15, "analysis_end": 35},
}

STAB_LIMIT_CM = 8.0

def _clamp_val(x, a, b):
    return a if x < a else (b if x > b else x)

def analyze_one_condition(dfc, cond_id):
    cfg = SOT_PROTOCOL[cond_id]
    if dfc.empty:
        return None, None
    dfc = dfc.sort_values("time").reset_index(drop=True)
    t0 = float(dfc["time"].iloc[0])
    t_rel = dfc["time"] - t0
    start = float(cfg["analysis_start"])
    end = float(cfg["analysis_end"])
    win = dfc[(t_rel >= start) & (t_rel <= end)].copy()
    if len(win) < 10:
        return {"condition": cond_id, "name": cfg["name"], "analysis_window_s": [start, end], "n": int(len(win)), "error": "Not enough samples"}, win
    x = win["cop_x_cm"].astype(float).to_numpy()
    y = win["cop_y_cm"].astype(float).to_numpy()
    duration_s = float(win["time"].iloc[-1] - win["time"].iloc[0])
    if duration_s <= 0:
        duration_s = float((len(win) - 1) * 0.02)
    dx = np.diff(x); dy = np.diff(y)
    seg = np.sqrt(dx*dx + dy*dy)
    path_length_cm = float(np.sum(seg))
    mean_speed_cm_s = float(path_length_cm / duration_s) if duration_s > 0 else float("nan")
    rms_r = float(np.sqrt(np.mean(x*x + y*y)))
    cov = np.cov(np.vstack([x, y]))
    eig = np.linalg.eigvalsh(cov)
    eig = np.maximum(eig, 0.0)
    ellipse95_area = float(math.pi * CHI2_95_2DOF * math.sqrt(eig[0] * eig[1]))
    stability_pct = 100.0 * (1.0 - (rms_r / STAB_LIMIT_CM))
    stability_pct = float(_clamp_val(stability_pct, 0.0, 100.0))
    return {
        "condition": cond_id, "name": cfg["name"], "analysis_window_s": [start, end],
        "n": int(len(win)), "duration_s": duration_s, "rms_r_cm": rms_r,
        "path_length_cm": path_length_cm, "mean_speed_cm_s": mean_speed_cm_s,
        "ellipse95_area_cm2": ellipse95_area, "stability_pct": stability_pct,
    }, win

def plot_statok_png(win, res, out_dir):
    c = res["condition"]
    x = win["cop_x_cm"].astype(float).to_numpy()
    y = win["cop_y_cm"].astype(float).to_numpy()
    plt.figure(figsize=(4, 4))
    plt.plot(x, y, linewidth=1)
    plt.axhline(0); plt.axvline(0)
    plt.gca().set_aspect("equal", adjustable="box")
    plt.title(f"C{c} {res['name']}")
    plt.xlabel("X (cm)"); plt.ylabel("Y (cm)")
    mx = np.nanmax(np.abs(x)) if len(x) else 1
    my = np.nanmax(np.abs(y)) if len(y) else 1
    m = max(mx, my, 0.5) * 1.2
    plt.xlim(-m, m); plt.ylim(-m, m)
    path = os.path.join(out_dir, f"cond{c}_statok.png")
    plt.tight_layout(); plt.savefig(path, dpi=150); plt.close()
    return path

def compute_sensory_ratios(results_by_c):
    def get_stab(c):
        r = results_by_c.get(c)
        if not r or "error" in r:
            return None
        return r.get("stability_pct")
    c1 = get_stab(1); c2 = get_stab(2); c3 = get_stab(3)
    c4 = get_stab(4); c5 = get_stab(5); c6 = get_stab(6)
    out = {}
    if c1 is not None and c1 > 1e-6:
        if c2 is not None: out["SOMES"] = float(_clamp_val(c2 / c1, 0, 2))
        if c4 is not None: out["VISIO"] = float(_clamp_val(c4 / c1, 0, 2))
        if c5 is not None: out["VEST"] = float(_clamp_val(c5 / c1, 0, 2))
    denom = (c2 or 0) + (c5 or 0)
    if denom > 1e-6:
        num = (c3 or 0) + (c6 or 0)
        out["PREF_VIS"] = float(_clamp_val(num / denom, 0, 3))
    return out

def plot_ratio_bars(ratios, out_dir):
    paths = {}
    for key in ["SOMES", "VISIO", "VEST"]:
        if key not in ratios:
            continue
        val = float(ratios[key])
        plt.figure(figsize=(4.2, 1.1))
        plt.xlim(0, max(1.0, min(2.0, val * 1.1)))
        plt.yticks([])
        plt.xlabel("Ratio")
        plt.title(key, fontsize=12, fontweight="bold")
        plt.barh([0], [val], height=0.5)
        plt.axvline(1.0, linewidth=1)
        plt.tight_layout()
        p = os.path.join(out_dir, f"ratio_{key}.png")
        plt.savefig(p, dpi=150); plt.close()
        paths[key] = p
    return paths

def build_multitest_like_pdf(pdf_path, source_csv, results_by_c, img_paths):
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(pdf_path, pagesize=A4, leftMargin=1.2*RL_CM, rightMargin=1.2*RL_CM, topMargin=1.0*RL_CM, bottomMargin=1.0*RL_CM)
    story = []

    # Logo + coordonnÃ©es cabinet (centrÃ©)
    from reportlab.lib.enums import TA_CENTER
    style_centered = styles["Normal"].clone("centered")
    style_centered.alignment = TA_CENTER

    if os.path.isfile(LOGO_PATH):
        story.append(RLImage(LOGO_PATH, width=3.0*RL_CM, height=3.0*RL_CM))
    story.append(Paragraph(CABINET_FOOTER.replace("\n", "<br/>"), style_centered))
    story.append(Spacer(1, 0.2*RL_CM))

    # Titre
    story.append(Paragraph("RAPPORT â€” BILAN SOT", styles["Title"]))
    story.append(Paragraph(f"Fichier : {os.path.basename(source_csv)}", styles["Normal"]))
    story.append(Paragraph(f"GÃ©nÃ©rÃ© : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Normal"]))
    story.append(Spacer(1, 0.3*RL_CM))

    # Tableau rÃ©sultats
    header = ["Cond", "Nom", "FenÃªtre", "StabilitÃ© %", "Vitesse (cm/s)", "Surface 95% (cmÂ²)"]
    rows = [header]
    for c in range(1, 7):
        if c not in results_by_c: continue
        r = results_by_c[c]
        if "error" in r:
            rows.append([f"C{c}", r.get("name", ""), "-", "-", "-", "-"])
        else:
            rows.append([f"C{c}", r["name"], f"{r['analysis_window_s'][0]}â€“{r['analysis_window_s'][1]}s",
                         f"{r['stability_pct']:.1f}", f"{r['mean_speed_cm_s']:.3f}", f"{r['ellipse95_area_cm2']:.3f}"])
    tbl = Table(rows, colWidths=[1.0*RL_CM, 4.2*RL_CM, 2.2*RL_CM, 2.2*RL_CM, 3.0*RL_CM, 3.0*RL_CM])
    tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("GRID",(0,0),(-1,-1),0.5,colors.grey),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("ALIGN",(3,1),(-1,-1),"CENTER")]))
    story.append(tbl); story.append(Spacer(1, 0.35*RL_CM))

    # SynthÃ¨se sensorielle
    ratios = compute_sensory_ratios(results_by_c)
    if ratios:
        story.append(Paragraph("SynthÃ¨se sensorielle (ratios)", styles["Heading2"]))
        rr = [["SomesthÃ©sie", "Vision", "Vestibule", "PrÃ©f. visuelle"],
              [f"{ratios.get('SOMES',0):.2f}" if 'SOMES' in ratios else "-",
               f"{ratios.get('VISIO',0):.2f}" if 'VISIO' in ratios else "-",
               f"{ratios.get('VEST',0):.2f}" if 'VEST' in ratios else "-",
               f"{ratios.get('PREF_VIS',0):.2f}" if 'PREF_VIS' in ratios else "-"]]
        t2 = Table(rr, colWidths=[4.0*RL_CM]*4)
        t2.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.whitesmoke),("GRID",(0,0),(-1,-1),0.5,colors.grey),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("ALIGN",(0,1),(-1,1),"CENTER")]))
        story.append(t2); story.append(Spacer(1, 0.35*RL_CM))

        # Diagrammes SOMES / VISIO / VEST
        ratio_img_paths = plot_ratio_bars(ratios, os.path.dirname(pdf_path))
        if ratio_img_paths:
            row = []
            for key in ["SOMES", "VISIO", "VEST"]:
                if key in ratio_img_paths:
                    row.append(RLImage(ratio_img_paths[key], width=5.7*RL_CM, height=2.0*RL_CM))
                else:
                    row.append(Spacer(1, 2.0*RL_CM))
            t3 = Table([row], colWidths=[6.0*RL_CM, 6.0*RL_CM, 6.0*RL_CM])
            story.append(t3); story.append(Spacer(1, 0.3*RL_CM))

    # StatokinÃ©sigrammes en grille 2x3
    story.append(Paragraph("StatokinÃ©sigrammes (COP X vs COP Y)", styles["Heading2"]))
    story.append(Spacer(1, 0.15*RL_CM))
    grid = []; row = []
    for c in range(1, 7):
        if c in img_paths:
            row.append(RLImage(img_paths[c], width=8.5*RL_CM, height=8.5*RL_CM))
            if len(row) == 2: grid.append(row); row = []
    if row: row.append(Spacer(1, 8.5*RL_CM)); grid.append(row)
    if grid:
        g = Table(grid, colWidths=[9.0*RL_CM, 9.0*RL_CM])
        g.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
        story.append(g)
    doc.build(story)

def analyze_sot_csv(csv_path):
    df = pd.read_csv(csv_path)
    required = {"time", "condition", "cop_x_cm", "cop_y_cm"}
    if not required.issubset(set(df.columns)):
        raise RuntimeError("CSV colonnes manquantes")
    base = os.path.splitext(os.path.basename(csv_path))[0]
    out_dir = os.path.join(os.path.dirname(csv_path), base + "_results")
    os.makedirs(out_dir, exist_ok=True)
    results_by_c = {}; img_paths = {}
    for c in range(1, 7):
        dfc = df[df["condition"] == c]
        if dfc.empty: continue
        res, win = analyze_one_condition(dfc, c)
        if res is None: continue
        results_by_c[c] = res
        if win is not None and len(win) >= 10 and "error" not in res:
            img_paths[c] = plot_statok_png(win, res, out_dir)
    json_path = os.path.join(out_dir, "results.json")
    payload = {"source_csv": os.path.basename(csv_path), "generated_at": datetime.now().isoformat(),
               "protocol": SOT_PROTOCOL, "results": [results_by_c[k] for k in sorted(results_by_c.keys())]}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    pdf_path = os.path.join(out_dir, "report.pdf")
    build_multitest_like_pdf(pdf_path, csv_path, results_by_c, img_paths)
    return out_dir, json_path, pdf_path

def finalize_sot_and_analyze():
    global latest_results_dir, latest_pdf_path, latest_json_path
    try: stop_log()
    except: pass
    set_hdmi(mode="off")
    if not current_log_path or not os.path.isfile(current_log_path):
        print("[ANALYZE] no log file"); return False
    try:
        latest_results_dir, latest_json_path, latest_pdf_path = analyze_sot_csv(current_log_path)
        print(f"[ANALYZE] OK: {latest_pdf_path}"); return True
    except Exception as e:
        print(f"[ANALYZE] ERROR: {e}"); return False

@app.route("/sot/report.pdf")
def sot_report_pdf():
    if latest_pdf_path and os.path.isfile(latest_pdf_path):
        return send_file(latest_pdf_path, as_attachment=True)
    return "NO PDF\n", 404

@app.route("/sot/results.json")
def sot_results_json():
    if latest_json_path and os.path.isfile(latest_json_path):
        return send_file(latest_json_path, as_attachment=True)
    return "NO JSON\n", 404

@app.route("/system/shutdown")
def system_shutdown():
    try: exercise_stop()
    except: pass
    try: exercise2_stop()
    except: pass
    try: exercise3_stop()
    except: pass
    try: exercise4_stop()
    except: pass
    try: exercise5_stop()
    except: pass
    try: exercise6_stop()
    except: pass
    try: exercise7_stop()
    except: pass
    try: exercise8_stop()
    except: pass
    try: exercise9_stop()
    except: pass
    try: exercise10_stop()
    except: pass
    try: exercise11_stop()
    except: pass
    try: exercise12_stop()
    except: pass
    try: stop_condition()
    except: pass
    try: stop_log()
    except: pass
    try: kill_chromium()
    except: pass
    def delayed_shutdown():
        time.sleep(1.0)
        try: subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        except Exception as e: print("[SHUTDOWN ERROR]", e)
    threading.Thread(target=delayed_shutdown, daemon=True).start()
    return "SHUTDOWN\n"

@app.route("/sot/info")
def sot_info():
    cond = SOT_CONDITIONS.get(sot_condition, {})
    duration = cond.get("duration", 0)
    elapsed = time.time() - sot_start_time if sot_start_time > 0 else 0
    remaining = max(0, duration - elapsed)
    return Response(json.dumps({
        "condition": sot_condition,
        "name": cond.get("name", ""),
        "duration": duration,
        "elapsed": round(elapsed, 1),
        "remaining": round(remaining, 1),
        "finished": remaining <= 0 and sot_start_time > 0,
        "logging": logging_active,
    }), mimetype="application/json")

# ==========================================================
# EXERCICE 2 : SINUS
# ==========================================================
def exercise2_amp_value(level):
    if level == "low": return 0.12
    if level == "high": return 0.32
    return 0.22

def exercise2_freq_value(level):
    if level == "low": return 0.08
    if level == "high": return 1.45
    return 0.85

def exercise_slew_per_s(level):
    if level == "low": return 2.0
    if level == "high": return 8.5
    return 4.0
EX2_MARGIN_TICKS = 50
EX2_SLOW_ZONE_TICKS = 220

def ex2_apply_soft_limit(cmd):
    global esp_pos, esp_pos_min_safe, esp_pos_max_safe
    if esp_pos < 0 or esp_pos_min_safe < 0 or esp_pos_max_safe < 0:
        return cmd
    dist_to_min = esp_pos - esp_pos_min_safe
    dist_to_max = esp_pos_max_safe - esp_pos
    if cmd < 0:
        if dist_to_min < EX2_SLOW_ZONE_TICKS:
            ratio = dist_to_min / float(EX2_SLOW_ZONE_TICKS)
            ratio = max(0.0, min(1.0, ratio))
            ratio = ratio  # freinage lineaire
            return cmd * ratio
    if cmd > 0:
        if dist_to_max < EX2_SLOW_ZONE_TICKS:
            ratio = dist_to_max / float(EX2_SLOW_ZONE_TICKS)
            ratio = max(0.0, min(1.0, ratio))
            ratio = ratio
            return cmd * ratio
    return cmd

def exercise2_loop():
    global exercise2_running
    t0 = time.time()
    last_t = time.time()
    cmd_now = 0.0
    while exercise2_running:
        now = time.time()
        dt = now - last_t
        if dt <= 0.0: dt = 0.02
        last_t = now
        amp = exercise2_amp_value(exercise2_mode["amplitude"])
        freq = exercise2_freq_value(exercise2_mode["speed"])
        t = now - t0
        cmd_target = amp * math.sin(2.0 * math.pi * freq * t)
        cmd_target = ex2_apply_soft_limit(cmd_target)
        max_step = exercise_slew_per_s(exercise2_mode["speed"]) * dt
        delta = cmd_target - cmd_now
        if delta > max_step: delta = max_step
        elif delta < -max_step: delta = -max_step
        cmd_now += delta
        cmd_now = max(-CMD_MAX, min(CMD_MAX, cmd_now))
        if uart:
            try: uart.write(f"COP:Y:{cmd_now:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise2_start():
    global exercise2_running, send_to_esp, control_source
    exercise2_stop()
    control_source = "exercise2"
    send_to_esp = True
    esp_send("ARM:1")
    time.sleep(0.1)
    esp_send("AUTO:1")
    ensure_chromium()
    if exercise2_mode["screen"] == "opto":
        start_opto(direction=exercise2_mode["direction"], speed=exercise2_mode["opto_speed"])
    else:
        start_black_screen()
    exercise2_running = True
    threading.Thread(target=exercise2_loop, daemon=True).start()

def exercise2_stop():
    global exercise2_running, send_to_esp, control_source
    exercise2_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 3 : PETIT -> GRAND -> PETIT
# ==========================================================
def exercise3_envelope(x):
    if x < 0.5:
        return 0.25 + 1.5 * x
    else:
        return 1.75 - 1.5 * x

def exercise3_loop():
    global exercise3_running
    t0 = time.time()
    last_t = time.time()
    cmd_now = 0.0
    total_duration = float(exercise3_mode["duration"])
    while exercise3_running:
        now = time.time()
        dt = now - last_t
        if dt <= 0.0: dt = 0.02
        last_t = now
        elapsed = now - t0
        if elapsed >= total_duration:
            break
        phase = elapsed / total_duration
        env = exercise3_envelope(phase)
        amp_base = exercise2_amp_value(exercise3_mode["amplitude"])
        freq = exercise2_freq_value(exercise3_mode["speed"])
        amp = amp_base * env
        cmd_target = amp * math.sin(2.0 * math.pi * freq * elapsed)
        cmd_target = ex2_apply_soft_limit(cmd_target)
        max_step = exercise_slew_per_s(exercise3_mode["speed"]) * dt
        delta = cmd_target - cmd_now
        if delta > max_step: delta = max_step
        elif delta < -max_step: delta = -max_step
        cmd_now += delta
        cmd_now = max(-CMD_MAX, min(CMD_MAX, cmd_now))
        if uart:
            try: uart.write(f"COP:Y:{cmd_now:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)
    exercise3_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

def exercise3_start():
    global exercise3_running, send_to_esp, control_source
    exercise3_stop()
    control_source = "exercise3"
    send_to_esp = True
    esp_send("ARM:1")
    time.sleep(0.1)
    esp_send("AUTO:1")
    ensure_chromium()
    if exercise3_mode["screen"] == "opto":
        start_opto(direction=exercise3_mode["direction"], speed=exercise3_mode["opto_speed"])
    else:
        start_black_screen()
    exercise3_running = True
    threading.Thread(target=exercise3_loop, daemon=True).start()

def exercise3_stop():
    global exercise3_running, send_to_esp, control_source
    exercise3_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 4 : IMPULSIONS ALEATOIRES
# ==========================================================
def exercise4_amp_value(level):
    if level == "low": return 0.28
    if level == "high": return 0.55
    return 0.42

def exercise4_freq_value(level):
    if level == "low": return 2.2
    if level == "high": return 5.5
    return 3.8

def exercise4_pulse_shape(x):
    if x <= 0.0 or x >= 1.0: return 0.0
    if x < 0.12: return x / 0.12
    if x < 0.55: return 1.0
    if x < 0.72: return 1.0 - ((x - 0.55) / 0.17) * 0.25
    return 0.75

def exercise_pulse_duration(speed):
    if speed == "low": return 0.70
    if speed == "high": return 0.16
    return 0.30

def exercise_pulse_rest_cmd(speed, amp, sign):
    factor = 0.22
    if speed == "low":
        factor = 0.18
    elif speed == "high":
        factor = 0.26
    return sign * min(0.16, amp * factor)

def exercise4_loop():
    global exercise4_running
    rest_cmd = 0.0
    while exercise4_running:
        wait_s = random.uniform(float(exercise4_mode["gap_min"]), float(exercise4_mode["gap_max"]))
        t_wait_start = time.time()
        while exercise4_running and (time.time() - t_wait_start < wait_s):
            if uart:
                try: uart.write(f"COP:Y:{rest_cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        if not exercise4_running: break

        sign = random.choice([-1.0, 1.0])
        amp = exercise4_amp_value(exercise4_mode["amplitude"])
        pulse_ms = max(200, int(exercise4_mode["pulse_ms"]))
        pulse_duration = min(pulse_ms / 1000.0, exercise_pulse_duration(exercise4_mode["speed"]))

        t0 = time.time()
        while exercise4_running:
            elapsed = time.time() - t0
            phase = elapsed / pulse_duration
            if phase >= 1.0: break
            shape = exercise4_pulse_shape(phase)
            cmd = sign * amp * shape
            if exercise4_mode["speed"] == "high": cmd *= 1.22
            elif exercise4_mode["speed"] == "low": cmd *= 0.90
            cmd = ex2_apply_soft_limit(cmd)
            cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
            if uart:
                try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)

        rest_cmd = exercise_pulse_rest_cmd(exercise4_mode["speed"], amp, sign)

    exercise4_running = False
    esp_send("STOP")
    set_hdmi(mode="off")

def exercise4_start():
    global exercise4_running, send_to_esp, control_source
    exercise4_stop()
    control_source = "exercise4"
    send_to_esp = True
    esp_send("ARM:1")
    time.sleep(0.1)
    esp_send("AUTO:1")
    ensure_chromium()
    if exercise4_mode["screen"] == "opto":
        start_opto(direction=exercise4_mode["direction"], speed=exercise4_mode["opto_speed"])
    else:
        start_black_screen()
    exercise4_running = True
    threading.Thread(target=exercise4_loop, daemon=True).start()

def exercise4_stop():
    global exercise4_running, send_to_esp, control_source
    exercise4_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 5 : POINTS VOR
# ==========================================================
exercise5_running = False
exercise5_mode = {
    "platform": "fixed",
    "vor_mode": "lr",
    "vor_interval": 2,
    "amplitude": "medium",
    "speed": "medium",
}

def exercise5_next_pair(vmode):
    if vmode == "random":
        return random.choice(["lr", "ud", "diag1", "diag2"])
    return vmode

def exercise5_vor_loop():
    global exercise5_running
    last_change = 0.0
    current_pair = exercise5_next_pair(exercise5_mode["vor_mode"])
    set_hdmi(mode="vor", vor_pair=current_pair)
    while exercise5_running:
        now = time.time()
        if (now - last_change) >= float(exercise5_mode["vor_interval"]):
            current_pair = exercise5_next_pair(exercise5_mode["vor_mode"])
            set_hdmi(mode="vor", vor_pair=current_pair)
            last_change = now
        time.sleep(0.05)

def exercise5_loop_sinus():
    global exercise5_running
    t0 = time.time()
    while exercise5_running:
        amp = exercise2_amp_value(exercise5_mode["amplitude"])
        freq = exercise2_freq_value(exercise5_mode["speed"])
        t = time.time() - t0
        cmd = amp * math.sin(2.0 * math.pi * freq * t)
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise5_loop_ramp():
    global exercise5_running
    t0 = time.time()
    cycle_s = 30.0
    while exercise5_running:
        elapsed = time.time() - t0
        phase_env = (elapsed % cycle_s) / cycle_s
        env = exercise3_envelope(phase_env)
        amp_base = exercise2_amp_value(exercise5_mode["amplitude"])
        freq = exercise2_freq_value(exercise5_mode["speed"])
        amp = amp_base * env
        cmd = amp * math.sin(2.0 * math.pi * freq * elapsed)
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise5_loop_impulses():
    global exercise5_running
    rest_cmd = 0.0
    while exercise5_running:
        wait_s = random.uniform(1.0, 3.0)
        t_wait = time.time()
        while exercise5_running and (time.time() - t_wait < wait_s):
            if uart:
                try: uart.write(f"COP:Y:{rest_cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        if not exercise5_running: break
        sign = random.choice([-1.0, 1.0])
        amp = exercise4_amp_value(exercise5_mode["amplitude"])
        pulse_duration = exercise_pulse_duration(exercise5_mode["speed"])
        t0 = time.time()
        while exercise5_running:
            phase = (time.time() - t0) / pulse_duration
            if phase >= 1.0: break
            shape = exercise4_pulse_shape(phase)
            cmd = sign * amp * shape
            if exercise5_mode["speed"] == "high": cmd *= 1.18
            elif exercise5_mode["speed"] == "low": cmd *= 0.90
            cmd = ex2_apply_soft_limit(cmd)
            cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
            if uart:
                try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        rest_cmd = exercise_pulse_rest_cmd(exercise5_mode["speed"], amp, sign)

def exercise5_start():
    global exercise5_running, send_to_esp, control_source
    exercise5_stop()
    exercise5_running = True
    ensure_chromium()
    threading.Thread(target=exercise5_vor_loop, daemon=True).start()
    platform = exercise5_mode["platform"]
    if platform == "fixed":
        send_to_esp = False
        control_source = "cop"
        esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True
        control_source = "cop"
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True
        control_source = "exercise5"
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("AUTO:1")
        if platform == "sinus":
            threading.Thread(target=exercise5_loop_sinus, daemon=True).start()
        elif platform == "ramp":
            threading.Thread(target=exercise5_loop_ramp, daemon=True).start()
        elif platform == "impulses":
            threading.Thread(target=exercise5_loop_impulses, daemon=True).start()

def exercise5_stop():
    global exercise5_running, send_to_esp, control_source
    exercise5_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 6 : POINT MOBILE
# ==========================================================
exercise6_running = False
exercise6_mode = {
    "platform": "fixed",
    "point_mode": "lr",
    "point_speed": "medium",
    "amplitude": "medium",
    "speed": "medium"
}

def exercise6_set_screen():
    set_hdmi(mode="point", point_mode=exercise6_mode["point_mode"], point_speed=exercise6_mode["point_speed"])

def exercise6_loop_sinus():
    global exercise6_running
    t0 = time.time()
    while exercise6_running:
        amp = exercise2_amp_value(exercise6_mode["amplitude"])
        freq = exercise2_freq_value(exercise6_mode["speed"])
        t = time.time() - t0
        cmd = amp * math.sin(2.0 * math.pi * freq * t)
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise6_loop_ramp():
    global exercise6_running
    t0 = time.time()
    cycle_s = 30.0
    while exercise6_running:
        elapsed = time.time() - t0
        phase_env = (elapsed % cycle_s) / cycle_s
        env = exercise3_envelope(phase_env)
        amp_base = exercise2_amp_value(exercise6_mode["amplitude"])
        freq = exercise2_freq_value(exercise6_mode["speed"])
        amp = amp_base * env
        cmd = amp * math.sin(2.0 * math.pi * freq * elapsed)
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise6_loop_impulses():
    global exercise6_running
    rest_cmd = 0.0
    while exercise6_running:
        wait_s = random.uniform(1.0, 3.0)
        t_wait = time.time()
        while exercise6_running and (time.time() - t_wait < wait_s):
            if uart:
                try: uart.write(f"COP:Y:{rest_cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        if not exercise6_running: break
        sign = random.choice([-1.0, 1.0])
        amp = exercise4_amp_value(exercise6_mode["amplitude"])
        pulse_duration = exercise_pulse_duration(exercise6_mode["speed"])
        t0 = time.time()
        while exercise6_running:
            phase = (time.time() - t0) / pulse_duration
            if phase >= 1.0: break
            shape = exercise4_pulse_shape(phase)
            cmd = sign * amp * shape
            if exercise6_mode["speed"] == "high": cmd *= 1.18
            elif exercise6_mode["speed"] == "low": cmd *= 0.90
            cmd = ex2_apply_soft_limit(cmd)
            cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
            if uart:
                try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        rest_cmd = exercise_pulse_rest_cmd(exercise6_mode["speed"], amp, sign)

def exercise6_start():
    global exercise6_running, send_to_esp, control_source
    exercise6_stop()
    exercise6_running = True
    ensure_chromium()
    exercise6_set_screen()
    platform = exercise6_mode["platform"]
    if platform == "fixed":
        send_to_esp = False; control_source = "cop"; esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True; control_source = "cop"
        esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True; control_source = "exercise6"
        esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
        if platform == "sinus":
            threading.Thread(target=exercise6_loop_sinus, daemon=True).start()
        elif platform == "ramp":
            threading.Thread(target=exercise6_loop_ramp, daemon=True).start()
        elif platform == "impulses":
            threading.Thread(target=exercise6_loop_impulses, daemon=True).start()

def exercise6_stop():
    global exercise6_running, send_to_esp, control_source
    exercise6_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 7 : CITATIONS (DOUBLE TACHE)
# ==========================================================
QUOTES = [
"On peut rire de tout, mais pas avec tout le monde. - Desproges",
"L'ennemi est bete : il croit que c'est nous l'ennemi alors que c'est lui. - Desproges",
"L'humour est la politesse du desespoir. - Desproges",
"Quand on est plus de quatre on est une bande de cons. - Coluche",
"Dieu a dit : il faut partager. Les riches auront la nourriture, les pauvres de l'appetit. - Coluche",
"La dictature c'est ferme ta gueule, la democratie c'est cause toujours. - Coluche",
"Je suis capable du meilleur comme du pire, mais dans le pire c'est moi le meilleur. - Coluche",
"L'intelligence c'est comme un parachute, quand on n'en a pas on s'ecrase. - Desproges",
"Un intellectuel assis va moins loin qu'un con qui marche. - Audiard",
"La seule certitude que j'ai, c'est d'etre dans le doute. - Devos",
"Je ne suis pas contre le progres, mais je prefere quand ca marche. - Devos",
"La logique mene a tout, a condition d'en sortir. - Devos",
"Un pessimiste est un optimiste qui a de l'experience. - Desproges",
"La culture c'est comme la confiture, moins on en a plus on l'etale. - Desproges",
"Il faut rire avant d'etre heureux, de peur de mourir sans avoir ri. - La Bruyere",
"Les conneries c'est comme les impots, on finit toujours par les payer. - Coluche",
"La vie est trop importante pour etre prise au serieux. - Wilde",
"Il vaut mieux etre riche et en bonne sante que pauvre et malade. - Coluche",
"Je parle pour ne rien dire mais quand je n'ai rien a dire je veux qu'on le sache. - Devos",
"La betise insiste toujours. - Camus",
"Les previsions sont difficiles surtout lorsqu'elles concernent l'avenir. - Niels Bohr",
"Il n'y a pas de probleme dont une absence de solution ne finisse par venir a bout. - Devos",
"Je ne suis pas superstitieux, ca porte malheur. - Coluche",
"La connaissance s'acquiert par l'experience, tout le reste n'est que de l'information. - Einstein",
"La difference entre le genie et la betise, c'est que le genie a des limites. - Einstein",
"Le hasard c'est Dieu qui se promene incognito. - Einstein",
"Rien ne sert de courir si l'on ne sait pas ou aller. - Devos",
"La gravite n'est pas responsable des gens qui tombent amoureux. - Einstein",
"Quand on pense qu'il suffirait que les gens n'achetent pas pour que ca ne se vende plus. - Coluche",
"La bureaucratie c'est l'art de rendre le possible impossible. - Devos",
"Si les cons volaient il ferait nuit. - Audiard",
"Je prefere les questions aux reponses. - Devos",
"Le doute est un hommage rendu a l'espoir. - Desproges",
"Le bon sens est la chose du monde la mieux partagee. - Descartes",
"Le rire est le propre de l'homme. - Rabelais",
"Tout ce qui est exagere est insignifiant. - Talleyrand",
"La patience est l'art d'esperer. - Vauvenargues",
"On n'est jamais si bien servi que par soi-meme. - Proverbe",
"La vie est courte, souriez pendant que vous avez encore des dents.",
"Si ca ne marche pas, eteignez et rallumez. - sagesse universelle",
"On peut rire de tout, c'est meme a ca qu'on reconnait l'intelligence. - Desproges",
"Le rire est une chose serieuse avec laquelle il ne faut pas plaisanter. - Devos",
"Le cerveau commence a fonctionner des la naissance et ne s'arrete que quand on prend la parole. - Coluche",
"La verite n'est jamais amusante sinon tout le monde la dirait. - Coluche",
"Il vaut mieux se taire et passer pour un con que parler et ne laisser aucun doute. - Desproges",
"Il faut viser la lune car meme en cas d'echec on atterrit dans les etoiles. - Wilde",
"Le rire est une distance entre deux personnes. - Victor Borge",
"Les adultes savent que les heros legendaires ne sont que des legendes. - Desproges",
"On ne sait jamais a quel saint se vouer, surtout quand on est athee. - Devos",
"La vie est courte mais l'ennui l'allonge. - Renard"
]

def random_quote():
    return random.choice(QUOTES)

exercise7_running = False
exercise7_mode = {
    "platform": "fixed",
    "interval": 5,
    "amplitude": "medium",
    "speed": "medium"
}

def exercise7_quote_loop():
    global exercise7_running
    while exercise7_running:
        q = random_quote()
        set_hdmi(mode="quote", quote=q)
        time.sleep(float(exercise7_mode["interval"]))

def exercise7_loop_sinus():
    global exercise7_running
    t0 = time.time()
    while exercise7_running:
        amp = exercise2_amp_value(exercise7_mode["amplitude"])
        freq = exercise2_freq_value(exercise7_mode["speed"])
        t = time.time() - t0
        cmd = amp * math.sin(2.0 * math.pi * freq * t)
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise7_loop_ramp():
    global exercise7_running
    t0 = time.time()
    cycle_s = 30.0
    while exercise7_running:
        elapsed = time.time() - t0
        phase_env = (elapsed % cycle_s) / cycle_s
        env = exercise3_envelope(phase_env)
        amp = exercise2_amp_value(exercise7_mode["amplitude"]) * env
        freq = exercise2_freq_value(exercise7_mode["speed"])
        cmd = amp * math.sin(2.0 * math.pi * freq * elapsed)
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise7_loop_impulses():
    global exercise7_running
    rest_cmd = 0.0
    while exercise7_running:
        wait_s = random.uniform(1.0, 3.0)
        t_wait = time.time()
        while exercise7_running and (time.time() - t_wait < wait_s):
            if uart:
                try: uart.write(f"COP:Y:{rest_cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        if not exercise7_running: break
        sign = random.choice([-1.0, 1.0])
        amp = exercise4_amp_value(exercise7_mode["amplitude"])
        pd = exercise_pulse_duration(exercise7_mode["speed"])
        t0 = time.time()
        while exercise7_running:
            phase = (time.time() - t0) / pd
            if phase >= 1.0: break
            cmd = sign * amp * exercise4_pulse_shape(phase)
            if exercise7_mode["speed"] == "high": cmd *= 1.18
            elif exercise7_mode["speed"] == "low": cmd *= 0.90
            cmd = ex2_apply_soft_limit(cmd)
            cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
            if uart:
                try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        rest_cmd = exercise_pulse_rest_cmd(exercise7_mode["speed"], amp, sign)

def exercise7_start():
    global exercise7_running, send_to_esp, control_source
    exercise7_stop()
    exercise7_running = True
    ensure_chromium()
    threading.Thread(target=exercise7_quote_loop, daemon=True).start()
    platform = exercise7_mode["platform"]
    if platform == "fixed":
        send_to_esp = False; control_source = "cop"; esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True; control_source = "cop"
        esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True; control_source = "exercise7"
        esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
        if platform == "sinus":
            threading.Thread(target=exercise7_loop_sinus, daemon=True).start()
        elif platform == "ramp":
            threading.Thread(target=exercise7_loop_ramp, daemon=True).start()
        elif platform == "impulses":
            threading.Thread(target=exercise7_loop_impulses, daemon=True).start()

def exercise7_stop():
    global exercise7_running, send_to_esp, control_source
    exercise7_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 8 : CIBLE COP
# ==========================================================
exercise8_running = False
exercise8_mode = {
    "platform": "fixed",
    "target": "front",
    "target_mode": "single",
    "difficulty": "medium",
    "amplitude": "medium",
    "speed": "medium"
}
exercise8_score = {
    "hold_time": 0.0,
    "goal_s": 5.0,
    "validated_count": 0,
    "last_time": 0.0,
    "show_badge_until": 0.0
}

def exercise8_target_xy(name):
    if name == "front": return (0.0, 0.55)
    if name == "back": return (0.0, -0.55)
    if name == "left": return (-0.55, 0.0)
    if name == "right": return (0.55, 0.0)
    return (0.0, 0.0)

def exercise8_target_radius(level):
    if level == "low": return 0.24
    if level == "high": return 0.12
    return 0.18

def exercise8_loop():
    global exercise8_running, exercise8_score
    tx, ty = exercise8_target_xy(exercise8_mode["target"])
    tr = exercise8_target_radius(exercise8_mode["difficulty"])
    exercise8_score = {"hold_time": 0.0, "goal_s": 5.0, "validated_count": 0, "last_time": time.time(), "show_badge_until": 0.0}
    last_target_change = time.time()
    target_hold = random.uniform(8, 12)
    while exercise8_running:
        now = time.time()
        dt = now - exercise8_score["last_time"]
        if dt <= 0.0: dt = 0.02
        exercise8_score["last_time"] = now
        if exercise8_mode["target_mode"] == "random":
            hold_in_progress = exercise8_score["hold_time"] > 0.0
            if (not hold_in_progress) and (now - last_target_change > target_hold):
                tx, ty = random.choice([(0.55, 0.0), (-0.55, 0.0), (0.0, 0.55), (0.0, -0.55), (0.0, 0.0)])
                tr = exercise8_target_radius(exercise8_mode["difficulty"])
                exercise8_score["hold_time"] = 0.0
                last_target_change = now
                target_hold = random.uniform(8, 12)
        cx = max(-1.0, min(1.0, cop_x_f / 4.0))
        cy = max(-1.0, min(1.0, cop_y_f / 4.0))
        dx = cx - tx
        dy = cy - ty
        dist = math.sqrt(dx*dx + dy*dy)
        inside = (dist <= tr)
        if inside:
            exercise8_score["hold_time"] += dt
        else:
            exercise8_score["hold_time"] = 0.0
        if exercise8_score["hold_time"] >= exercise8_score["goal_s"]:
            exercise8_score["validated_count"] += 1
            exercise8_score["hold_time"] = 0.0
            exercise8_score["show_badge_until"] = now + 3.0
            if exercise8_mode["target_mode"] == "random":
                tx, ty = random.choice([(0.55, 0.0), (-0.55, 0.0), (0.0, 0.55), (0.0, -0.55), (0.0, 0.0)])
                last_target_change = now
                target_hold = random.uniform(8, 12)
        show_badge = 1 if now < exercise8_score["show_badge_until"] else 0
        set_hdmi(mode="target", target_x=tx, target_y=ty, target_r=tr, cursor_x=cx, cursor_y=cy,
                 hold_time=exercise8_score["hold_time"], goal_s=exercise8_score["goal_s"],
                 score_percent=100.0 * exercise8_score["hold_time"] / max(0.01, exercise8_score["goal_s"]),
                 show_badge=show_badge)
        time.sleep(0.02)

def exercise8_loop_sinus():
    global exercise8_running
    t0 = time.time()
    while exercise8_running:
        amp = exercise2_amp_value(exercise8_mode["amplitude"])
        freq = exercise2_freq_value(exercise8_mode["speed"])
        cmd = amp * math.sin(2.0 * math.pi * freq * (time.time() - t0))
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise8_loop_ramp():
    global exercise8_running
    t0 = time.time()
    while exercise8_running:
        elapsed = time.time() - t0
        env = exercise3_envelope((elapsed % 30.0) / 30.0)
        amp = exercise2_amp_value(exercise8_mode["amplitude"]) * env
        freq = exercise2_freq_value(exercise8_mode["speed"])
        cmd = amp * math.sin(2.0 * math.pi * freq * elapsed)
        cmd = ex2_apply_soft_limit(cmd)
        cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
        if uart:
            try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise8_loop_impulses():
    global exercise8_running
    rest_cmd = 0.0
    while exercise8_running:
        wait_s = random.uniform(1.0, 3.0)
        t_wait = time.time()
        while exercise8_running and (time.time() - t_wait < wait_s):
            if uart:
                try: uart.write(f"COP:Y:{rest_cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        if not exercise8_running: break
        sign = random.choice([-1.0, 1.0])
        amp = exercise4_amp_value(exercise8_mode["amplitude"])
        pd = exercise_pulse_duration(exercise8_mode["speed"])
        t0 = time.time()
        while exercise8_running:
            phase = (time.time() - t0) / pd
            if phase >= 1.0: break
            cmd = sign * amp * exercise4_pulse_shape(phase)
            if exercise8_mode["speed"] == "high": cmd *= 1.18
            elif exercise8_mode["speed"] == "low": cmd *= 0.90
            cmd = ex2_apply_soft_limit(cmd)
            cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
            if uart:
                try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        rest_cmd = exercise_pulse_rest_cmd(exercise8_mode["speed"], amp, sign)

def exercise8_start():
    global exercise8_running, send_to_esp, control_source
    exercise8_stop()
    exercise8_running = True
    ensure_chromium()
    threading.Thread(target=exercise8_loop, daemon=True).start()
    platform = exercise8_mode["platform"]
    if platform == "fixed":
        send_to_esp = False; control_source = "cop"; esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True; control_source = "cop"
        esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True; control_source = "exercise8"
        esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
        if platform == "sinus":
            threading.Thread(target=exercise8_loop_sinus, daemon=True).start()
        elif platform == "ramp":
            threading.Thread(target=exercise8_loop_ramp, daemon=True).start()
        elif platform == "impulses":
            threading.Thread(target=exercise8_loop_impulses, daemon=True).start()

def exercise8_stop():
    global exercise8_running, send_to_esp, control_source
    exercise8_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")


# ==========================================================
# EXERCICE 9 : CIBLES SEQUENTIELLES
# ==========================================================
exercise9_running = False
exercise9_mode = {
    "platform": "fixed",
    "difficulty": "medium",
    "sequence": "cross",
    "amplitude": "medium",
    "speed": "medium"
}
exercise9_score = {
    "index": 0,
    "hold_time": 0.0,
    "goal_s": 2.0,
    "validated_count": 0,
    "laps": 0,
    "show_badge_until": 0.0,
    "last_time": 0.0
}

def cop_cursor_norm():
    return (
        max(-1.0, min(1.0, cop_x_f / 4.0)),
        max(-1.0, min(1.0, cop_y_f / 4.0)),
    )

def exercise9_sequence_points(name):
    cross = [[0.0,0.0],[0.0,0.58],[0.0,0.0],[0.58,0.0],[0.0,0.0],[0.0,-0.58],[0.0,0.0],[-0.58,0.0]]
    square = [[-0.50,0.50],[0.50,0.50],[0.50,-0.50],[-0.50,-0.50],[0.0,0.0]]
    star = [[0.0,0.62],[0.20,0.20],[0.62,0.20],[0.30,-0.10],[0.42,-0.55],[0.0,-0.28],[-0.42,-0.55],[-0.30,-0.10],[-0.62,0.20],[-0.20,0.20]]
    if name == "square": return square
    if name == "star": return star
    return cross

def exercise9_loop():
    global exercise9_running, exercise9_score
    seq = exercise9_sequence_points(exercise9_mode["sequence"])
    tr = exercise8_target_radius(exercise9_mode["difficulty"])
    exercise9_score = {"index": 0, "hold_time": 0.0, "goal_s": 2.0, "validated_count": 0, "laps": 0, "show_badge_until": 0.0, "last_time": time.time()}
    while exercise9_running:
        now = time.time()
        dt = now - exercise9_score["last_time"]
        if dt <= 0.0: dt = 0.02
        exercise9_score["last_time"] = now
        i = int(exercise9_score["index"]) % max(1, len(seq))
        tx, ty = seq[i]
        cx, cy = cop_cursor_norm()
        dist = math.sqrt((cx - tx)*(cx - tx) + (cy - ty)*(cy - ty))
        inside = (dist <= tr)
        if inside:
            exercise9_score["hold_time"] += dt
        else:
            exercise9_score["hold_time"] = 0.0
        if exercise9_score["hold_time"] >= exercise9_score["goal_s"]:
            exercise9_score["validated_count"] += 1
            exercise9_score["hold_time"] = 0.0
            exercise9_score["index"] += 1
            if exercise9_score["index"] >= len(seq):
                exercise9_score["index"] = 0
                exercise9_score["laps"] += 1
                exercise9_score["show_badge_until"] = now + 2.5
        set_hdmi(mode="sequence", title="Ex 9 - Cibles sequentielles",
                 seq_points=seq, seq_index=exercise9_score["index"],
                 cursor_x=cx, cursor_y=cy, target_x=tx, target_y=ty, target_r=tr,
                 hold_time=exercise9_score["hold_time"], goal_s=exercise9_score["goal_s"],
                 score_percent=100.0*exercise9_score["validated_count"],
                 show_badge=1 if now < exercise9_score["show_badge_until"] else 0)
        time.sleep(0.02)

def exercise9_start():
    global exercise9_running, send_to_esp, control_source
    exercise9_stop()
    exercise9_running = True
    ensure_chromium()
    threading.Thread(target=exercise9_loop, daemon=True).start()
    platform = exercise9_mode["platform"]
    if platform == "fixed":
        send_to_esp = False; control_source = "cop"; esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True; control_source = "cop"; esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True; control_source = "exercise9"; esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
        if platform == "sinus": threading.Thread(target=exercise8_loop_sinus, daemon=True).start()
        elif platform == "ramp": threading.Thread(target=exercise8_loop_ramp, daemon=True).start()
        else: threading.Thread(target=exercise8_loop_impulses, daemon=True).start()

def exercise9_stop():
    global exercise9_running, send_to_esp, control_source
    exercise9_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 10 : PARCOURS COP
# ==========================================================
exercise10_running = False
exercise10_mode = {
    "platform": "fixed",
    "difficulty": "medium",
    "path": "infinity",
    "amplitude": "medium",
    "speed": "medium"
}
exercise10_score = {
    "index": 0,
    "completed": 0,
    "show_badge_until": 0.0
}

def exercise10_path_points(kind):
    pts = []
    if kind == "circle":
        for k in range(14):
            a = (2.0*math.pi*k)/14.0
            pts.append([0.55*math.cos(a), 0.55*math.sin(a)])
    elif kind == "square":
        pts = [[-0.55,0.55],[0.55,0.55],[0.55,-0.55],[-0.55,-0.55],[-0.55,0.55],[0.0,0.0]]
    else:
        for k in range(16):
            a = (2.0*math.pi*k)/16.0
            pts.append([0.55*math.sin(a), 0.38*math.sin(2*a)])
    return pts

def exercise10_loop():
    global exercise10_running, exercise10_score
    path = exercise10_path_points(exercise10_mode["path"])
    tr = max(0.11, exercise8_target_radius(exercise10_mode["difficulty"]) * 0.95)
    exercise10_score = {"index": 0, "completed": 0, "show_badge_until": 0.0}
    while exercise10_running:
        i = int(exercise10_score["index"]) % max(1, len(path))
        tx, ty = path[i]
        cx, cy = cop_cursor_norm()
        dist = math.sqrt((cx - tx)*(cx - tx) + (cy - ty)*(cy - ty))
        if dist <= tr:
            exercise10_score["index"] += 1
            if exercise10_score["index"] >= len(path):
                exercise10_score["index"] = 0
                exercise10_score["completed"] += 1
                exercise10_score["show_badge_until"] = time.time() + 2.5
        now = time.time()
        set_hdmi(mode="path", title="Ex 10 - Parcours",
                 path_points=path, path_index=exercise10_score["index"],
                 cursor_x=cx, cursor_y=cy, target_x=tx, target_y=ty, target_r=tr,
                 score_percent=100.0 * (exercise10_score["index"] / max(1, len(path))),
                 show_badge=1 if now < exercise10_score["show_badge_until"] else 0)
        time.sleep(0.02)

def exercise10_start():
    global exercise10_running, send_to_esp, control_source
    exercise10_stop()
    exercise10_running = True
    ensure_chromium()
    threading.Thread(target=exercise10_loop, daemon=True).start()
    platform = exercise10_mode["platform"]
    if platform == "fixed":
        send_to_esp = False; control_source = "cop"; esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True; control_source = "cop"; esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True; control_source = "exercise10"; esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
        if platform == "sinus": threading.Thread(target=exercise8_loop_sinus, daemon=True).start()
        elif platform == "ramp": threading.Thread(target=exercise8_loop_ramp, daemon=True).start()
        else: threading.Thread(target=exercise8_loop_impulses, daemon=True).start()

def exercise10_stop():
    global exercise10_running, send_to_esp, control_source
    exercise10_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 11 : LABYRINTHE COP
# ==========================================================
exercise11_running = False
exercise11_mode = {
    "platform": "fixed",
    "difficulty": "medium",
    "amplitude": "medium",
    "speed": "medium"
}
exercise11_score = {
    "index": 0,
    "best": 0,
    "finished": 0,
    "show_badge_until": 0.0,
    "offtrack": 0
}

MAZE_POINTS = [
    [-0.78, 0.78], [-0.30, 0.78], [-0.30, 0.35], [0.18, 0.35], [0.18, 0.62],
    [0.70, 0.62], [0.70, 0.08], [0.30, 0.08], [0.30, -0.28], [0.72, -0.28],
    [0.72, -0.72], [0.10, -0.72], [0.10, -0.42], [-0.45, -0.42], [-0.45, -0.10],
    [-0.78, -0.10], [-0.78, 0.78]
]

def point_to_segment_distance(px, py, ax, ay, bx, by):
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    c1 = vx*wx + vy*wy
    if c1 <= 0: return math.sqrt((px-ax)*(px-ax)+(py-ay)*(py-ay))
    c2 = vx*vx + vy*vy
    if c2 <= 1e-9: return math.sqrt((px-ax)*(px-ax)+(py-ay)*(py-ay))
    t = c1 / c2
    if t >= 1: return math.sqrt((px-bx)*(px-bx)+(py-by)*(py-by))
    projx = ax + t*vx
    projy = ay + t*vy
    return math.sqrt((px-projx)*(px-projx)+(py-projy)*(py-projy))

def min_dist_to_polyline(px, py, pts):
    if len(pts) < 2: return 999.0
    d = 999.0
    for i in range(len(pts)-1):
        a = pts[i]; b = pts[i+1]
        di = point_to_segment_distance(px, py, a[0], a[1], b[0], b[1])
        if di < d: d = di
    return d

def maze_width_for_diff(level):
    if level == "low": return 0.20
    if level == "high": return 0.12
    return 0.16

def exercise11_loop():
    global exercise11_running, exercise11_score
    pts = MAZE_POINTS
    tr = 0.10
    width = maze_width_for_diff(exercise11_mode["difficulty"])
    exercise11_score = {"index": 0, "best": 0, "finished": 0, "show_badge_until": 0.0, "offtrack": 0}
    while exercise11_running:
        i = int(exercise11_score["index"])
        if i >= len(pts): i = len(pts)-1
        tx, ty = pts[i]
        cx, cy = cop_cursor_norm()
        dist = math.sqrt((cx-tx)*(cx-tx)+(cy-ty)*(cy-ty))
        if dist <= tr and exercise11_score["index"] < len(pts)-1:
            exercise11_score["index"] += 1
            if exercise11_score["index"] > exercise11_score["best"]:
                exercise11_score["best"] = exercise11_score["index"]
            if exercise11_score["index"] >= len(pts)-1:
                exercise11_score["finished"] = 1
                exercise11_score["show_badge_until"] = time.time() + 3.0
        dline = min_dist_to_polyline(cx, cy, pts)
        off = 1 if dline > width else 0
        exercise11_score["offtrack"] = off
        set_hdmi(mode="maze", title="Ex 11 - Labyrinthe",
                 maze_points=pts, maze_index=exercise11_score["index"], maze_width=width, maze_offtrack=off,
                 cursor_x=cx, cursor_y=cy, target_x=tx, target_y=ty, target_r=tr,
                 score_percent=100.0 * (exercise11_score["index"] / max(1, len(pts)-1)),
                 show_badge=1 if time.time() < exercise11_score["show_badge_until"] else 0)
        time.sleep(0.02)

def exercise11_start():
    global exercise11_running, send_to_esp, control_source
    exercise11_stop()
    exercise11_running = True
    ensure_chromium()
    threading.Thread(target=exercise11_loop, daemon=True).start()
    platform = exercise11_mode["platform"]
    if platform == "fixed":
        send_to_esp = False; control_source = "cop"; esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True; control_source = "cop"; esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True; control_source = "exercise11"; esp_send("ARM:1"); time.sleep(0.1); esp_send("AUTO:1")
        if platform == "sinus": threading.Thread(target=exercise8_loop_sinus, daemon=True).start()
        elif platform == "ramp": threading.Thread(target=exercise8_loop_ramp, daemon=True).start()
        else: threading.Thread(target=exercise8_loop_impulses, daemon=True).start()

def exercise11_stop():
    global exercise11_running, send_to_esp, control_source
    exercise11_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")

# ==========================================================
# EXERCICE 12 : PLATEFORME + VIDEO
# ==========================================================
exercise12_running = False
exercise12_mode = {
    "platform": "fixed",
    "amplitude": "medium",
    "speed": "medium",
    "video_on": "on",
    "video_file": "voiture1.mp4",
    "video_mode": "single",
    "video_interval": 20
}

exercise12_video_index = 0

def exercise12_loop_sinus():
    global exercise12_running
    t0 = time.time()
    cmd_now = 0.0
    last_t = time.time()
    while exercise12_running:
        now = time.time()
        dt = now - last_t
        if dt <= 0.0: dt = 0.02
        last_t = now
        amp = exercise2_amp_value(exercise12_mode["amplitude"])
        freq = exercise2_freq_value(exercise12_mode["speed"])
        cmd_target = amp * math.sin(2.0 * math.pi * freq * (now - t0))
        cmd_target = ex2_apply_soft_limit(cmd_target)
        max_step = exercise_slew_per_s(exercise12_mode["speed"]) * dt
        delta = cmd_target - cmd_now
        if delta > max_step: delta = max_step
        elif delta < -max_step: delta = -max_step
        cmd_now += delta
        cmd_now = max(-CMD_MAX, min(CMD_MAX, cmd_now))
        if uart:
            try: uart.write(f"COP:Y:{cmd_now:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise12_loop_ramp():
    global exercise12_running
    t0 = time.time()
    cmd_now = 0.0
    last_t = time.time()
    cycle_s = 24.0
    while exercise12_running:
        now = time.time()
        dt = now - last_t
        if dt <= 0.0: dt = 0.02
        last_t = now
        elapsed = now - t0
        phase_env = (elapsed % cycle_s) / cycle_s
        env = exercise3_envelope(phase_env)
        amp_base = exercise2_amp_value(exercise12_mode["amplitude"])
        freq = exercise2_freq_value(exercise12_mode["speed"])
        cmd_target = (amp_base * env) * math.sin(2.0 * math.pi * freq * elapsed)
        cmd_target = ex2_apply_soft_limit(cmd_target)
        max_step = exercise_slew_per_s(exercise12_mode["speed"]) * dt
        delta = cmd_target - cmd_now
        if delta > max_step: delta = max_step
        elif delta < -max_step: delta = -max_step
        cmd_now += delta
        cmd_now = max(-CMD_MAX, min(CMD_MAX, cmd_now))
        if uart:
            try: uart.write(f"COP:Y:{cmd_now:.4f}\n".encode("ascii"))
            except: pass
        time.sleep(0.02)

def exercise12_loop_impulses():
    global exercise12_running
    rest_cmd = 0.0
    while exercise12_running:
        wait_s = random.uniform(1.0, 3.0)
        t_wait = time.time()
        while exercise12_running and (time.time() - t_wait < wait_s):
            if uart:
                try: uart.write(f"COP:Y:{rest_cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        if not exercise12_running: break
        sign = random.choice([-1.0, 1.0])
        amp = exercise4_amp_value(exercise12_mode["amplitude"])
        pd = exercise_pulse_duration(exercise12_mode["speed"])
        t0 = time.time()
        while exercise12_running:
            phase = (time.time() - t0) / pd
            if phase >= 1.0: break
            cmd = sign * amp * exercise4_pulse_shape(phase)
            if exercise12_mode["speed"] == "high": cmd *= 1.18
            elif exercise12_mode["speed"] == "low": cmd *= 0.90
            cmd = ex2_apply_soft_limit(cmd)
            cmd = max(-CMD_MAX, min(CMD_MAX, cmd))
            if uart:
                try: uart.write(f"COP:Y:{cmd:.4f}\n".encode("ascii"))
                except: pass
            time.sleep(0.02)
        rest_cmd = exercise_pulse_rest_cmd(exercise12_mode["speed"], amp, sign)


def exercise12_video_playlist_loop(playlist):
    global exercise12_running, exercise12_video_index
    if not playlist:
        return
    try:
        interval_s = max(5, int(exercise12_mode.get("video_interval", 20)))
    except:
        interval_s = 20
    while exercise12_running:
        t0 = time.time()
        while exercise12_running and (time.time() - t0 < interval_s):
            time.sleep(0.2)
        if not exercise12_running:
            break
        exercise12_video_index = (exercise12_video_index + 1) % len(playlist)
        set_hdmi(video_file=playlist[exercise12_video_index], video_playlist=playlist, video_index=exercise12_video_index)
def exercise12_start():
    global exercise12_running, send_to_esp, control_source, exercise12_video_index
    exercise12_stop()
    exercise12_running = True
    ensure_chromium()

    playlist = list_static_videos()
    if not playlist:
        playlist = [exercise12_mode["video_file"]]
    if exercise12_mode["video_file"] not in playlist:
        exercise12_mode["video_file"] = playlist[0]
    exercise12_video_index = playlist.index(exercise12_mode["video_file"]) if exercise12_mode["video_file"] in playlist else 0

    if exercise12_mode["video_on"] == "on":
        set_hdmi(mode="video", title="Ex 12 - Video + plateforme",
                 video_file=playlist[exercise12_video_index],
                 video_playlist=playlist, video_index=exercise12_video_index)
        if exercise12_mode.get("video_mode", "single") == "playlist" and len(playlist) > 1:
            threading.Thread(target=exercise12_video_playlist_loop, args=(playlist,), daemon=True).start()
    else:
        set_hdmi(mode="black", title="Ex 12 - Plateforme")

    platform = exercise12_mode["platform"]
    if platform == "fixed":
        send_to_esp = False
        control_source = "cop"
        esp_send("STOP")
    elif platform == "auto":
        send_to_esp = True
        control_source = "cop"
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("AUTO:1")
    elif platform in ["sinus", "ramp", "impulses"]:
        send_to_esp = True
        control_source = "exercise12"
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("AUTO:1")
        if platform == "sinus":
            threading.Thread(target=exercise12_loop_sinus, daemon=True).start()
        elif platform == "ramp":
            threading.Thread(target=exercise12_loop_ramp, daemon=True).start()
        else:
            threading.Thread(target=exercise12_loop_impulses, daemon=True).start()

def exercise12_stop():
    global exercise12_running, send_to_esp, control_source
    exercise12_running = False
    send_to_esp = False
    control_source = "cop"
    esp_send("STOP")
    set_hdmi(mode="off")
# ==========================================================
# EXERCICE 1 : plateforme + Ã©cran
# ==========================================================
def exercise_apply():
    global send_to_esp
    if exercise_mode["platform"] == "auto":
        send_to_esp = True
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("AUTO:1")
    else:
        send_to_esp = False
        esp_send("STOP")
    if exercise_mode["screen"] == "opto":
        start_opto(direction=exercise_mode["direction"], speed=exercise_mode["speed"])
    else:
        start_black_screen()

def exercise_start():
    global exercise_running
    exercise_running = True
    ensure_chromium()
    exercise_apply()

def exercise_stop():
    global exercise_running, send_to_esp
    exercise_running = False
    send_to_esp = False
    esp_send("STOP")
    set_hdmi(mode="off")

@app.route("/exercise1/status")
def exercise1_status():
    return Response(json.dumps({
        "running": exercise_running,
        "platform": exercise_mode["platform"],
        "screen": exercise_mode["screen"],
        "direction": exercise_mode["direction"],
        "speed": exercise_mode["speed"],
        "hdmi": hdmi_state,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise1/start")
def exercise1_start():
    exercise_start()
    return "EXERCISE1 STARTED\n"

@app.route("/exercise1/stop")
def exercise1_stop():
    exercise_stop()
    return "EXERCISE1 STOPPED\n"

@app.route("/exercise1/set")
def exercise1_set():
    platform = request.args.get("platform")
    screen = request.args.get("screen")
    direction = request.args.get("direction")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto"]:
        exercise_mode["platform"] = platform
    if screen in ["black", "opto"]:
        exercise_mode["screen"] = screen
    if direction in ["right", "left", "up", "down"]:
        exercise_mode["direction"] = direction
    if speed is not None:
        try:
            v = int(speed)
            exercise_mode["speed"] = max(1, min(30, v))
        except: pass
    if exercise_running:
        exercise_apply()
    return Response(json.dumps({"ok": True, "exercise_mode": exercise_mode}), mimetype="application/json")

@app.route("/exercise2/status")
def exercise2_status():
    return Response(json.dumps({
        "running": exercise2_running,
        "mode": exercise2_mode,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise2/start")
def route_exercise2_start():
    exercise2_start()
    return "EXERCISE2 STARTED\n"

@app.route("/exercise2/stop")
def route_exercise2_stop():
    exercise2_stop()
    return "EXERCISE2 STOPPED\n"

@app.route("/exercise2/set")
def route_exercise2_set():
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    screen = request.args.get("screen")
    direction = request.args.get("direction")
    opto_speed = request.args.get("opto_speed")
    if amplitude in ["low", "medium", "high"]:
        exercise2_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]:
        exercise2_mode["speed"] = speed
    if screen in ["black", "opto"]:
        exercise2_mode["screen"] = screen
    if direction in ["right", "left", "up", "down"]:
        exercise2_mode["direction"] = direction
    if opto_speed is not None:
        try: exercise2_mode["opto_speed"] = max(1, min(30, int(opto_speed)))
        except: pass
    if exercise2_running:
        if exercise2_mode["screen"] == "opto":
            start_opto(direction=exercise2_mode["direction"], speed=exercise2_mode["opto_speed"])
        else:
            start_black_screen()
    return Response(json.dumps({"ok": True, "exercise2_mode": exercise2_mode}), mimetype="application/json")

@app.route("/exercise3/status")
def exercise3_status():
    return Response(json.dumps({
        "running": exercise3_running,
        "mode": exercise3_mode,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise3/start")
def route_exercise3_start():
    exercise3_start()
    return "EXERCISE3 STARTED\n"

@app.route("/exercise3/stop")
def route_exercise3_stop():
    exercise3_stop()
    return "EXERCISE3 STOPPED\n"

@app.route("/exercise3/set")
def route_exercise3_set():
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    screen = request.args.get("screen")
    direction = request.args.get("direction")
    opto_speed = request.args.get("opto_speed")
    duration = request.args.get("duration")
    if amplitude in ["low", "medium", "high"]:
        exercise3_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]:
        exercise3_mode["speed"] = speed
    if screen in ["black", "opto"]:
        exercise3_mode["screen"] = screen
    if direction in ["right", "left", "up", "down"]:
        exercise3_mode["direction"] = direction
    if opto_speed is not None:
        try: exercise3_mode["opto_speed"] = max(1, min(30, int(opto_speed)))
        except: pass
    if duration is not None:
        try: exercise3_mode["duration"] = max(5, min(120, int(duration)))
        except: pass
    if exercise3_running:
        if exercise3_mode["screen"] == "opto":
            start_opto(direction=exercise3_mode["direction"], speed=exercise3_mode["opto_speed"])
        else:
            start_black_screen()
    return Response(json.dumps({"ok": True, "exercise3_mode": exercise3_mode}), mimetype="application/json")

@app.route("/exercise4/status")
def exercise4_status():
    return Response(json.dumps({
        "running": exercise4_running,
        "mode": exercise4_mode,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise4/start")
def route_exercise4_start():
    exercise4_start()
    return "EXERCISE4 STARTED\n"

@app.route("/exercise4/stop")
def route_exercise4_stop():
    exercise4_stop()
    return "EXERCISE4 STOPPED\n"

@app.route("/exercise4/set")
def route_exercise4_set():
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    screen = request.args.get("screen")
    direction = request.args.get("direction")
    opto_speed = request.args.get("opto_speed")
    gap_min = request.args.get("gap_min")
    gap_max = request.args.get("gap_max")
    pulse_ms = request.args.get("pulse_ms")
    if amplitude in ["low", "medium", "high"]: exercise4_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise4_mode["speed"] = speed
    if screen in ["black", "opto"]: exercise4_mode["screen"] = screen
    if direction in ["right", "left", "up", "down"]: exercise4_mode["direction"] = direction
    if opto_speed is not None:
        try: exercise4_mode["opto_speed"] = max(1, min(30, int(opto_speed)))
        except: pass
    if gap_min is not None:
        try: exercise4_mode["gap_min"] = max(0.2, float(gap_min))
        except: pass
    if gap_max is not None:
        try: exercise4_mode["gap_max"] = max(exercise4_mode["gap_min"], float(gap_max))
        except: pass
    if pulse_ms is not None:
        try: exercise4_mode["pulse_ms"] = max(200, min(3000, int(pulse_ms)))
        except: pass
    if exercise4_running:
        if exercise4_mode["screen"] == "opto":
            start_opto(direction=exercise4_mode["direction"], speed=exercise4_mode["opto_speed"])
        else:
            start_black_screen()
    return Response(json.dumps({"ok": True, "exercise4_mode": exercise4_mode}), mimetype="application/json")

@app.route("/exercise5/status")
def exercise5_status():
    return Response(json.dumps({
        "running": exercise5_running,
        "mode": exercise5_mode,
        "control_source": control_source,
        "send_to_esp": send_to_esp,
        "hdmi": hdmi_state
    }), mimetype="application/json")

@app.route("/exercise5/start")
def route_exercise5_start():
    exercise5_start()
    return "EXERCISE5 STARTED\n"

@app.route("/exercise5/stop")
def route_exercise5_stop():
    exercise5_stop()
    return "EXERCISE5 STOPPED\n"

@app.route("/exercise5/set")
def route_exercise5_set():
    platform = request.args.get("platform")
    vor_mode = request.args.get("vor_mode")
    vor_interval = request.args.get("vor_interval")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]:
        exercise5_mode["platform"] = platform
    if vor_mode in ["lr", "ud", "diag1", "diag2", "random"]:
        exercise5_mode["vor_mode"] = vor_mode
    if vor_interval is not None:
        try:
            v = int(vor_interval)
            if v in [2, 5, 10]: exercise5_mode["vor_interval"] = v
        except: pass
    if amplitude in ["low", "medium", "high"]: exercise5_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise5_mode["speed"] = speed
    if exercise5_running:
        pair = exercise5_next_pair(exercise5_mode["vor_mode"])
        set_hdmi(mode="vor", vor_pair=pair)
    return Response(json.dumps({"ok": True, "exercise5_mode": exercise5_mode}), mimetype="application/json")

@app.route("/exercise6/status")
def exercise6_status():
    return Response(json.dumps({
        "running": exercise6_running,
        "mode": exercise6_mode,
        "control_source": control_source,
        "send_to_esp": send_to_esp,
        "hdmi": hdmi_state
    }), mimetype="application/json")

@app.route("/exercise6/start")
def route_exercise6_start():
    exercise6_start()
    return "EXERCISE6 STARTED\n"

@app.route("/exercise6/stop")
def route_exercise6_stop():
    exercise6_stop()
    return "EXERCISE6 STOPPED\n"

@app.route("/exercise6/set")
def route_exercise6_set():
    platform = request.args.get("platform")
    point_mode = request.args.get("point_mode")
    point_speed = request.args.get("point_speed")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]:
        exercise6_mode["platform"] = platform
    if point_mode in ["lr", "ud", "circle", "infinity"]:
        exercise6_mode["point_mode"] = point_mode
    if point_speed in ["low", "medium", "high"]:
        exercise6_mode["point_speed"] = point_speed
    if amplitude in ["low", "medium", "high"]:
        exercise6_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]:
        exercise6_mode["speed"] = speed
    if exercise6_running:
        exercise6_set_screen()
    return Response(json.dumps({"ok": True, "exercise6_mode": exercise6_mode}), mimetype="application/json")

@app.route("/exercise7/status")
def exercise7_status():
    return Response(json.dumps({
        "running": exercise7_running,
        "mode": exercise7_mode,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise7/start")
def route_exercise7_start():
    exercise7_start()
    return "EXERCISE7 STARTED\n"

@app.route("/exercise7/stop")
def route_exercise7_stop():
    exercise7_stop()
    return "EXERCISE7 STOPPED\n"

@app.route("/exercise7/set")
def route_exercise7_set():
    platform = request.args.get("platform")
    interval = request.args.get("interval")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]:
        exercise7_mode["platform"] = platform
    if interval is not None:
        try:
            v = int(interval)
            if v in [2, 5, 10]: exercise7_mode["interval"] = v
        except: pass
    if amplitude in ["low", "medium", "high"]: exercise7_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise7_mode["speed"] = speed
    return Response(json.dumps({"ok": True, "exercise7_mode": exercise7_mode}), mimetype="application/json")

@app.route("/exercise8/status")
def exercise8_status():
    return Response(json.dumps({
        "running": exercise8_running,
        "mode": exercise8_mode,
        "score": exercise8_score,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise8/start")
def route_exercise8_start():
    exercise8_start()
    return "EXERCISE8 STARTED\n"

@app.route("/exercise8/stop")
def route_exercise8_stop():
    exercise8_stop()
    return "EXERCISE8 STOPPED\n"

@app.route("/exercise8/set")
def route_exercise8_set():
    platform = request.args.get("platform")
    target = request.args.get("target")
    target_mode = request.args.get("target_mode")
    difficulty = request.args.get("difficulty")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]: exercise8_mode["platform"] = platform
    if target in ["front", "back", "left", "right", "center"]: exercise8_mode["target"] = target
    if target_mode in ["single", "random"]: exercise8_mode["target_mode"] = target_mode
    if difficulty in ["low", "medium", "high"]: exercise8_mode["difficulty"] = difficulty
    if amplitude in ["low", "medium", "high"]: exercise8_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise8_mode["speed"] = speed
    return Response(json.dumps({"ok": True, "exercise8_mode": exercise8_mode}), mimetype="application/json")


@app.route("/exercise9/status")
def exercise9_status():
    return Response(json.dumps({
        "running": exercise9_running,
        "mode": exercise9_mode,
        "score": exercise9_score,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise9/start")
def route_exercise9_start():
    exercise9_start()
    return "EXERCISE9 STARTED\n"

@app.route("/exercise9/stop")
def route_exercise9_stop():
    exercise9_stop()
    return "EXERCISE9 STOPPED\n"

@app.route("/exercise9/set")
def route_exercise9_set():
    platform = request.args.get("platform")
    difficulty = request.args.get("difficulty")
    sequence = request.args.get("sequence")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]: exercise9_mode["platform"] = platform
    if difficulty in ["low", "medium", "high"]: exercise9_mode["difficulty"] = difficulty
    if sequence in ["cross", "square", "star"]: exercise9_mode["sequence"] = sequence
    if amplitude in ["low", "medium", "high"]: exercise9_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise9_mode["speed"] = speed
    return Response(json.dumps({"ok": True, "exercise9_mode": exercise9_mode}), mimetype="application/json")

@app.route("/exercise10/status")
def exercise10_status():
    return Response(json.dumps({
        "running": exercise10_running,
        "mode": exercise10_mode,
        "score": exercise10_score,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise10/start")
def route_exercise10_start():
    exercise10_start()
    return "EXERCISE10 STARTED\n"

@app.route("/exercise10/stop")
def route_exercise10_stop():
    exercise10_stop()
    return "EXERCISE10 STOPPED\n"

@app.route("/exercise10/set")
def route_exercise10_set():
    platform = request.args.get("platform")
    difficulty = request.args.get("difficulty")
    path = request.args.get("path")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]: exercise10_mode["platform"] = platform
    if difficulty in ["low", "medium", "high"]: exercise10_mode["difficulty"] = difficulty
    if path in ["infinity", "circle", "square"]: exercise10_mode["path"] = path
    if amplitude in ["low", "medium", "high"]: exercise10_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise10_mode["speed"] = speed
    return Response(json.dumps({"ok": True, "exercise10_mode": exercise10_mode}), mimetype="application/json")

@app.route("/exercise11/status")
def exercise11_status():
    return Response(json.dumps({
        "running": exercise11_running,
        "mode": exercise11_mode,
        "score": exercise11_score,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise11/start")
def route_exercise11_start():
    exercise11_start()
    return "EXERCISE11 STARTED\n"

@app.route("/exercise11/stop")
def route_exercise11_stop():
    exercise11_stop()
    return "EXERCISE11 STOPPED\n"

@app.route("/exercise11/set")
def route_exercise11_set():
    platform = request.args.get("platform")
    difficulty = request.args.get("difficulty")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]: exercise11_mode["platform"] = platform
    if difficulty in ["low", "medium", "high"]: exercise11_mode["difficulty"] = difficulty
    if amplitude in ["low", "medium", "high"]: exercise11_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise11_mode["speed"] = speed
    return Response(json.dumps({"ok": True, "exercise11_mode": exercise11_mode}), mimetype="application/json")


@app.route("/videos/list")
def videos_list_route():
    vids = list_static_videos()
    return Response(json.dumps({"videos": vids}), mimetype="application/json")
@app.route("/exercise12/status")
def exercise12_status():
    return Response(json.dumps({
        "running": exercise12_running,
        "mode": exercise12_mode,
        "control_source": control_source,
        "send_to_esp": send_to_esp
    }), mimetype="application/json")

@app.route("/exercise12/start")
def route_exercise12_start():
    exercise12_start()
    return "EXERCISE12 STARTED\n"

@app.route("/exercise12/stop")
def route_exercise12_stop():
    exercise12_stop()
    return "EXERCISE12 STOPPED\n"

@app.route("/exercise12/set")
def route_exercise12_set():
    platform = request.args.get("platform")
    amplitude = request.args.get("amplitude")
    speed = request.args.get("speed")
    video_on = request.args.get("video_on")
    video_file = request.args.get("video_file")
    video_mode = request.args.get("video_mode")
    video_interval = request.args.get("video_interval")
    if platform in ["fixed", "auto", "sinus", "ramp", "impulses"]: exercise12_mode["platform"] = platform
    if amplitude in ["low", "medium", "high"]: exercise12_mode["amplitude"] = amplitude
    if speed in ["low", "medium", "high"]: exercise12_mode["speed"] = speed
    if video_on in ["on", "off"]: exercise12_mode["video_on"] = video_on
    if video_mode in ["single", "playlist"]: exercise12_mode["video_mode"] = video_mode
    if video_interval is not None:
        try:
            iv = int(video_interval)
            exercise12_mode["video_interval"] = max(5, min(300, iv))
        except:
            pass
    if video_file:
        safe = os.path.basename(str(video_file))
        if safe.lower().endswith(".mp4"):
            exercise12_mode["video_file"] = safe
    return Response(json.dumps({"ok": True, "exercise12_mode": exercise12_mode}), mimetype="application/json")
@app.route("/exercices/tare")
def exercices_tare():
    tare()
    return "OK TARE\n"

@app.route("/exercices/center")
def exercices_center():
    ok = set_center_offset()
    if ok:
        esp_send("ARM:1")
        time.sleep(0.1)
        esp_send("HOME")
        time.sleep(5)
        esp_send("CENTER")
        return "OK CENTER + HOME + CENTER\n"
    return "ERROR: no load\n"

@app.route("/exercices")
def exercices():
    return """
<!DOCTYPE html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>Exercices</title>
<style>
:root{--bg1:#eef7ff;--bg2:#f4fff7;--ink:#183043;--muted:#5b7388;--line:#d4e3f1;--card:#ffffff;--ok:#2e7d32;--danger:#c62828;--blue:#1565c0;--orange:#ef6c00;--gray:#546e7a}
*{box-sizing:border-box}
body{font-family:"Segoe UI",Tahoma,sans-serif;margin:0;padding:16px;color:var(--ink);background:radial-gradient(1200px 700px at 0% -5%,#dff0ff 0%,transparent 62%),radial-gradient(1200px 700px at 100% -8%,#e6ffe9 0%,transparent 56%),linear-gradient(145deg,var(--bg1) 0%,#fff 56%,var(--bg2) 100%)}
h1{margin:4px 0 14px 0;font-size:clamp(22px,4vw,30px)}
.step,.box{border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:0 8px 18px rgba(16,42,67,.08)}
.step{padding:14px;margin:10px 0}
.box{padding:14px;margin:12px 0}
.active{border-color:#2196F3;background:#eaf5ff}
.done{border-color:#4CAF50;background:#edf9ef}
button,select,input{padding:11px 12px;font-size:15px;margin:5px;border-radius:10px;border:1px solid #cfddeb}
button{border:none;color:#fff;cursor:pointer;min-height:44px}
.green{background:var(--ok)}.red{background:var(--danger)}.blue{background:var(--blue)}.orange{background:var(--orange)}.gray{background:var(--gray)}
button:disabled{opacity:.45;cursor:not-allowed}
.status{font-size:14px;margin-top:10px;color:#26445d;background:#f4f9ff;border-radius:12px;padding:12px;border:1px solid #dceaf8}
.hidden{display:none}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.arrows{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.arrows button{font-size:22px;padding:12px 16px}
@media (max-width:420px){body{padding:10px} h1{font-size:22px} .step,.box{padding:10px;margin:8px 0} button,select,input{font-size:14px;padding:10px} .arrows button{font-size:19px;padding:10px 12px}}
</style></head><body>
<h1>Exercices de reeducation</h1>
<div class="step active" id="step1"><b>1. TARE (plateforme vide)</b><br><button class="blue" onclick="doTare()">TARE A VIDE</button><span id="tare_st"></span></div>
<div class="step" id="step2"><b>2. SET CENTER (patient immobile)</b><br><button class="blue" onclick="doCenter()" id="btn_center" disabled>SET CENTER + HOME</button><span id="center_st"></span></div>
<div class="step" id="step3">
<h3>Choix de l exercice</h3>
<select id="exChoice" onchange="switchEx()">
<option value="ex1">Ex 1 - Fixe / Asservie</option><option value="ex2">Ex 2 - Sinusoide</option><option value="ex3">Ex 3 - Petit Grand Petit</option><option value="ex4">Ex 4 - Impulsions</option><option value="ex5">Ex 5 - Points VOR</option><option value="ex6">Ex 6 - Point mobile</option><option value="ex7">Ex 7 - Citations</option><option value="ex8">Ex 8 - Cible COP</option><option value="ex9">Ex 9 - Cibles sequentielles</option><option value="ex10">Ex 10 - Parcours</option><option value="ex11">Ex 11 - Labyrinthe</option><option value="ex12">Ex 12 - Plateforme + Video</option>
</select>

<div id="ex1_panel"><div class="box"><h3>Plateforme</h3><select id="ex1_platform"><option value="fixed">Fixe</option><option value="auto">Asservie</option></select></div></div>
<div id="ex2_panel" class="hidden"><div class="box"><h3>Amplitude</h3><select id="ex2_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select></div><div class="box"><h3>Vitesse</h3><select id="ex2_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex3_panel" class="hidden"><div class="box"><h3>Amplitude</h3><select id="ex3_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select></div><div class="box"><h3>Vitesse</h3><select id="ex3_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div><div class="box"><h3>Duree (s)</h3><input id="ex3_duration" type="number" min="5" max="120" value="30"/></div></div>
<div id="ex4_panel" class="hidden"><div class="box"><h3>Amplitude</h3><select id="ex4_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select></div><div class="box"><h3>Vitesse</h3><select id="ex4_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div><div class="box"><h3>Delai (s)</h3>Min:<input id="ex4_gap_min" type="number" step="0.1" min="0.2" max="20" value="1.5" style="width:70px"/> Max:<input id="ex4_gap_max" type="number" step="0.1" min="0.2" max="20" value="4.0" style="width:70px"/></div><div class="box"><h3>Duree impulsion (ms)</h3><input id="ex4_pulse_ms" type="number" min="200" max="3000" value="900"/></div></div>
<div id="ex5_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex5_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Mode points</h3><select id="ex5_vor_mode"><option value="lr">Gauche/Droite</option><option value="ud">Haut/Bas</option><option value="diag1">Diag 1</option><option value="diag2">Diag 2</option><option value="random">Aleatoire</option></select></div><div class="box"><h3>Temps paire</h3><select id="ex5_interval"><option value="2">2s</option><option value="5">5s</option><option value="10">10s</option></select></div><div class="box" id="ex5_motion_box"><h3>Amplitude</h3><select id="ex5_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse</h3><select id="ex5_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex6_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex6_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Trajectoire</h3><select id="ex6_point_mode"><option value="lr">Gauche-Droite</option><option value="ud">Haut-Bas</option><option value="circle">Cercle</option><option value="infinity">Huit couche</option></select></div><div class="box"><h3>Vitesse point</h3><select id="ex6_point_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div><div class="box" id="ex6_motion_box"><h3>Amplitude plat.</h3><select id="ex6_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse plat.</h3><select id="ex6_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex7_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex7_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Intervalle citations</h3><select id="ex7_interval"><option value="2">2s</option><option value="5" selected>5s</option><option value="10">10s</option></select></div><div class="box" id="ex7_motion_box"><h3>Amplitude</h3><select id="ex7_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse</h3><select id="ex7_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex8_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex8_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Mode cible</h3><select id="ex8_target_mode" onchange="syncEx8TargetField()"><option value="single">Cible fixe</option><option value="random">Cibles aleatoires</option></select></div><div class="box" id="ex8_target_box"><h3>Cible (mode fixe)</h3><select id="ex8_target"><option value="center">Centre</option><option value="front">Avant</option><option value="back">Arriere</option><option value="left">Gauche</option><option value="right">Droite</option></select></div><div class="box"><h3>Difficulte</h3><select id="ex8_difficulty"><option value="low">Facile</option><option value="medium" selected>Moyen</option><option value="high">Difficile</option></select></div><div class="box" id="ex8_motion_box"><h3>Amplitude plat.</h3><select id="ex8_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse plat.</h3><select id="ex8_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex9_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex9_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Sequence</h3><select id="ex9_sequence"><option value="cross">Croix</option><option value="square">Carre</option><option value="star">Etoile</option></select></div><div class="box"><h3>Difficulte</h3><select id="ex9_difficulty"><option value="low">Facile</option><option value="medium" selected>Moyen</option><option value="high">Difficile</option></select></div><div class="box" id="ex9_motion_box"><h3>Amplitude plat.</h3><select id="ex9_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse plat.</h3><select id="ex9_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex10_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex10_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Parcours</h3><select id="ex10_path"><option value="infinity">Huit couche</option><option value="circle">Cercle</option><option value="square">Carre</option></select></div><div class="box"><h3>Difficulte</h3><select id="ex10_difficulty"><option value="low">Facile</option><option value="medium" selected>Moyen</option><option value="high">Difficile</option></select></div><div class="box" id="ex10_motion_box"><h3>Amplitude plat.</h3><select id="ex10_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse plat.</h3><select id="ex10_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex11_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex11_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Difficulte</h3><select id="ex11_difficulty"><option value="low">Facile</option><option value="medium" selected>Moyen</option><option value="high">Difficile</option></select></div><div class="box" id="ex11_motion_box"><h3>Amplitude plat.</h3><select id="ex11_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse plat.</h3><select id="ex11_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>
<div id="ex12_panel" class="hidden"><div class="box"><h3>Plateforme</h3><select id="ex12_platform" onchange="syncPlatformOptions()"><option value="fixed">Fixe</option><option value="auto">Asservie</option><option value="sinus">Sinus</option><option value="ramp">Petit-Grand-Petit</option><option value="impulses">Impulsions</option></select></div><div class="box"><h3>Affichage video</h3><select id="ex12_video_on"><option value="on">Video ON</option><option value="off">Noir</option></select><h3>Mode video</h3><select id="ex12_video_mode"><option value="single">Video unique</option><option value="playlist">Playlist auto</option></select><h3>Changement (s)</h3><input id="ex12_video_interval" type="number" min="5" max="300" value="20"/><h3>Fichier video</h3><select id="ex12_video_file"><option value="voiture1.mp4">voiture1.mp4</option></select></div><div class="box" id="ex12_motion_box"><h3>Amplitude plat.</h3><select id="ex12_amplitude"><option value="low">Leger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select><h3>Vitesse plat.</h3><select id="ex12_speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select></div></div>

<div id="screen_panel"><div class="box"><h3>Ecran HDMI (Ex 1-4)</h3><select id="screen" onchange="applyScreen()"><option value="black">Noir</option><option value="opto">Optocinetique</option></select><div id="opto_controls" class="hidden" style="margin-top:10px"><b>Direction</b><div class="arrows"><button class="orange" onclick="setDir('left')">&#11013;</button><button class="orange" onclick="setDir('right')">&#10145;</button><button class="orange" onclick="setDir('up')">&#11014;</button><button class="orange" onclick="setDir('down')">&#11015;</button></div><b>Vitesse bandes</b><button class="gray" onclick="chgOS(-2)">-</button><span id="speedv" style="font-size:20px;font-weight:bold">6</span><button class="gray" onclick="chgOS(2)">+</button></div></div></div>

<div class="box"><button class="green" onclick="startEx()" id="btn_start" disabled>START</button><button class="red" onclick="stopEx()">STOP</button></div>
</div>
<div class="status" id="st">-</div>
<div class="box row"><button class="blue" onclick="window.location='/'">Accueil</button><button class="blue" onclick="window.location='/sot'">SOT</button><button class="red" onclick="shutdownPi()" style="font-size:13px">ETEINDRE LE RASPBERRY</button></div>

<script>
var os=6,cx="ex1",exAll=["ex1","ex2","ex3","ex4","ex5","ex6","ex7","ex8","ex9","ex10","ex11","ex12"];
var hideScreen=["ex5","ex6","ex7","ex8","ex9","ex10","ex11","ex12"];
function setHidden(id,hide){var e=document.getElementById(id);if(e)e.className=hide?"hidden":"";}
function isDynamicPlatform(v){return v==="sinus"||v==="ramp"||v==="impulses";}
function syncPlatformOptions(){setHidden("ex5_motion_box",!isDynamicPlatform(document.getElementById("ex5_platform").value));setHidden("ex6_motion_box",!isDynamicPlatform(document.getElementById("ex6_platform").value));setHidden("ex7_motion_box",!isDynamicPlatform(document.getElementById("ex7_platform").value));setHidden("ex8_motion_box",!isDynamicPlatform(document.getElementById("ex8_platform").value));setHidden("ex9_motion_box",!isDynamicPlatform(document.getElementById("ex9_platform").value));setHidden("ex10_motion_box",!isDynamicPlatform(document.getElementById("ex10_platform").value));setHidden("ex11_motion_box",!isDynamicPlatform(document.getElementById("ex11_platform").value));setHidden("ex12_motion_box",!isDynamicPlatform(document.getElementById("ex12_platform").value));}
function syncEx8TargetField(){setHidden("ex8_target_box",document.getElementById("ex8_target_mode").value!=="single");}
function syncOptoControls(){var showOpto=(hideScreen.indexOf(cx)<0)&&document.getElementById("screen").value==="opto";setHidden("opto_controls",!showOpto);}
function switchEx(){cx=document.getElementById("exChoice").value;exAll.forEach(function(e){document.getElementById(e+"_panel").className=(cx===e)?"":"hidden";});document.getElementById("screen_panel").className=hideScreen.indexOf(cx)>=0?"hidden":"";syncPlatformOptions();syncEx8TargetField();syncOptoControls();}
async function loadVideoList(){try{var r=await fetch('/videos/list');var d=await r.json();var s=document.getElementById('ex12_video_file');if(!s)return;var current=s.value;s.innerHTML='';var vids=(d.videos||[]);if(!vids.length)vids=['voiture1.mp4'];vids.forEach(function(v){var o=document.createElement('option');o.value=v;o.textContent=v;s.appendChild(o);});if(vids.indexOf(current)>=0)s.value=current;}catch(e){}}
async function doTare(){document.getElementById("tare_st").textContent="...";await fetch("/exercices/tare");document.getElementById("tare_st").textContent="OK";document.getElementById("step1").className="step done";document.getElementById("step2").className="step active";document.getElementById("btn_center").disabled=false;}
async function doCenter(){document.getElementById("center_st").textContent="HOME...";document.getElementById("btn_center").disabled=true;var r=await fetch("/exercices/center");var t=await r.text();if(t.indexOf("ERROR")>=0){document.getElementById("center_st").textContent="ERREUR";document.getElementById("btn_center").disabled=false;return;}document.getElementById("center_st").textContent="OK";document.getElementById("step2").className="step done";document.getElementById("step3").className="step active";document.getElementById("btn_start").disabled=false;}
function setDir(d){for(var i=1;i<=4;i++)fetch("/exercise"+i+"/set?direction="+d);}
function chgOS(delta){os=Math.max(1,Math.min(30,os+delta));document.getElementById("speedv").textContent=os;fetch("/exercise1/set?speed="+os);for(var i=2;i<=4;i++)fetch("/exercise"+i+"/set?opto_speed="+os);}
function applyScreen(){var s=document.getElementById("screen").value;for(var i=1;i<=4;i++)fetch("/exercise"+i+"/set?screen="+s);syncOptoControls();}
async function startEx(){
  if(cx==="ex1"){fetch("/exercise1/set?platform="+document.getElementById("ex1_platform").value+"&screen="+document.getElementById("screen").value+"&speed="+os);await fetch("/exercise1/start");}
  if(cx==="ex2"){fetch("/exercise2/set?amplitude="+document.getElementById("ex2_amplitude").value+"&speed="+document.getElementById("ex2_speed").value+"&screen="+document.getElementById("screen").value+"&opto_speed="+os);await fetch("/exercise2/start");}
  if(cx==="ex3"){fetch("/exercise3/set?amplitude="+document.getElementById("ex3_amplitude").value+"&speed="+document.getElementById("ex3_speed").value+"&screen="+document.getElementById("screen").value+"&opto_speed="+os+"&duration="+document.getElementById("ex3_duration").value);await fetch("/exercise3/start");}
  if(cx==="ex4"){fetch("/exercise4/set?amplitude="+document.getElementById("ex4_amplitude").value+"&speed="+document.getElementById("ex4_speed").value+"&screen="+document.getElementById("screen").value+"&opto_speed="+os+"&gap_min="+document.getElementById("ex4_gap_min").value+"&gap_max="+document.getElementById("ex4_gap_max").value+"&pulse_ms="+document.getElementById("ex4_pulse_ms").value);await fetch("/exercise4/start");}
  if(cx==="ex5"){fetch("/exercise5/set?platform="+document.getElementById("ex5_platform").value+"&vor_mode="+document.getElementById("ex5_vor_mode").value+"&vor_interval="+document.getElementById("ex5_interval").value+"&amplitude="+document.getElementById("ex5_amplitude").value+"&speed="+document.getElementById("ex5_speed").value);await fetch("/exercise5/start");}
  if(cx==="ex6"){fetch("/exercise6/set?platform="+document.getElementById("ex6_platform").value+"&point_mode="+document.getElementById("ex6_point_mode").value+"&point_speed="+document.getElementById("ex6_point_speed").value+"&amplitude="+document.getElementById("ex6_amplitude").value+"&speed="+document.getElementById("ex6_speed").value);await fetch("/exercise6/start");}
  if(cx==="ex7"){fetch("/exercise7/set?platform="+document.getElementById("ex7_platform").value+"&interval="+document.getElementById("ex7_interval").value+"&amplitude="+document.getElementById("ex7_amplitude").value+"&speed="+document.getElementById("ex7_speed").value);await fetch("/exercise7/start");}
  if(cx==="ex8"){fetch("/exercise8/set?platform="+document.getElementById("ex8_platform").value+"&target="+document.getElementById("ex8_target").value+"&target_mode="+document.getElementById("ex8_target_mode").value+"&difficulty="+document.getElementById("ex8_difficulty").value+"&amplitude="+document.getElementById("ex8_amplitude").value+"&speed="+document.getElementById("ex8_speed").value);await fetch("/exercise8/start");}
  if(cx==="ex9"){fetch("/exercise9/set?platform="+document.getElementById("ex9_platform").value+"&difficulty="+document.getElementById("ex9_difficulty").value+"&sequence="+document.getElementById("ex9_sequence").value+"&amplitude="+document.getElementById("ex9_amplitude").value+"&speed="+document.getElementById("ex9_speed").value);await fetch("/exercise9/start");}
  if(cx==="ex10"){fetch("/exercise10/set?platform="+document.getElementById("ex10_platform").value+"&difficulty="+document.getElementById("ex10_difficulty").value+"&path="+document.getElementById("ex10_path").value+"&amplitude="+document.getElementById("ex10_amplitude").value+"&speed="+document.getElementById("ex10_speed").value);await fetch("/exercise10/start");}
  if(cx==="ex11"){fetch("/exercise11/set?platform="+document.getElementById("ex11_platform").value+"&difficulty="+document.getElementById("ex11_difficulty").value+"&amplitude="+document.getElementById("ex11_amplitude").value+"&speed="+document.getElementById("ex11_speed").value);await fetch("/exercise11/start");}
  if(cx==="ex12"){fetch("/exercise12/set?platform="+document.getElementById("ex12_platform").value+"&amplitude="+document.getElementById("ex12_amplitude").value+"&speed="+document.getElementById("ex12_speed").value+"&video_on="+document.getElementById("ex12_video_on").value+"&video_mode="+document.getElementById("ex12_video_mode").value+"&video_interval="+document.getElementById("ex12_video_interval").value+"&video_file="+document.getElementById("ex12_video_file").value);await fetch("/exercise12/start");}
}
async function stopEx(){for(var i=1;i<=12;i++)await fetch("/exercise"+i+"/stop");}
async function refreshStatus(){
  try{var u="/exercise1/status";
  if(cx==="ex2")u="/exercise2/status";if(cx==="ex3")u="/exercise3/status";if(cx==="ex4")u="/exercise4/status";
  if(cx==="ex5")u="/exercise5/status";if(cx==="ex6")u="/exercise6/status";if(cx==="ex7")u="/exercise7/status";
  if(cx==="ex8")u="/exercise8/status";if(cx==="ex9")u="/exercise9/status";if(cx==="ex10")u="/exercise10/status";if(cx==="ex11")u="/exercise11/status";if(cx==="ex12")u="/exercise12/status";
  var r=await fetch(u);var s=await r.json();
  if(cx==="ex1")document.getElementById("st").textContent="EX1 | run="+s.running+" | plat="+s.platform;
  if(cx==="ex2")document.getElementById("st").textContent="EX2 | run="+s.running+" | amp="+s.mode.amplitude+" | vit="+s.mode.speed;
  if(cx==="ex3")document.getElementById("st").textContent="EX3 | run="+s.running+" | amp="+s.mode.amplitude+" | dur="+s.mode.duration;
  if(cx==="ex4")document.getElementById("st").textContent="EX4 | run="+s.running+" | amp="+s.mode.amplitude+" | gap="+s.mode.gap_min+"-"+s.mode.gap_max;
  if(cx==="ex5")document.getElementById("st").textContent="EX5 | run="+s.running+" | plat="+s.mode.platform+" | vor="+s.mode.vor_mode;
  if(cx==="ex6")document.getElementById("st").textContent="EX6 | run="+s.running+" | plat="+s.mode.platform+" | pt="+s.mode.point_mode;
  if(cx==="ex7")document.getElementById("st").textContent="EX7 | run="+s.running+" | plat="+s.mode.platform+" | int="+s.mode.interval+"s";
  if(cx==="ex8")document.getElementById("st").textContent="EX8 | run="+s.running+" | plat="+s.mode.platform+" | maintien="+(s.score.hold_time||0).toFixed(1)+"/"+(s.score.goal_s||5).toFixed(0)+"s";
  if(cx==="ex9")document.getElementById("st").textContent="EX9 | run="+s.running+" | seq="+s.mode.sequence+" | idx="+(s.score.index||0)+" | laps="+(s.score.laps||0);
  if(cx==="ex10")document.getElementById("st").textContent="EX10 | run="+s.running+" | path="+s.mode.path+" | idx="+(s.score.index||0)+" | ok="+(s.score.completed||0);
  if(cx==="ex11")document.getElementById("st").textContent="EX11 | run="+s.running+" | maze="+(s.score.index||0)+" | off="+(s.score.offtrack||0);
  if(cx==="ex12")document.getElementById("st").textContent="EX12 | run="+s.running+" | plat="+s.mode.platform+" | video="+s.mode.video_on+" | mode="+s.mode.video_mode+" | file="+s.mode.video_file;
  }catch(e){}
}
setInterval(refreshStatus,500);refreshStatus();switchEx();loadVideoList();
async function shutdownPi(){if(!confirm("Eteindre le Raspberry ?"))return;await fetch("/system/shutdown");document.getElementById("st").textContent="Arret en cours...";}
</script>
</body></html>
"""

# ==========================================================
# MAIN
# ==========================================================
def main():
    global uart, INVERT_Y_CMD, TOTAL_MIN

    parser = argparse.ArgumentParser()
    parser.add_argument("--uart", default="/dev/ttyUSB0")
    parser.add_argument("--invert", action="store_true", help="inverse le sens de commande Y (si Ã§a part Ã  l'envers)")
    parser.add_argument("--total_min", type=float, default=TOTAL_MIN, help="seuil 'personne dessus'")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    INVERT_Y_CMD = bool(args.invert)
    TOTAL_MIN = float(args.total_min)

    init_phidgets()
    time.sleep(0.5)

    # UART
    try:
        uart = serial.Serial(args.uart, 115200, timeout=0.1)
        print(f"[OK] UART: {args.uart}")
    except Exception as e:
        uart = None
        print(f"[WARN] UART not available: {e}")

    # Tare auto au dÃ©marrage (tu peux refaire via bouton)
    print("[INFO] Auto TARE...")
    tare()
    print("[OK] TARE:", tare_raw)

    # DÃ©marre boucle
    threading.Thread(target=update_control_loop, daemon=True).start()
    if uart:
        threading.Thread(target=uart_reader, daemon=True).start()

    # PrÃ©-lancer Chromium sur /hdmi aprÃ¨s un dÃ©lai (Flask doit Ãªtre up)
    def prelaunch_chromium():
        time.sleep(3.0)
        print("[HDMI] Pre-launching Chromium on /hdmi...")
        ensure_chromium()
        set_hdmi(mode="black")
        print("[HDMI] Chromium ready")
    threading.Thread(target=prelaunch_chromium, daemon=True).start()

    print(f"[OK] Web UI: http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, threaded=True)

if __name__ == "__main__":
    main()























































