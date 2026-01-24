#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, redirect, render_template, request, url_for
import serial


@dataclass
class AppConfig:
    home_dir: Path
    sessions_dir: Path
    send_wbb_path: Path
    make_pdf_path: Path
    python_path: Path
    serial_port: str
    serial_baud: int


@dataclass
class AppState:
    active_session: Optional[Path] = None
    wbb_process: Optional[subprocess.Popen] = None


DEFAULT_WBB_ARGS = [
    "--invert",
    "--hz",
    "120",
    "--alpha",
    "0.95",
    "--dead",
    "0.001",
    "--gain",
    "6.5",
    "--expo",
    "0.60",
    "--boost",
    "0.28",
    "--boost_thr",
    "0.003",
    "--boost_decay",
    "0.55",
    "--min_total",
    "12000",
    "--print",
]


def build_config() -> AppConfig:
    home_dir = Path(os.environ.get("SOT_HOME", str(Path.home()))).expanduser()
    sessions_dir = Path(os.environ.get("SOT_SESSIONS", str(home_dir / "sessions")))
    send_wbb_path = Path(os.environ.get("SOT_SEND_WBB", str(home_dir / "send_wbb.py")))
    make_pdf_path = Path(os.environ.get("SOT_MAKE_PDF", str(home_dir / "make_sot_pdf.py")))
    python_path = Path(os.environ.get("SOT_PYTHON", str(home_dir / "sotenv" / "bin" / "python")))
    serial_port = os.environ.get("SOT_SERIAL_PORT", "/dev/serial0")
    serial_baud = int(os.environ.get("SOT_SERIAL_BAUD", "115200"))
    return AppConfig(
        home_dir=home_dir,
        sessions_dir=sessions_dir,
        send_wbb_path=send_wbb_path,
        make_pdf_path=make_pdf_path,
        python_path=python_path,
        serial_port=serial_port,
        serial_baud=serial_baud,
    )


def ensure_sessions_dir(config: AppConfig) -> None:
    config.sessions_dir.mkdir(parents=True, exist_ok=True)


def create_session_folder(config: AppConfig, patient_data: dict) -> Path:
    ensure_sessions_dir(config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = config.sessions_dir / f"SOT_{timestamp}"
    session_path.mkdir(parents=True, exist_ok=True)
    (session_path / "patient.json").write_text(
        json.dumps(patient_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return session_path


def list_sessions(config: AppConfig) -> list[Path]:
    if not config.sessions_dir.exists():
        return []
    return sorted(
        [p for p in config.sessions_dir.iterdir() if p.is_dir()],
        reverse=True,
    )


def load_patient(session_path: Path) -> dict:
    patient_file = session_path / "patient.json"
    if not patient_file.exists():
        return {}
    return json.loads(patient_file.read_text(encoding="utf-8"))


def serial_send(config: AppConfig, command: str) -> None:
    with serial.Serial(config.serial_port, config.serial_baud, timeout=1) as port:
        port.write((command.strip() + "\n").encode("utf-8"))


def start_wbb_stream(config: AppConfig, state: AppState, args: list[str]) -> None:
    if state.wbb_process and state.wbb_process.poll() is None:
        return
    cmd = [str(config.python_path), str(config.send_wbb_path)] + args
    state.wbb_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def stop_wbb_stream(state: AppState) -> None:
    if not state.wbb_process:
        return
    if state.wbb_process.poll() is None:
        state.wbb_process.send_signal(signal.SIGTERM)
        state.wbb_process.wait(timeout=5)
    state.wbb_process = None


def create_app() -> Flask:
    app = Flask(__name__)
    config = build_config()
    state = AppState()

    @app.context_processor
    def inject_globals():
        return {
            "config": config,
            "state": state,
            "default_wbb_args": " ".join(DEFAULT_WBB_ARGS),
        }

    @app.get("/")
    def index():
        sessions = list_sessions(config)
        active_session = state.active_session
        patient = load_patient(active_session) if active_session else {}
        return render_template(
            "index.html",
            sessions=sessions,
            active_session=active_session,
            patient=patient,
        )

    @app.post("/session/new")
    def new_session():
        patient = {
            "name": request.form.get("name", "").strip(),
            "patient_id": request.form.get("patient_id", "").strip(),
            "dob": request.form.get("dob", "").strip(),
            "height_cm": request.form.get("height_cm", "").strip(),
            "weight_kg": request.form.get("weight_kg", "").strip(),
            "notes": request.form.get("notes", "").strip(),
        }
        state.active_session = create_session_folder(config, patient)
        return redirect(url_for("index"))

    @app.post("/session/select")
    def select_session():
        session_path = request.form.get("session_path", "")
        if session_path:
            state.active_session = Path(session_path)
        return redirect(url_for("index"))

    @app.post("/esp/command")
    def esp_command():
        command = request.form.get("command", "").strip()
        if command:
            serial_send(config, command)
        return redirect(url_for("index"))

    @app.post("/wbb/start")
    def wbb_start():
        raw_args = request.form.get("wbb_args", "").strip()
        args = raw_args.split() if raw_args else DEFAULT_WBB_ARGS
        start_wbb_stream(config, state, args)
        return redirect(url_for("index"))

    @app.post("/wbb/stop")
    def wbb_stop():
        stop_wbb_stream(state)
        return redirect(url_for("index"))

    @app.post("/pdf/generate")
    def generate_pdf():
        if not state.active_session:
            return redirect(url_for("index"))
        summary = state.active_session / "summary.csv"
        if not summary.exists():
            return redirect(url_for("index"))
        cmd = [
            str(config.python_path),
            str(config.make_pdf_path),
            str(summary),
            "--title",
            "SOT - Rapport",
        ]
        patient = load_patient(state.active_session)
        if patient.get("name"):
            cmd += ["--patient", patient["name"]]
        subprocess.run(cmd, check=False)
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
