# MÉMO PROJET — Plateforme SOT + WBB + ESP32 (Jan 2026)

## Objectif
- Faire un SOT 6 conditions (20 s chacune) avec une plateforme motorisée avant/arrière.
- Mesure COP 2D (X et Y) via Wii Balance Board (WBB).
- L’asservissement plateforme ne se fait que sur l’axe Y (avant/arrière) car 1 moteur.
- À la fin : génération automatique C1..C6.csv + summary.csv + PDF avec graphiques + synthèse sensorielle (SOM/VIS/VEST/DEP.V).

## Hardware
- Raspberry Pi (en Wi-Fi au routeur du cabinet).
- WBB (connexion Bluetooth, il faut appuyer sur le bouton rouge SYNC au lancement).
- ESP32 (commande moteur H-bridge + VL53L0X + 2 switches fin de course).
- Capteurs WBB : dimensions 43 cm (longueur) et 23.5 cm (largeur) entre capteurs.
- Moteur NIDEC 404.867 24VDC (peu important ici, réglages software OK).

## Logique SOT (conditions)
- C1 : plateforme fixe, yeux ouverts
- C2 : plateforme fixe, yeux fermés
- C3 : plateforme fixe, optocinétique
- C4 : plateforme asservie (AP), yeux ouverts
- C5 : plateforme asservie, yeux fermés
- C6 : plateforme asservie, optocinétique

Durée de chaque condition : 20 s.  
Entre conditions : pause manuelle (l’utilisateur déclenche la suivante).

## Dossiers / fichiers sur le Pi
### Sessions
- Dossier des sessions : `~/sessions/`
- Exemple session : `~/sessions/SOT_20260124_112417/`

Contenu typique :
- `C1.csv, C2.csv, C3.csv, C4.csv, C5.csv, C6.csv`
- `summary.csv`
- `SOT_report.pdf` (PDF généré)

### Script PDF
- Script : `~/make_sot_pdf.py`
- Il lit `summary.csv` + les `C1..C6.csv` et génère :
  - `SOT_report.pdf` dans le même dossier de session.

### Environnement Python (venv)
- Venv utilisé : `~/sotenv/`
- Exécuter python via : `~/sotenv/bin/python`

### Scripts “stream WBB → ESP”
- Script Pi : `send_wbb.py`
- Chemin : `~/send_wbb.py`

Rôle : lire WBB, calculer COP, filtrer, envoyer vers ESP via UART :  
envoi format ASCII : `COP:Y:<float>\n`

UART côté Pi : candidates (`/dev/serial0`, `/dev/ttyS0`, `/dev/ttyAMA0`)

Commandes envoyées au boot (si pas `--no_arm_auto`) :
- `ARM:1` puis `AUTO:1`

Commande utilisée (actuelle, stable) :
```
sudo ./send_wbb.py --invert --hz 120 --alpha 0.95 --dead 0.001 \
  --gain 6.5 --expo 0.60 --boost 0.28 --boost_thr 0.003 --boost_decay 0.55 \
  --min_total 12000 --print
```

Important :
- `--min_total 12000` a supprimé les mouvements de plateforme “à vide”.
- `--invert` gère le sens (avant/arrière inversé).
- `--alpha 0.95` = très réactif.
- `--dead 0.001` = deadband faible.
- `--gain, --expo, --boost, --boost_thr, --boost_decay` existent dans la version récente du script.

## ESP32 (code Arduino) — état actuel
Code “asservissement vitesse” : reçoit `COP:Y:x` (float) et calcule une commande PWM signée.

Système de sécurité : switches front/back + release + hardstop + slowzone.  
Homing VL53 + switches → calcule `centerMm`, `posMinMm`, `posMaxMm`, `frontIsMin`, safe range.

### Paramètres importants (ceux qu’on réglait à la main avant)
- `SET:SLEW:60`
- `SET:PMIN:30`
- `SET:KICKPWM:140`
- `SET:KICKMS:50`
- `SET:ENDMARGIN:12`
- `SET:SLOWZONE:8`
- `SET:SLOWPWM:90`
- `SET:HARDSTOP:2`

### Ajouts déjà intégrés dans l’ESP (dans ton code actuel)
- Commande `CENTER` / `CENTER:1` : recentre au centre (VL53 + `centerMm`)
- Commande `PARAMS?` : imprime tous les paramètres courants
- Commande `PROFILE:SOT` : applique le preset ci-dessus + save NVS
- Correction du bug `SET:SLOWZONE:` : index substring corrigé (utiliser `substring(13)`).

### Commandes série ESP acceptées
`ARM:1 / ARM:0`  
`HOME` (homing complet)  
`CENTER`  
`AUTO:1 / AUTO:0`  
`COP:Y:<float>` (ou `COP:<float>`)  
`STOP`, `STAT`, `REL`  
`CAL:CLR`  
`COPINV:0/1`  
`DBG:COP:0/1`  
`SET:SLEW:x`, `SET:PMIN:x`, `SET:KICKPWM:x`, `SET:KICKMS:x`,  
`SET:ENDMARGIN:x`, `SET:SLOWZONE:x`, `SET:SLOWPWM:x`, `SET:HARDSTOP:x`  
`PROFILE:SOT`  
`PARAMS?`

## Mesures / calculs côté rapport (PDF)
### Aire
Méthode choisie : Option A — Aire de l’ellipse de confiance (95%).  
On calcule l’ellipse sur COP 2D (X,Y).

### Vitesse
Mean speed mm/s (moyenne de la vitesse du COP).

### Synthèse sensorielle (basée sur le texte fourni)
On utilise les ratios “performance” (plus haut = meilleur) :
- SOM = C2 / C1
- VIS = C4 / C1
- VEST = C5 / C1
- Dépendance visuelle (DEP.V) = (C3 + C6) / (C2 + C5)

Important :
- Tu veux que DEP.V monte quand la dépendance visuelle est forte (donc ratio direct, pas inversé).

## Format des fichiers CSV (session)
### summary.csv
Exemple (déjà réel chez toi) :
```
code,label,platform_moves,area_cm2,mean_speed_mm_s,csv
C1,Plateforme fixe - Yeux ouverts,0,0.779,7.44,sessions/SOT_20260124_112417/C1.csv
...
```

Note : parfois les chemins dans csv sont relatifs/absolus, il faut que le script PDF gère les deux.

### C1.csv etc.
Contiennent la trace temporelle COP 2D (X,Y) (au moins `t_s`, `x_mm`, `y_mm` ou équivalent).  
On a déjà corrigé la lecture et obtenu les diagrammes (ellipse OK).

## Problèmes rencontrés et solutions
### PEP 668 (pip bloqué)
Erreur “externally-managed-environment” → solution : utiliser venv `~/sotenv`.

### Permission denied dans dossier session
Les fichiers sessions avaient été créés parfois en `root:root` (lancement via sudo), donc `sed/cp` échouaient.

Solution : ne pas mixer sudo / user pour écrire dans sessions, ou faire :
```
sudo chown -R pi:pi ~/sessions
```

### Unicode / PDF
Erreur unicode corrigée en ajoutant l’encodage (ex : `# -*- coding: utf-8 -*-`).  
Warning font “Glyph 146 missing” : non bloquant.

## Ce qui est “OK” aujourd’hui
- Exécution SOT : C1..C6 capturés.
- Fichiers générés : C1..C6.csv + summary.csv.
- Script PDF fonctionne et produit `SOT_report.pdf`.
- Il manquait au début la synthèse (SOM/VIS/VEST/DEP.V) → désormais OK dans la dernière version qui te convient.
- Plus de mouvements à vide grâce à `--min_total 12000`.

## Ce qu’on veut faire ensuite (prochaine étape)
Web UI ultra simple sur le Pi (pilotage à distance Wi-Fi)

Un serveur web Python (Flask) sur le Pi, accessible par navigateur.

Fonctions :
- “Nouvelle session”, patient
- “Start WBB streaming” (affiche “appuie sur bouton rouge WBB”)
- Boutons `ARM`, `HOME`, `CENTER`, `PROFILE:SOT`, `AUTO` on/off
- Lancer C1..C6 (20 s) avec pause manuelle entre conditions
- Générer PDF + bouton télécharger

Le web server remplacerait idéalement `send_wbb.py` (ou l’appelle en sous-process).

## Fichiers de référence à donner dans la nouvelle discussion
- `~/send_wbb.py`
- `~/make_sot_pdf.py`
- `~/sessions/SOT_20260124_112417/summary.csv`
- `~/sessions/SOT_20260124_112417/C1.csv ... C6.csv`
- venv `~/sotenv/`
- ESP32 code Arduino (celui que tu as collé, avec `PROFILE:SOT` + `PARAMS?` + `CENTER` + fix `SLOWZONE`)

## Commandes “workflow” actuelles (pour lancer un test)
Sur ESP via serial (ou automatisé) :
- `ARM:1`
- `HOME` (si besoin)
- `PROFILE:SOT`

Sur Pi :
```
sudo ./send_wbb.py --invert --hz 120 --alpha 0.95 --dead 0.001 \
  --gain 6.5 --expo 0.60 --boost 0.28 --boost_thr 0.003 --boost_decay 0.55 \
  --min_total 12000 --print
```

Après la session :
```
cd ~/sessions/SOT_YYYYMMDD_HHMMSS
~/sotenv/bin/python ~/make_sot_pdf.py summary.csv --title "SOT - Rapport" --patient "Nom Prénom"
```

## Ensuite: Ce qu’on va construire (MVP réaliste)
### 1) Gestion patients
- créer/éditer patient : nom, ID, date de naissance, taille, poids, notes
- auto-générer un dossier de session (timestamp)
- export CSV/JSON/PDF plus tard

### 2) Test SOT (6 conditions)
Pour chaque condition :
- durée (ex 20s), repos (ex 10s), nb essais (ex 3)
- consignes affichées

Enregistrement :
- 4 capteurs WBB, CoP_Y filtré, timestamp
- commandes envoyées à l’ESP (COP:Y, AUTO on/off, params)
- (optionnel) logs ESP si on câble retour UART

**Très important :** définir tes 6 conditions version “maison” (puisque tu as une plateforme active).  
On peut mapper comme ça (exemple) :
- stable sol / yeux ouverts (plateforme fixe)
- stable sol / yeux fermés
- surface instable (plateforme asservie) / yeux ouverts
- surface instable (plateforme asservie) / yeux fermés
- “visuel perturbé” (si tu as un casque/écran) ou variante
- instable + perturbation visuelle (option)

### 3) Exercices (modes)
- Sinusoïde : amplitude, période, durée
- Aléatoire : bruit filtré (type “pink noise”), amplitude max, bande passante
- Paliers (step) : perturbations brusques contrôlées
- Asservissement WBB (mode que tu as) : paramètres (gain/expo/boost/hz)

## Code actuel ESP32 (référence)
```
<code collé dans la conversation précédente>
```
