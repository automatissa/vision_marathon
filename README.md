# vision_marathon

Chronométrage de courses (coureurs et cyclistes) sur Raspberry Pi 5, avec deux systèmes redondants :

- **RFID** (`app.py`) : un lecteur UHF sur `/dev/ttyUSB0` lit le badge de chaque participant.
- **Caméra** (`vision.py`) : une caméra USB lit le numéro de dossard.

Les deux alimentent **un seul comptage**. Si les deux voient le même passage (moins de 5 s d'écart), il ne compte qu'une fois. Si l'un des deux rate un passage, l'autre le compte quand même. Chaque passage garde sa source dans la base : `rfid`, `camera` ou `both`.

## Installation sur le Raspberry Pi

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Puis ouvrir http://127.0.0.1:5000 (course) et http://127.0.0.1:5000/inscription (inscriptions).

## Inscription

Pour chaque participant : scanner son badge, saisir son nom **et son numéro de dossard**. Sans dossard, la caméra ne peut pas le compter (le RFID fonctionne toujours).

## Réglages caméra (en haut de `vision.py`)

| Réglage | Rôle |
|---|---|
| `CAMERA_INDEX` | numéro de la caméra (`0` = `/dev/video0`) |
| `ROI` | zone de l'image à analyser `(x, y, largeur, hauteur)`. La limiter au passage des dossards rend l'OCR plus rapide et plus fiable. |
| `OCR_WIDTH` | largeur de l'image envoyée à l'OCR. Plus petit = plus rapide, mais les petits dossards deviennent illisibles. |
| `CONFIRM_READS` / `CONFIRM_WINDOW` | nombre de lectures identiques nécessaires avant de compter. Pour des cyclistes rapides, passer `CONFIRM_READS` à `1`. |
| `OCR_MIN_SCORE` | confiance minimale de l'OCR |

Conseils : placer la caméra pour que le dossard fasse au moins 80 pixels de haut dans l'image, avec un bon éclairage, et un passage étroit (un ou deux participants de front).

L'état de la caméra et les 10 derniers dossards lus sont dans `/api/status` (`camera`, `camera_recent`), pratique pour régler la caméra avant le départ.
