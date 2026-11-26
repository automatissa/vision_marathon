import time
import threading
import sqlite3
import secrets
from collections import deque
from datetime import datetime
from pathlib import Path

import serial
from flask import Flask, jsonify, render_template, render_template_string, request

import vision
from vision import normalize_bib

PORT = '/dev/ttyUSB0'
BAUDRATE = 115200
COMMAND = bytes.fromhex('AA 00 22 00 00 22 DD')
MIN_LAP_SECONDS = 10.0  # Durée minimale entre deux tours après le premier
ABSENCE_SECONDS = 2.0   # Absence nécessaire avant un nouveau passage
POLL_SECONDS = 0.10  # Interrogation toutes les 100 ms (au lieu de 250 ms)
MERGE_SECONDS = 5.0  # RFID et caméra à moins de 5 s d'écart = même passage
DB_PATH = Path(__file__).with_name('competition.db')

app = Flask(__name__)
lock = threading.RLock()
# last_seen et armed sont indexés par (source, epc) : chaque système (RFID,
# caméra) a sa propre détection d'absence. last_lap et laps sont communs :
# un passage vu par les deux systèmes ne compte qu'une fois.
state = {'running': False, 'started_wall': None, 'started_mono': None,
         'stopped_elapsed': 0.0, 'last_seen': {}, 'armed': {}, 'last_lap': {},
         'laps': {}, 'reader': 'Initialisation', 'unknown': {},
         'scan': None, 'known_epcs': set(),
         'bib_to_epc': {}, 'last_passage': {}, 'camera': 'Initialisation',
         'camera_recent': deque(maxlen=10)}

_schema_ready = False


def connect_db():
    global _schema_ready
    db = sqlite3.connect(DB_PATH)
    if _schema_ready:
        return db
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS participants (epc TEXT PRIMARY KEY, name TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS passages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, epc TEXT NOT NULL,
        lap INTEGER NOT NULL, lap_seconds REAL NOT NULL,
        elapsed_seconds REAL NOT NULL, timestamp TEXT NOT NULL)''')
    # Ajouts pour la caméra (sans casser une base existante).
    if 'bib' not in {r[1] for r in db.execute('PRAGMA table_info(participants)')}:
        db.execute('ALTER TABLE participants ADD COLUMN bib TEXT')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_bib ON participants(bib)')
    if 'source' not in {r[1] for r in db.execute('PRAGMA table_info(passages)')}:
        db.execute("ALTER TABLE passages ADD COLUMN source TEXT NOT NULL DEFAULT 'rfid'")
    db.commit()
    _schema_ready = True
    return db


def decode_frames(buf):
    frames = []
    while True:
        start = buf.find(b'\xAA')
        if start < 0:
            buf.clear()
            return frames
        del buf[:start]
        if len(buf) < 7:
            return frames
        n = int.from_bytes(buf[3:5], 'big')
        if n > 512:
            del buf[0]
            continue
        size = n + 7
        if len(buf) < size:
            return frames
        frame = bytes(buf[:size])
        if frame[-1] != 0xDD or (sum(frame[1:-2]) & 0xFF) != frame[-2]:
            del buf[0]
            continue
        del buf[:size]
        frames.append(frame)


def epc_from_frame(frame):
    if frame[1] != 0x02 or frame[2] != 0x22:
        return None
    payload = frame[5:-2]
    if len(payload) < 5:
        return None
    pc = int.from_bytes(payload[1:3], 'big')
    length = ((pc >> 11) & 0x1F) * 2
    if length < 2 or len(payload) < 3 + length:
        return None
    return payload[3:3+length].hex().upper()


def process_epc(epc, now):
    # Tous les badges sont traités indépendamment : un nouveau badge
    # peut être compté dès son premier passage, même juste après un autre.
    with lock:
        scan = state['scan']
        if scan is not None and not state['running']:
            if now <= scan['deadline']:
                scan['epcs'].add(epc)
                if 'first_seen' not in scan:
                    scan['first_seen'] = now
            return
        if not state['running']:
            return

        # Chargés une seule fois au départ : évite une requête SQLite
        # à chaque lecture d'un badge (plusieurs lectures par seconde).
        if epc not in state['known_epcs']:
            state['unknown'][epc] = datetime.now().isoformat(timespec='seconds')
            return

        record_detection(epc, 'rfid', now)


def process_bib(bib, now):
    """Appelé par vision.py pour chaque dossard lu par la caméra."""
    with lock:
        epc = state['bib_to_epc'].get(bib)
        state['camera_recent'].append({
            'bib': bib, 'known': epc is not None,
            'time': datetime.now().isoformat(timespec='seconds')})
        if epc is None or not state['running']:
            return
        record_detection(epc, 'camera', now)


def set_camera_status(text):
    with lock:
        state['camera'] = text


def record_detection(epc, source, now):
    """Compte un tour pour un participant, que la détection vienne du RFID
    ('rfid') ou de la caméra ('camera'). Les mêmes règles s'appliquent aux
    deux, et un passage vu par les deux systèmes n'est compté qu'une fois."""
    with lock:
        if not state['running'] or now < state['started_mono']:
            return
        key = (source, epc)
        previous_seen = state['last_seen'].get(key)
        if previous_seen is not None and now - previous_seen >= ABSENCE_SECONDS:
            state['armed'][key] = True
        state['last_seen'][key] = now
        if not state['armed'].get(key, True):
            return

        last_lap = state['last_lap'].get(epc)
        if last_lap is None:
            # Premier passage accepté immédiatement.
            lap_seconds = now - state['started_mono']
        else:
            # Délai minimum uniquement pour CE participant, pas les autres.
            lap_seconds = now - last_lap
            if lap_seconds < MIN_LAP_SECONDS:
                mark_seen_by_both(epc, source, now)
                return

        elapsed = now - state['started_mono']
        lap = state['laps'].get(epc, 0) + 1
        db = connect_db()
        try:
            cur = db.execute(
                'INSERT INTO passages (epc, lap, lap_seconds, elapsed_seconds, timestamp, source) VALUES (?,?,?,?,?,?)',
                (epc, lap, lap_seconds, elapsed,
                 datetime.now().isoformat(timespec='seconds'), source)
            )
            db.commit()
        finally:
            db.close()
        state['laps'][epc] = lap
        state['last_lap'][epc] = now
        state['last_passage'][epc] = {'id': cur.lastrowid, 'source': source}
        state['armed'][key] = False
        print(f'TOUR {lap}: {epc} ({source}) - {lap_seconds:.2f} s', flush=True)


def mark_seen_by_both(epc, source, now):
    """Le passage déjà compté par un système est confirmé par l'autre."""
    passage = state['last_passage'].get(epc)
    if (passage is None or passage['source'] in (source, 'both')
            or abs(now - state['last_lap'][epc]) > MERGE_SECONDS):
        return
    db = connect_db()
    try:
        db.execute("UPDATE passages SET source='both' WHERE id=?", (passage['id'],))
        db.commit()
    finally:
        db.close()
    passage['source'] = 'both'


def update_badge_absence():
    now = time.monotonic()
    with lock:
        if not state['running']:
            return
        for key, last_seen in list(state['last_seen'].items()):
            if now - last_seen >= ABSENCE_SECONDS:
                state['armed'][key] = True


def reader_worker():
    while True:
        try:
            with serial.Serial(PORT, BAUDRATE, timeout=0.02) as reader:
                with lock:
                    state['reader'] = 'Connecté'
                buf = bytearray()
                while True:
                    update_badge_absence()
                    reader.write(COMMAND)
                    reader.flush()
                    until = time.monotonic() + POLL_SECONDS
                    while time.monotonic() < until:
                        chunk = reader.read(reader.in_waiting or 1)
                        if chunk:
                            buf.extend(chunk)
                            for frame in decode_frames(buf):
                                epc = epc_from_frame(frame)
                                if epc:
                                    process_epc(epc, time.monotonic())
        except (serial.SerialException, OSError) as exc:
            with lock:
                state['reader'] = f'Déconnecté : {exc}'
            time.sleep(2)


REGISTRATION_HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inscription RFID</title>
<style>
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#172337;background:#f4f6fc}
*{box-sizing:border-box}
body{margin:0;padding:24px}
main{max-width:1080px;margin:0 auto}
a{color:#185abc}
h1{margin:18px 0 22px}
.panel{background:#fff;border-radius:14px;padding:24px;margin:18px 0;box-shadow:0 3px 14px #17233712}
.panel h2{margin-top:0}
.muted{color:#607085}
button{background:#185abc;color:#fff;border:0;border-radius:8px;padding:11px 17px;cursor:pointer;font:inherit}
button:disabled{opacity:.46;cursor:not-allowed}
button.delete{background:#b42318}
button + button{margin-left:9px}
input[type=text]{padding:11px;border:1px solid #cbd5e1;border-radius:8px;max-width:100%;width:380px;font:inherit}
label[for=name],label[for=bib]{display:block;margin:16px 0 7px;font-weight:600}
.bib-cell{font-weight:650;font-variant-numeric:tabular-nums}
#message{white-space:pre-wrap;margin-top:12px}
#message.error{color:#b42318}
#message.success{color:#087443}
#badge-info{font-size:.93rem;overflow-wrap:anywhere}
.table-wrapper{width:100%;overflow-x:auto}
table{width:100%;border-collapse:collapse;margin:14px 0;min-width:630px}
th,td{text-align:left;padding:14px 12px;border-bottom:1px solid #e4eaf1}
th{background:#edf3ff;font-size:.92rem}
tr:nth-child(even) td{background:#fafbfd}
tbody tr{cursor:pointer}
tbody tr:hover td{background:#eff5ff}
tbody tr.selected td{background:#dceaff}
.name-cell{font-weight:650}
.epc-cell{font-family:ui-monospace,Consolas,monospace;font-size:.86rem;overflow-wrap:anywhere}
.pick-cell{width:60px;text-align:center}
.pick-cell input{width:18px;height:18px;accent-color:#185abc;cursor:pointer}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}
.actions button + button{margin-left:0}
#selected-label{font-size:.9rem;margin-top:12px}
@media(max-width:640px){body{padding:12px}.panel{padding:16px}h1{font-size:1.55rem}}
</style>
</head>
<body>
<main>
<a href="/">← Retour au chronométrage</a>
<h1>Inscription des participants RFID</h1>
<section class="panel">
  <h2>Scanner un badge</h2>
  <p class="muted">Arrête la course et éloigne les autres badges de l'antenne. Clique sur « Scanner un badge », puis présente un seul badge.</p>
  <button id="scan" type="button" onclick="startScan()">Scanner un badge</button>
  <p id="message" role="status">En attente.</p>
  <p id="badge-info" class="muted"></p>
  <label for="name">Nom du participant</label>
  <input id="name" type="text" placeholder="Nom et prénom" maxlength="120" autocomplete="off">
  <label for="bib">Numéro de dossard (lu par la caméra)</label>
  <input id="bib" type="text" inputmode="numeric" pattern="[0-9]*" placeholder="Ex. 1042" maxlength="5" autocomplete="off">
  <p><button id="save" type="button" disabled onclick="save()">Enregistrer le participant</button></p>
</section>
<section class="panel">
  <h2>Participants inscrits</h2>
  <p class="muted">Sélectionne une seule ligne du tableau, puis utilise le bouton Modifier ou Supprimer ci-dessous.</p>
  <div class="table-wrapper">
    <table aria-label="Participants inscrits">
      <thead><tr><th class="pick-cell">Choix</th><th>Dossard</th><th>Nom du participant</th><th>EPC du badge</th></tr></thead>
      <tbody id="people"></tbody>
    </table>
  </div>
  <div id="selected-label" class="muted">Aucun participant sélectionné.</div>
  <div class="actions">
    <button id="edit-selected" type="button" disabled onclick="renameSelected()">Modifier</button>
    <button id="delete-selected" class="delete" type="button" disabled onclick="deleteSelected()">Supprimer</button>
  </div>
</section>
</main>
<script>
let token=null, timer=null, selectedEpc=null, participants=[], courseRunning=false;
const msg=document.getElementById('message');
const scanBtn=document.getElementById('scan');
const saveBtn=document.getElementById('save');
const tableBody=document.getElementById('people');
const editBtn=document.getElementById('edit-selected');
const deleteBtn=document.getElementById('delete-selected');
const selectedLabel=document.getElementById('selected-label');
function setMessage(text,isError=false){msg.textContent=text;msg.className=isError?'error':'success'}
async function api(path,options){
  const r=await fetch(path,options);
  const j=await r.json();
  if(!r.ok)throw Error(j.error||'Erreur');
  return j;
}
function selectedParticipant(){return participants.find(p=>p.epc===selectedEpc)||null}
function updateSelection(){
  const p=selectedParticipant();
  editBtn.disabled=!p||courseRunning;
  deleteBtn.disabled=!p||courseRunning;
  selectedLabel.textContent=p?'Participant sélectionné : '+p.name+(courseRunning?' — arrête la course pour modifier.':''):
                              'Aucun participant sélectionné.';
  for(const row of tableBody.querySelectorAll('tr[data-epc]')){
    const active=row.dataset.epc===selectedEpc;
    row.classList.toggle('selected',active);
    const radio=row.querySelector('input[type=radio]');
    if(radio)radio.checked=active;
  }
}
function selectParticipant(epc){selectedEpc=epc;updateSelection()}
async function refresh(){
  try{
    const j=await api('/api/status');
    participants=j.participants;
    courseRunning=j.running;
    if(!selectedParticipant())selectedEpc=null;
    tableBody.replaceChildren();
    if(!participants.length){
      const tr=document.createElement('tr'),td=document.createElement('td');
      td.colSpan=4;
      td.textContent='Aucun participant inscrit.';
      tr.append(td);tableBody.append(tr);
    }
    participants.forEach(p=>{
      const tr=document.createElement('tr');
      tr.dataset.epc=p.epc;
      tr.tabIndex=0;
      tr.addEventListener('click',()=>selectParticipant(p.epc));
      tr.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectParticipant(p.epc)}});
      const choose=document.createElement('td');choose.className='pick-cell';
      const radio=document.createElement('input');radio.type='radio';radio.name='participant';
      radio.setAttribute('aria-label','Sélectionner '+p.name);
      radio.checked=p.epc===selectedEpc;
      choose.append(radio);
      const bib=document.createElement('td');bib.className='bib-cell';bib.textContent=p.bib||'—';
      const name=document.createElement('td');name.className='name-cell';name.textContent=p.name;
      const epc=document.createElement('td');epc.className='epc-cell';epc.textContent=p.epc;
      tr.append(choose,bib,name,epc);tableBody.append(tr);
    });
    updateSelection();
  }catch(e){setMessage('Impossible de charger les participants : '+e.message,true)}
}
async function renameSelected(){
  const p=selectedParticipant();
  if(!p)return;
  const name=prompt('Nouveau nom du participant :',p.name);
  if(name===null)return;
  const cleaned=name.trim();
  if(!cleaned||cleaned.length>120){alert('Nom invalide.');return}
  const bib=prompt('Numéro de dossard (vide = aucun) :',p.bib||'');
  if(bib===null)return;
  try{
    await api('/api/participants/rename',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({epc:p.epc,name:cleaned,bib:bib.trim()})
    });
    setMessage('Participant modifié avec succès.');
    await refresh();
  }catch(e){alert(e.message)}
}
async function deleteSelected(){
  const p=selectedParticipant();
  if(!p||!confirm('Supprimer le participant '+p.name+' ?'))return;
  try{
    await api('/api/participants/delete',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({epc:p.epc})
    });
    selectedEpc=null;
    setMessage('Participant supprimé.');
    await refresh();
  }catch(e){alert(e.message)}
}
async function startScan(){
  clearInterval(timer);timer=null;token=null;
  saveBtn.disabled=true;
  document.getElementById('badge-info').textContent='';
  scanBtn.disabled=true;
  try{
    const j=await api('/api/scan/start',{method:'POST'});
    token=j.token;
    setMessage('Recherche en cours (15 secondes)…');
    timer=setInterval(poll,500);
  }catch(e){setMessage(e.message,true);scanBtn.disabled=false}
}
async function poll(){
  if(!token)return;
  try{
    const j=await api('/api/scan/status?token='+encodeURIComponent(token));
    if(j.status==='waiting')return;
    clearInterval(timer);timer=null;scanBtn.disabled=false;
    if(j.status==='ready'){
      document.getElementById('badge-info').textContent='Badge : '+j.epc;
      setMessage('Badge détecté ! Saisis son nom puis enregistre.');
      saveBtn.disabled=false;
    }else{
      token=null;
      setMessage(j.status==='multiple'?'Plusieurs badges détectés. Éloigne les autres badges puis recommence.':'Aucun badge détecté. Réessaie.',true);
    }
  }catch(e){clearInterval(timer);timer=null;scanBtn.disabled=false;setMessage(e.message,true)}
}
async function save(){
  const name=document.getElementById('name').value.trim();
  const bib=document.getElementById('bib').value.trim();
  if(!name){setMessage('Saisis un nom.',true);return}
  if(!token){setMessage('Scanne d’abord un badge.',true);return}
  saveBtn.disabled=true;
  try{
    await api('/api/scan/confirm',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,name,bib})
    });
    setMessage('Participant enregistré !');
    document.getElementById('badge-info').textContent='';
    document.getElementById('name').value='';
    document.getElementById('bib').value='';
    token=null;
    await refresh();
  }catch(e){setMessage(e.message,true);saveBtn.disabled=false}
}
refresh();
</script>
</body></html>"""

@app.get('/inscription')
def registration_page():
    return render_template_string(REGISTRATION_HTML)

@app.post('/api/scan/start')
def scan_start():
    with lock:
        if state['running']:
            return jsonify(error='Arrête la course avant les inscriptions.'), 409
        token = secrets.token_urlsafe(24)
        state['scan'] = {'token': token, 'deadline': time.monotonic()+15,
                         'epcs': set(), 'finished': False}
        return jsonify(token=token)

@app.get('/api/scan/status')
def scan_status():
    with lock:
        scan = state['scan']
        if scan is None or request.args.get('token') != scan['token']:
            return jsonify(error='Scan expiré ou remplacé.'), 404
        if state['running']:
            return jsonify(error='Course en cours.'), 409
        # Répondre rapidement après la première lecture.
        now = time.monotonic()
        first = scan.get('first_seen')
        if first is None and now < scan['deadline']:
            return jsonify(status='waiting')
        if first is not None and now < min(first + 0.8, scan['deadline']):
            return jsonify(status='waiting')
        if len(scan['epcs']) == 0:
            return jsonify(status='empty')
        if len(scan['epcs']) > 1:
            return jsonify(status='multiple')
        return jsonify(status='ready', epc=next(iter(scan['epcs'])))

def parse_bib(data):
    """Retourne (dossard ou None, erreur ou None). Un champ vide = pas de dossard."""
    raw = str(data.get('bib') or '').strip()
    if not raw:
        return None, None
    bib = normalize_bib(raw)
    if bib is None:
        return None, 'Numéro de dossard invalide (chiffres uniquement, 5 au maximum).'
    return bib, None


BIB_TAKEN = 'Ce numéro de dossard est déjà utilisé.'


@app.post('/api/scan/confirm')
def scan_confirm():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    bib, bib_error = parse_bib(data)
    if bib_error:
        return jsonify(error=bib_error), 400
    with lock:
        scan = state['scan']
        if state['running']:
            return jsonify(error='Course en cours.'), 409
        if (not scan or scan['token'] != data.get('token') or
                ('first_seen' not in scan or
                 time.monotonic() < min(scan['first_seen'] + 0.8,
                                        scan['deadline'])) or
                len(scan['epcs']) != 1):
            return jsonify(error='Scan invalide : recommence.'), 409
        if not name or len(name) > 120:
            return jsonify(error='Nom invalide.'), 400
        epc = next(iter(scan['epcs']))
        db = connect_db()
        try:
            if db.execute('SELECT 1 FROM participants WHERE epc=?', (epc,)).fetchone():
                return jsonify(error='Ce badge est déjà inscrit.'), 409
            db.execute('INSERT INTO participants(epc,name,bib) VALUES (?,?,?)', (epc, name, bib))
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify(error=BIB_TAKEN), 409
        finally:
            db.close()
        state['scan'] = None
    return jsonify(ok=True, epc=epc)

@app.get('/')
def home():
    return render_template('index.html')


@app.get('/api/status')
def status():
    with lock:
        db = connect_db()
        try:
            people = db.execute('SELECT epc,name,bib FROM participants ORDER BY name').fetchall()
            passages = db.execute('SELECT epc,lap,lap_seconds,elapsed_seconds,timestamp,source FROM passages ORDER BY id').fetchall()
        finally:
            db.close()
        rows = []
        for epc, name, bib in people:
            ps = [p for p in passages if p[0] == epc]
            rows.append({'epc': epc, 'name': name, 'bib': bib, 'laps': len(ps),
                         'last_lap': ps[-1][2] if ps else None,
                         'best_lap': min((p[2] for p in ps), default=None),
                         'last_elapsed': ps[-1][3] if ps else None,
                         'last_source': ps[-1][5] if ps else None,
                         'sources': {s: sum(1 for p in ps if p[5] == s)
                                     for s in ('rfid', 'camera', 'both')}})
        rows.sort(key=lambda r: (-r['laps'], r['last_elapsed'] if r['last_elapsed'] is not None else float('inf')))
        elapsed = (time.monotonic() - state['started_mono']) if state['running'] else state['stopped_elapsed']
        return jsonify(running=state['running'], elapsed=elapsed, reader=state['reader'],
                       camera=state['camera'], camera_recent=list(state['camera_recent']),
                       participants=rows, unknown=list(state['unknown'].keys())[-10:])


@app.post('/api/participants')
def add_participant():
    data = request.get_json(silent=True) or {}
    epc = str(data.get('epc', '')).strip().upper().replace(' ', '')
    name = str(data.get('name', '')).strip()
    if not name or not epc or len(epc) % 2 or any(c not in '0123456789ABCDEF' for c in epc):
        return jsonify(error='Nom ou EPC invalide'), 400
    bib, bib_error = parse_bib(data)
    if bib_error:
        return jsonify(error=bib_error), 400
    with lock:
        if state['running']:
            return jsonify(error='Arrêter la course avant de modifier les participants'), 409
        db = connect_db()
        try:
            db.execute('INSERT INTO participants(epc,name,bib) VALUES (?,?,?) '
                       'ON CONFLICT(epc) DO UPDATE SET name=excluded.name, bib=excluded.bib',
                       (epc, name, bib))
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify(error=BIB_TAKEN), 409
        finally:
            db.close()
    return jsonify(ok=True)


@app.post('/api/start')
def start():
    with lock:
        if state['running']:
            return jsonify(error='Course déjà en cours'), 409
        db = connect_db()
        try:
            people = db.execute('SELECT epc, bib FROM participants').fetchall()
            known_epcs = {epc for epc, _ in people}
            bib_to_epc = {bib: epc for epc, bib in people if bib}
            if not known_epcs:
                return jsonify(error='Ajouter au moins un participant'), 400
            db.execute('DELETE FROM passages')
            db.commit()
        finally:
            db.close()
        state['scan'] = None
        now = time.monotonic()
        state.update(running=True, started_mono=now, started_wall=datetime.now().isoformat(),
                     stopped_elapsed=0.0, last_seen={}, armed={}, last_lap={},
                     laps={}, unknown={}, known_epcs=known_epcs,
                     bib_to_epc=bib_to_epc, last_passage={})
    return jsonify(ok=True)


@app.post('/api/stop')
def stop():
    with lock:
        if state['running']:
            state['stopped_elapsed'] = time.monotonic() - state['started_mono']
            state['running'] = False
    return jsonify(ok=True)




@app.post('/api/participants/rename')
def rename_participant():
    data = request.get_json(silent=True) or {}
    epc = str(data.get('epc', '')).strip().upper()
    name = str(data.get('name', '')).strip()

    if not epc or not name or len(name) > 120:
        return jsonify(error='Nom ou badge invalide.'), 400
    bib, bib_error = parse_bib(data)
    if bib_error:
        return jsonify(error=bib_error), 400

    with lock:
        if state['running']:
            return jsonify(error='Arrête la course avant modification.'), 409

        db = connect_db()
        try:
            if 'bib' in data:
                cur = db.execute('UPDATE participants SET name=?, bib=? WHERE epc=?',
                                 (name, bib, epc))
            else:
                cur = db.execute('UPDATE participants SET name=? WHERE epc=?',
                                 (name, epc))
            if cur.rowcount == 0:
                return jsonify(error='Participant introuvable.'), 404
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify(error=BIB_TAKEN), 409
        finally:
            db.close()

    return jsonify(ok=True)


@app.post('/api/participants/delete')
def delete_participant():
    data = request.get_json(silent=True) or {}
    epc = str(data.get('epc', '')).strip().upper()

    if not epc:
        return jsonify(error='Badge invalide.'), 400

    with lock:
        if state['running']:
            return jsonify(error='Arrête la course avant suppression.'), 409

        db = connect_db()
        try:
            cur = db.execute(
                'DELETE FROM participants WHERE epc=?',
                (epc,)
            )
            if cur.rowcount == 0:
                return jsonify(error='Participant introuvable.'), 404
            db.commit()
        finally:
            db.close()

    return jsonify(ok=True)


if __name__ == '__main__':
    connect_db().close()
    threading.Thread(target=reader_worker, daemon=True).start()
    vision.start(on_bib=process_bib, on_status=set_camera_status)
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
