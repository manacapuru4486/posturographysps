# SOT Control Web UI

Interface web minimaliste pour piloter une session SOT avec une Wii Balance Board et un ESP32.

## Lancer l'application

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Ouvrir ensuite `http://localhost:5000`.

## Configuration

Les chemins peuvent être personnalisés avec des variables d'environnement :

- `SOT_HOME` (par défaut `~`)
- `SOT_SESSIONS` (par défaut `~/sessions`)
- `SOT_SEND_WBB` (par défaut `~/send_wbb.py`)
- `SOT_MAKE_PDF` (par défaut `~/make_sot_pdf.py`)
- `SOT_PYTHON` (par défaut `~/sotenv/bin/python`)
- `SOT_SERIAL_PORT` (par défaut `/dev/serial0`)
- `SOT_SERIAL_BAUD` (par défaut `115200`)

## Remarques

- La génération PDF utilise `summary.csv` dans le dossier de session actif.
- Les boutons de condition SOT déclenchent uniquement un timer côté navigateur.
