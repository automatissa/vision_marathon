"""Lecture des numéros de dossard par caméra USB.

Partie redondante du chronométrage RFID : la caméra lit les numéros de
dossard et les envoie à app.py, qui compte les tours avec les mêmes règles
que le RFID. Un seul tour est enregistré si les deux systèmes voient le
même passage.

Ce module ne connaît pas la base de données : il appelle simplement
on_bib(numero, instant) pour chaque lecture confirmée.
"""
import re
import threading
import time

import cv2

CAMERA_INDEX = 0          # /dev/video0 (première caméra USB)
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
OCR_WIDTH = 960           # l'image est réduite à cette largeur avant OCR (plus rapide)
ROI = None                # zone analysée (x, y, largeur, hauteur) ; None = image entière
OCR_MIN_SCORE = 0.70      # confiance minimale de l'OCR (0 à 1)
BIB_MIN_DIGITS = 1
BIB_MAX_DIGITS = 5        # dossards de 1 à 99999
CONFIRM_READS = 2         # lectures identiques nécessaires avant de compter
CONFIRM_WINDOW = 3.0      # ... dans cette fenêtre (secondes)

_DIGITS = re.compile(rf'\d{{{BIB_MIN_DIGITS},{BIB_MAX_DIGITS}}}')


def normalize_bib(value):
    """'0042', ' 42 ' -> '42'. Retourne None si ce n'est pas un dossard valide."""
    text = str(value or '').replace(' ', '').strip()
    if not _DIGITS.fullmatch(text):
        return None
    return str(int(text))


def bibs_from_ocr(result, min_score=OCR_MIN_SCORE):
    """Extrait les numéros de dossard d'un résultat RapidOCR."""
    bibs = set()
    for item in result or []:
        _box, text, score = item[0], item[1], float(item[2])
        if score < min_score:
            continue
        bib = normalize_bib(text)
        if bib is not None:
            bibs.add(bib)
    return bibs


class BibConfirmer:
    """Évite les fausses lectures : un numéro doit être lu CONFIRM_READS fois
    dans CONFIRM_WINDOW secondes avant d'être transmis.

    Le premier signalement d'un passage utilise l'instant de la PREMIÈRE
    lecture, pour que le temps du tour soit le plus juste possible."""

    def __init__(self, reads=CONFIRM_READS, window=CONFIRM_WINDOW):
        self.reads = reads
        self.window = window
        self.seen = {}          # dossard -> liste des instants récents
        self.confirmed = {}     # dossard -> instant de la dernière lecture confirmée

    def add(self, bib, now):
        """Retourne l'instant à signaler, ou None si pas encore confirmé."""
        times = [t for t in self.seen.get(bib, []) if now - t <= self.window]
        times.append(now)
        self.seen[bib] = times
        last = self.confirmed.get(bib)
        if last is not None and now - last <= self.window:
            self.confirmed[bib] = now
            return now                      # passage déjà confirmé : on continue de signaler
        if len(times) >= self.reads:
            self.confirmed[bib] = now
            return times[0]                 # nouveau passage : instant de la 1re lecture
        return None


class Camera:
    """Lit la caméra en continu et garde seulement l'image la plus récente,
    pour que l'OCR travaille toujours sur ce qui se passe maintenant."""

    def __init__(self, on_status):
        self.on_status = on_status
        self.cond = threading.Condition()
        self.frame = None
        self.frame_time = 0.0

    def run(self):
        while True:
            cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                cap.release()
                self.on_status(f'Déconnectée : caméra {CAMERA_INDEX} introuvable')
                time.sleep(2)
                continue
            self.on_status('Connectée')
            while True:
                ok, frame = cap.read()
                now = time.monotonic()
                if not ok:
                    self.on_status('Déconnectée : plus d’image')
                    break
                with self.cond:
                    self.frame, self.frame_time = frame, now
                    self.cond.notify()
            cap.release()
            time.sleep(2)

    def next_frame(self, after):
        """Attend une image plus récente que `after`."""
        with self.cond:
            self.cond.wait_for(lambda: self.frame_time > after, timeout=1.0)
            return self.frame, self.frame_time


def prepare(frame):
    if ROI is not None:
        x, y, w, h = ROI
        frame = frame[y:y + h, x:x + w]
    height, width = frame.shape[:2]
    if width > OCR_WIDTH:
        frame = cv2.resize(frame, (OCR_WIDTH, int(height * OCR_WIDTH / width)))
    return frame


def ocr_worker(camera, on_bib, on_status):
    try:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()
    except Exception as exc:                # dépendance absente : le RFID continue seul
        on_status(f'OCR indisponible : {exc}')
        return
    confirmer = BibConfirmer()
    last_time = 0.0
    while True:
        frame, frame_time = camera.next_frame(last_time)
        if frame is None or frame_time <= last_time:
            continue
        last_time = frame_time
        try:
            result, _ = engine(prepare(frame))
        except Exception as exc:
            on_status(f'Erreur OCR : {exc}')
            continue
        for bib in bibs_from_ocr(result):
            instant = confirmer.add(bib, frame_time)
            if instant is not None:
                on_bib(bib, instant)


def start(on_bib, on_status=lambda s: None):
    """Lance la caméra et l'OCR en arrière-plan.

    on_bib(dossard: str, instant: float)  instant = time.monotonic() de l'image
    on_status(texte: str)                 état de la caméra pour l'interface
    """
    camera = Camera(on_status)
    threading.Thread(target=camera.run, daemon=True, name='camera').start()
    threading.Thread(target=ocr_worker, args=(camera, on_bib, on_status),
                     daemon=True, name='ocr').start()
