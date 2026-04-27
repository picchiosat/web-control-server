from flask import Flask, render_template, request, session, jsonify, send_from_directory
from paho.mqtt import client as mqtt_client
from werkzeug.security import generate_password_hash, check_password_hash
import json
import os
import sqlite3
import urllib.request
import threading
import time
import logging
from pywebpush import webpush, WebPushException
from logging.handlers import RotatingFileHandler
from flask_socketio import SocketIO, emit

# --- DYNAMIC BASE PATH ---
# Calcola automaticamente la directory esatta in cui si trova questo file (app.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- PATHS ---
DB_PATH = os.path.join(BASE_DIR, 'monitor.db')
CACHE_FILE = os.path.join(BASE_DIR, 'telemetry_cache.json')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
DMR_IDS_PATH = os.path.join(BASE_DIR, 'dmrid.dat')
NXDN_IDS_PATH = os.path.join(BASE_DIR, 'nxdn.csv')
CLIENTS_PATH = os.path.join(BASE_DIR, 'clients.json')
LOG_FILE = os.path.join(BASE_DIR, 'fleet_console.log')

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10000000, backupCount=3),
        logging.StreamHandler()
    ],
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("FleetHub")
# Silence HTTP request spam (GET /api/states 200 OK)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('PRAGMA journal_mode=WAL;') # <-- MAGIC: Enable simultaneous read/write!

    c.execute('''CREATE TABLE IF NOT EXISTS radio_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME, client_id TEXT, 
                  source_id TEXT, target TEXT, slot INTEGER, duration REAL, ber REAL, loss REAL)''')
    try:
        c.execute("ALTER TABLE radio_logs ADD COLUMN protocol TEXT DEFAULT 'DMR'")
    except: pass 

    try:
        c.execute("ALTER TABLE radio_logs ADD COLUMN source_ext TEXT DEFAULT ''")
    except: pass

    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password_hash TEXT, 
                  role TEXT, allowed_nodes TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions
             (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, subscription TEXT UNIQUE)''')

    c.execute('''CREATE TABLE IF NOT EXISTS audit_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME, username TEXT, 
                  client_id TEXT, command TEXT)''')

    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        #  Default value if missing config file
         def_user = "admin"
         def_pass = "admin123"
        
        # Try read config.json
         try:
             with open(CONFIG_PATH, 'r') as f:
                 cfg = json.load(f)
                 def_user = cfg.get("web_admin", {}).get("default_user", "admin")
                 def_pass = cfg.get("web_admin", {}).get("default_pass", "admin123")
         except Exception:
             pass

         h = generate_password_hash(def_pass)
         c.execute("INSERT INTO users (username, password_hash, role, allowed_nodes) VALUES (?,?,?,?)",
                   (def_user, h, 'admin', 'all'))
         logger.info(f">>> DEFAULT USER CREATED - User: {def_user} | Pass: {def_pass} <<<")

    conn.commit()
    conn.close()

init_db()

# --- ID DATABASE LOADING ---
user_db = {}
nxdn_db = {}

def load_ids():
    global user_db, nxdn_db
    user_db.clear()
    nxdn_db.clear()

    if os.path.exists(DMR_IDS_PATH):
        with open(DMR_IDS_PATH, 'r', encoding='utf-8', errors='ignore') as f:
            for l in f:
                l = l.replace('"', '') # Rimuove eventuali virgolette
                if ',' in l: p = l.strip().split(',')
                elif '\t' in l: p = l.strip().split('\t')
                elif ';' in l: p = l.strip().split(';')
                else: p = l.strip().split() # Cerca gli spazi normali
                
                if len(p) >= 2 and p[0].strip().isdigit(): 
                    user_db[p[0].strip()] = p[1].strip()
                    
    if os.path.exists(NXDN_IDS_PATH):
        with open(NXDN_IDS_PATH, 'r', encoding='utf-8', errors='ignore') as f:
            for l in f:
                sep = '\t' if '\t' in l else (',' if ',' in l else ';')
                p = l.strip().split(sep)
                if len(p) >= 2 and p[0].strip().isdigit():
                    nxdn_db[p[0].strip()] = p[1].strip()

load_ids()

def get_call(id, proto="DMR"):
    sid = str(id)
    if proto == "NXDN": return nxdn_db.get(sid, sid)
    return user_db.get(sid, sid)

def save_cache(data):
    with open(CACHE_FILE, 'w') as f: json.dump(data, f)
    socketio.emit('dati_aggiornati')

def save_to_sqlite(client_id, data, protocol="DMR"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO radio_logs (timestamp, client_id, protocol, source_id, target, slot, duration, ber, source_ext) VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?, ?, ?)",
              (client_id, protocol, str(data.get('source_id', '---')), str(data.get('destination_id', '---')), data.get('slot', 0), round(data.get('duration', 0), 1), round(data.get('ber', 0), 2), str(data.get('source_ext', ''))))
    conn.commit()
    conn.close()
    socketio.emit('dati_aggiornati')

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
app.secret_key = 'ari_fvg_secret_ultra_secure'
client_states = {}
device_configs = {}
client_telemetry = {}
last_notified_errors = {}
device_health = {}
last_seen_reflector = {}
network_mapping = {}  
node_info = {}
node_general = {}

if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, 'r') as f: client_telemetry = json.load(f)
    except: client_telemetry = {}

active_calls = {}
with open(CONFIG_PATH) as f: config = json.load(f)

# --- MQTT CALLBACKS ---
mqtt_connected_status = False

def on_connect(client, userdata, flags, reason_code, properties=None):
    global mqtt_connected_status
    if reason_code == 0:
        mqtt_connected_status = True
        logger.info("✅ Successfully connected to MQTT Broker! Subscribing to topics...")
        # Invia lo stato Online ai client web
        socketio.emit('mqtt_status', {'connected': True})
        client.subscribe([
            ("servizi/+/stat", 0), 
            ("dmr-gateway/+/json", 0), 
            ("devices/+/services", 0), 
            ("nxdn-gateway/+/json", 0), 
            ("ysf-gateway/+/json", 0), 
            ("p25-gateway/+/json", 0), 
            ("dstar-gateway/+/json", 0), 
            ("mmdvm/+/json", 0), 
            ("devices/#", 0), 
            ("data/#", 0)
        ])
    else:
        mqtt_connected_status = False
        socketio.emit('mqtt_status', {'connected': False})
        logger.error(f"❌ MQTT Connection Error. Reason code: {reason_code}")

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    global mqtt_connected_status
    mqtt_connected_status = False
    # Invia lo stato Offline ai client web
    socketio.emit('mqtt_status', {'connected': False})
    logger.warning(f"⚠️ MQTT Disconnection detected! Reason code: {reason_code}. Attempting automatic reconnection...")

# Quando un nuovo utente apre la pagina web, inviagli subito lo stato attuale
@socketio.on('connect')
def handle_connect():
    emit('mqtt_status', {'connected': mqtt_connected_status})

def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload = msg.payload.decode().strip()
        parts = topic.split('/')
        if len(parts) < 2: return
        cid = parts[1].lower()

        # --- CAPTURE FULL CONFIGURATIONS ---
        if parts[0] == 'data' and len(parts) >= 4 and parts[3] == 'full_config':
            cid_conf = parts[1].lower()
            svc_name = parts[2].lower()
            if cid_conf not in device_configs:
                device_configs[cid_conf] = {}
            try:
                device_configs[cid_conf][svc_name] = json.loads(payload)
                logger.debug(f"Configuration saved for {cid_conf} -> {svc_name}")
            except Exception as e:
                logger.error(f"Error parsing config JSON: {e}")

        # --- NODE AND SERVICE STATE MANAGEMENT ---
        elif parts[0] == 'servizi':
            client_states[cid] = payload
            socketio.emit('dati_aggiornati')  # <--- WEBSOCKET
            
            # --- PUSH TRIGGER: NODE STATE ---
            if payload.upper() == 'OFFLINE':
                if last_notified_errors.get(f"{cid}_NODE") != 'OFFLINE':
                    broadcast_push_notification(f"💀 NODE OFFLINE: {cid.upper()}", "Connection lost with broker.")
                    last_notified_errors[f"{cid}_NODE"] = 'OFFLINE'
            elif payload.upper() == 'ONLINE':
                if last_notified_errors.get(f"{cid}_NODE") == 'OFFLINE':
                    broadcast_push_notification(f"🌤️ NODE ONLINE: {cid.upper()}", "Node is back online.")
                    del last_notified_errors[f"{cid}_NODE"]
                    
            if payload.upper() not in ['OFF', 'OFFLINE', '']:
                tel = client_telemetry.get(cid, {})
                if isinstance(tel, dict) and '🔄' in str(tel.get('ts1', '')):
                    client_telemetry[cid] = {"ts1": "Waiting...", "ts2": "Waiting...", "alt": ""}
                    save_cache(client_telemetry)

        # --- DEVICE HEALTH MANAGEMENT ---
        elif parts[0] == 'devices' and len(parts) >= 3 and parts[2] == 'services':
            try:
                data = json.loads(payload)
                device_health[cid] = {
                    "cpu": round(data.get("cpu_usage_percent", 0), 1),
                    "temp": round(data.get("cpu_temp", 0), 1),
                    "ram": round(data.get("memory_usage_percent", 0), 1),
                    "disk": round(data.get("disk_usage_percent", 0), 1),
                    "processes": data.get("processes", {}),
                    "files": data.get("files", data.get("config_files", [])),
                    "profiles": data.get("profiles", {"A": "PROFILE A", "B": "PROFILE B"})
                }
                socketio.emit('dati_aggiornati')  # <--- WEBSOCKET

                # --- PUSH TRIGGER: SERVICE ERRORS ---
                processes = data.get("processes", {})
                for svc_name, svc_status in processes.items():
                    status_key = f"{cid}_{svc_name}"
                    s_lower = svc_status.lower()
                    if s_lower in ["error", "stopped", "failed"]:
                        if last_notified_errors.get(status_key) != s_lower:
                            msg_err = f"Service {svc_name} KO ({svc_status})"
                            if s_lower == "error": msg_err += " - Auto-healing failed! ⚠️"
                            broadcast_push_notification(f"🚨 ALARM: {cid.upper()}", msg_err)
                            last_notified_errors[status_key] = s_lower
                    elif s_lower == "online" and status_key in last_notified_errors:
                        broadcast_push_notification(f"✅ RESTORED: {cid.upper()}", f"Service {svc_name} back ONLINE.")
                        del last_notified_errors[status_key]
                # -----------------------------------------

            except Exception as e: 
                logger.error(f"Error parsing health data: {e}")

        # --- DMR GATEWAY MANAGEMENT ---
        elif len(parts) >= 4 and parts[0] == 'data' and parts[2].lower() == 'dmrgateway' and (parts[3].upper().startswith('NETWORK') or parts[3].upper().startswith('DMR NETWORK')):
            try:
                cid = parts[1].lower()
                data = json.loads(payload)
                
                if cid not in network_mapping:
                    network_mapping[cid] = {"ts1": "", "ts2": ""}
                
                if str(data.get("Enabled")) == "1":
                    net_name = data.get("Name", "Net").upper()
                    is_ts1 = False
                    is_ts2 = False
                    
                    keys_to_check = ["PassAllTG", "PassAllPC", "TGRewrite", "PCRewrite", "TypeRewrite", "SrcRewrite"]
                    for k in keys_to_check:
                        val = str(data.get(k, "")).strip()
                        if val.startswith("1"): is_ts1 = True
                        if val.startswith("2"): is_ts2 = True
                        
                    if is_ts1: network_mapping[cid]["ts1"] = net_name
                    if is_ts2: network_mapping[cid]["ts2"] = net_name
                    socketio.emit('dati_aggiornati')  # <--- WEBSOCKET
                        
            except Exception as e:
                logger.error(f"Error parsing DMRGateway for {cid}: {e}")

        # --- MMDVMHOST INFO MANAGEMENT (FREQUENZE & LOCATION) ---
        elif len(parts) >= 4 and parts[0] == 'data' and parts[2].lower() == 'mmdvmhost' and parts[3].lower() == 'info':
            try:
                cid = parts[1].lower()
                data = json.loads(payload)
                
                # Estrazione dati
                tx = data.get("TXFrequency", "0")
                rx = data.get("RXFrequency", "0")
                lat = data.get("Latitude", "0.0")
                lon = data.get("Longitude", "0.0")
                loc = data.get("Location", "Sconosciuta")
                
                # Funzione per formattare gli Hz in MHz
                def format_freq(f):
                    if str(f).isdigit() and int(f) > 0:
                        return f"{int(f)/1000000:.3f} MHz"
                    return str(f)
                    
                # Salvataggio nel dizionario globale
                node_info[cid] = {
                    "tx": format_freq(tx), 
                    "rx": format_freq(rx),
                    "lat": lat,
                    "lon": lon,
                    "loc": loc
                }
                socketio.emit('dati_aggiornati')
            except Exception as e:
                logger.error(f"Error parsing MMDVMHost info for {cid}: {e}")

        # --- MMDVMHOST GENERAL MANAGEMENT (CALLSIGN & ID & DUPLEX) ---
        elif len(parts) >= 4 and parts[0] == 'data' and parts[2].lower() == 'mmdvmhost' and parts[3].lower() == 'general':
            try:
                cid = parts[1].lower()
                data = json.loads(payload)
                callsign = data.get("Callsign", "")
                radio_id = data.get("Id", "")
                duplex = data.get("Duplex", "1") # 1 = Repeater, 0 = Simplex
                
                if callsign:
                    node_general[cid] = {"callsign": callsign, "radio_id": radio_id, "duplex": str(duplex)}
                    socketio.emit('dati_aggiornati')
            except Exception as e:
                logger.error(f"Error parsing MMDVMHost general for {cid}: {e}")

        # --- OTHER GATEWAYS MANAGEMENT ---
        elif parts[0] in ['dmr-gateway', 'nxdn-gateway', 'ysf-gateway', 'p25-gateway', 'dstar-gateway']:
            data = json.loads(payload)
            proto = "DMR"
            if "nxdn" in parts[0]: proto = "NXDN"
            elif "ysf" in parts[0]: proto = "YSF"
            elif "p25" in parts[0]: proto = "P25"
            elif "dstar" in parts[0]: proto = "D-STAR"
            
            m = ""
            if 'status' in data: 
                m = data['status'].get('message', '')
            elif 'link' in data:
                l = data['link']
                dest = str(l.get('reflector') or l.get('talkgroup') or '---').strip()
                action = l.get('action')
                if action == 'linking': last_seen_reflector[f"{cid}_{proto}"] = dest
                elif action == 'unlinking': last_seen_reflector[f"{cid}_{proto}"] = "---"
                m = f"{'Link' if action=='linking' else 'Unlinked'} {dest}"
            
            if m: save_to_sqlite(cid, {'source_id': "🌐 " + m, 'destination_id': 'NET'}, protocol=proto)

        # --- MMDVM AND TRAFFIC MANAGEMENT ---
        elif parts[0] == 'mmdvm':
            data = json.loads(payload)
            if cid not in active_calls: active_calls[cid] = {}
            if cid not in client_telemetry or not isinstance(client_telemetry.get(cid), dict):
                client_telemetry[cid] = {"ts1": "Waiting...", "ts2": "Waiting...", "alt": "", "idle": True}

            if 'MMDVM' in data and data['MMDVM'].get('mode') == 'idle':
                client_telemetry[cid]["idle"] = True
                save_cache(client_telemetry)
                return 

            client_telemetry[cid]["idle"] = False

            if 'DMR' in data:
                d = data['DMR']
                act = d.get('action')
                sk = f"ts{d.get('slot', 1)}"
                if act in ['start', 'late_entry']:
                    src = get_call(d.get('source_id'))
                    dst = str(d.get('destination_id'))
                    active_calls[cid][sk] = {'src': src, 'dst': dst}
                    client_telemetry[cid]["alt"] = ""
                    client_telemetry[cid][sk] = f"🎙️ {src} ➔ TG {dst}"
                    socketio.emit('dati_aggiornati')  # <--- WEBSOCKET
                elif act in ['end', 'lost']:
                    info = active_calls[cid].get(sk, {'src': '---', 'dst': '---'})
                    d['source_id'], d['destination_id'] = info['src'], info['dst']
                    save_to_sqlite(cid, d, protocol="DMR")
                    client_telemetry[cid][sk] = f"{'✅' if act=='end' else '⚠️'} {info['src']}"
                    save_cache(client_telemetry)
                    if sk in active_calls[cid]: del active_calls[cid][sk]

            else:
                for k, ico, name in [('NXDN','🟢','NXDN'),('YSF','🟣','YSF'),('P25','🟠','P25'),('D-Star','🔵','D-STAR')]:
                    if k in data:
                        p = data[k]
                        act = p.get('action')
                        
                        if act == 'start':
                            if k == 'NXDN': src = get_call(p.get('source_id', '---'), 'NXDN')
                            elif k == 'P25': src = get_call(p.get('source_id', '---'), 'DMR')
                            else: src = str(p.get('Callsign', p.get('source_cs', p.get('source_info', p.get('source_id', '---')))))
                            
                            t_list = [p.get('reflector'), p.get('destination_cs'), p.get('destination_id')]
                            current_target = next((str(x).strip() for x in t_list if x and str(x).strip() not in ['', '---', '0', 'CQCQCQ']), None)
                            
                            if not current_target or current_target == cid.upper():
                                target = last_seen_reflector.get(f"{cid}_{name}", "---")
                            else:
                                target = current_target

                            active_calls[cid][k] = {'src': src, 'dst': target, 'ext': str(p.get('source_ext', ''))}
                            client_telemetry[cid].update({"ts1":"","ts2":"","alt": f"{ico} {name}: {src} ➔ {target}"})
                            socketio.emit('dati_aggiornati')  # <--- WEBSOCKET
                        
                        elif act in ['end', 'lost']:
                            info = active_calls[cid].get(k, {'src': '---', 'dst': '---', 'ext': ''})
                            p.update({'source_id': info['src'], 'destination_id': info['dst'], 'source_ext': info['ext']})
                            save_to_sqlite(cid, p, protocol=name)
                            client_telemetry[cid]["alt"] = f"{'✅' if act=='end' else '⚠️'} {name}: {info['src']}"
                            save_cache(client_telemetry)
                            if k in active_calls[cid]: del active_calls[cid][k]
    except Exception as e: 
        logger.error(f"MQTT MSG ERROR: {e}")

# --- MQTT CLIENT INITIALIZATION ---
# Recuperiamo l'ID dal config. Se per caso manca, usiamo un default di emergenza.
mqtt_id = config['mqtt'].get('client_id', 'flask_backend_generic')

mqtt_backend = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2, mqtt_id)
mqtt_backend.username_pw_set(config['mqtt']['user'], config['mqtt']['password'])
mqtt_backend.on_connect = on_connect
mqtt_backend.on_disconnect = on_disconnect
mqtt_backend.on_message = on_message
try:
    # Usiamo connect_async! Non blocca il server se l'IP è irraggiungibile
    mqtt_backend.connect_async(config['mqtt']['broker'], config['mqtt']['port'])
    mqtt_backend.loop_start()
    logger.info(f"Avvio connessione MQTT asincrona verso {config['mqtt']['broker']}...")
except Exception as e:
    logger.error(f"Errore critico nell'inizializzazione MQTT: {e}")

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/clients')
def get_clients():
    if os.path.exists(CLIENTS_PATH):
        with open(CLIENTS_PATH, 'r') as f: return jsonify(json.load(f))
    return jsonify([])

@app.route('/api/logs')
def get_logs():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute("SELECT timestamp, client_id, protocol, source_id, target, slot, duration, ber, source_ext FROM radio_logs ORDER BY id DESC LIMIT 60")
    logs = c.fetchall()
    conn.close()
    return jsonify(logs)

@app.route('/api/states', methods=['GET'])
def get_states():
    return jsonify({
        "states": client_states,
        "telemetry": client_telemetry,
        "health": device_health,
        "networks": network_mapping,
        "info": node_info,
        "general": node_general
    })

@app.route('/api/stats')
def get_stats():
    # Leggiamo il parametro 'node' (di default 'all')
    node = request.args.get('node', 'all').lower()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Prepariamo il filtro aggiuntivo se è stato selezionato un nodo
    node_filter = ""
    params = []
    if node != 'all':
        node_filter = " AND LOWER(client_id) = ?"
        params.append(node)
    
    # 1. Top 5 TalkGroups di OGGI
    c.execute(f"""SELECT target, COUNT(*) as cnt FROM radio_logs 
                 WHERE target NOT IN ('---', 'NET', '') 
                 AND date(timestamp) = date('now', 'localtime'){node_filter} 
                 GROUP BY target ORDER BY cnt DESC LIMIT 5""", params)
    top_tgs = [{"target": row[0], "count": row[1]} for row in c.fetchall()]
    
    # 2. Top 5 Callsign di OGGI
    c.execute(f"""SELECT source_id, COUNT(*) as cnt FROM radio_logs 
                 WHERE source_id NOT LIKE '🌐%' 
                 AND date(timestamp) = date('now', 'localtime'){node_filter} 
                 GROUP BY source_id ORDER BY cnt DESC LIMIT 5""", params)
    top_calls = [{"call": row[0], "count": row[1]} for row in c.fetchall()]
    
    # 3. Tempo medio dei transiti di OGGI
    c.execute(f"""SELECT AVG(duration) FROM radio_logs 
                 WHERE duration > 0.5 
                 AND date(timestamp) = date('now', 'localtime'){node_filter}""", params)
    avg_dur = c.fetchone()[0]
    avg_dur = round(avg_dur, 1) if avg_dur else 0
    
    # 4. Totale transiti di OGGI
    c.execute(f"SELECT COUNT(*) FROM radio_logs WHERE date(timestamp) = date('now', 'localtime'){node_filter}", params)
    today_tx = c.fetchone()[0]
    
    conn.close()
    return jsonify({
        "top_tgs": top_tgs,
        "top_calls": top_calls,
        "avg_duration": avg_dur,
        "today_tx": today_tx
    })

@app.route('/api/service_control', methods=['POST'])
def service_control():
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    cid = d.get('clientId').lower()
    action = d.get('action')
    service = d.get('service')
    mqtt_backend.publish(f"devices/{cid}/control", f"{action}:{service}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO audit_logs (timestamp, username, client_id, command) VALUES (datetime('now','localtime'), ?, ?, ?)",
              (session.get('user'), cid, f"SVC_{action.upper()}_{service}"))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/login', methods=['POST'])
def login():
    d = request.json
    username, password = d.get('user'), d.get('pass')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    if user and check_password_hash(user['password_hash'], password):
        session['logged_in'] = True
        session['user'] = user['username']
        session['role'] = user['role']
        session['allowed_nodes'] = user['allowed_nodes']
        return jsonify({"success": True, "role": user['role'], "allowed_nodes": user['allowed_nodes']})
    return jsonify({"success": False}), 401

@app.route('/api/command', methods=['POST'])
def cmd():
    if not session.get('logged_in'): return jsonify({"success": False, "error": "Not authenticated"}), 403
    d = request.json
    cid = d['clientId'].lower()
    cmd_type = d['type']
    username = session.get('user')
    role = session.get('role')
    allowed = session.get('allowed_nodes', '')
    is_allowed = (role == 'admin' or allowed == 'all' or cid in [x.strip() for x in allowed.split(',')])
    if cmd_type == 'REBOOT' and role != 'admin':
        return jsonify({"success": False, "error": "Only Admins can reboot."}), 403
    if is_allowed:
        mqtt_backend.publish(f"servizi/{cid}/cmnd", cmd_type)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO audit_logs (timestamp, username, client_id, command) VALUES (datetime('now','localtime'), ?, ?, ?)",
                  (username, cid, cmd_type))
        conn.commit()
        conn.close()
        client_telemetry[cid] = {"ts1": "🔄 Sent...", "ts2": "🔄 Sent...", "alt": ""}
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "You do not have permission for this node."}), 403

@app.route('/api/update_nodes', methods=['POST'])
def update_nodes():
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    mqtt_backend.publish("devices/control/request", "update")
    return jsonify({"success": True})

    # Mandiamo il comando "update" direttamente nel topic privato di ciascun nodo
    for client in clients_list:
        cid = client['id'].lower()
        mqtt_backend.publish(f"devices/{cid}/control", "update", qos=1)
        
    logger.info("📢 Inviato comando REQ CONFIG diretto a tutti i nodi della flotta.")
    return jsonify({"success": True})

@app.route('/api/users', methods=['GET'])
def get_users():
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, username, role, allowed_nodes FROM users")
    users = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(users)

@app.route('/api/users', methods=['POST'])
def add_user():
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    username = d.get('username')
    password = d.get('password')
    role = d.get('role', 'operator')
    allowed = d.get('allowed_nodes', '')
    if not username or not password:
        return jsonify({"success": False, "error": "Missing data"})
    h = generate_password_hash(password)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password_hash, role, allowed_nodes) VALUES (?,?,?,?)",
                  (username, h, role, allowed))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except sqlite3.IntegrityError:
        return jsonify({"success": False, "error": "Username already exists"})

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    u = c.fetchone()
    if u and u[0] == session.get('user'):
        conn.close()
        return jsonify({"success": False, "error": "You cannot delete yourself!"})
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/users/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    if session.get('role') != 'admin': 
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.json
    role = data.get('role', 'operator')
    allowed = data.get('allowed_nodes', 'all')
    password = data.get('password')

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        if password and password.strip() != "":
            h = generate_password_hash(password)
            c.execute("UPDATE users SET password_hash=?, role=?, allowed_nodes=? WHERE id=?", 
                      (h, role, allowed, user_id))
        else:
            c.execute("UPDATE users SET role=?, allowed_nodes=? WHERE id=?", 
                      (role, allowed, user_id))
            
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/change_password', methods=['POST'])
def change_password():
    if not session.get('logged_in'): 
        return jsonify({"success": False, "error": "Not authenticated"}), 403
    d = request.json
    new_pass = d.get('new_password')
    user_to_change = d.get('username')
    if session.get('role') != 'admin' and session.get('user') != user_to_change:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    if not new_pass:
        return jsonify({"success": False, "error": "Password cannot be empty"}), 400
    h = generate_password_hash(new_pass)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET password_hash = ? WHERE username = ?", (h, user_to_change))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/global_command', methods=['POST'])
def global_cmd():
    if session.get('role') != 'admin': 
        return jsonify({"success": False, "error": "Admin action only!"}), 403
    d = request.json
    cmd_type = d.get('type') 
    clients_list = []
    if os.path.exists(CLIENTS_PATH):
        with open(CLIENTS_PATH, 'r') as f:
            clients_list = json.load(f)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for client in clients_list:
        cid = client['id'].lower()
        mqtt_backend.publish(f"servizi/{cid}/cmnd", cmd_type)
        c.execute("INSERT INTO audit_logs (timestamp, username, client_id, command) VALUES (datetime('now','localtime'), ?, ?, ?)",
                  (session.get('user'), cid, f"GLOBAL_OVERRIDE_{cmd_type}"))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

def auto_update_ids():
    """Versione corretta: controlla all'avvio e poi ogni notte."""
    
    def download_logic():
        try:
            # Usiamo esattamente i nomi e le URL definiti nel tuo config/codice
            with open(CONFIG_PATH, 'r') as f:
                current_cfg = json.load(f)
            
            urls = current_cfg.get("id_urls", {
                "dmr": "https://radioid.net/static/users.csv",
                "nxdn": "https://radioid.net/static/nxdn.csv"
            })
            
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            logger.info("📡 Inizio download database ID (DMR e NXDN)...")
            
            # Download DMR -> dmrid.dat
            req_dmr = urllib.request.Request(urls["dmr"], headers=headers)
            with urllib.request.urlopen(req_dmr) as response, open(DMR_IDS_PATH, 'wb') as out_file:
                out_file.write(response.read())
                
            # Download NXDN -> nxdn.csv
            req_nxdn = urllib.request.Request(urls["nxdn"], headers=headers)
            with urllib.request.urlopen(req_nxdn) as response, open(NXDN_IDS_PATH, 'wb') as out_file:
                out_file.write(response.read())
                
            load_ids()
            logger.info("✅ Aggiornamento completato con successo.")
        except Exception as e:
            logger.error(f"❌ Errore durante il download: {e}")

    # --- CONTROLLO INIZIALE ALL'AVVIO ---
    if not os.path.exists(DMR_IDS_PATH) or not os.path.exists(NXDN_IDS_PATH):
        logger.info("🔍 File ID mancanti. Avvio download immediato...")
        download_logic()

    # --- CICLO NOTTURNO ---
    while True:
        try:
            now = time.strftime("%H:%M")
            with open(CONFIG_PATH, 'r') as f:
                target_time = json.load(f).get("update_schedule", "03:00")
            
            if now == target_time:
                logger.info(f"⏰ Orario programmato ({target_time}) raggiunto. Aggiorno...")
                download_logic()
                time.sleep(65)
        except Exception as e:
            logger.error(f"⚠️ Errore nel thread update: {e}")
        time.sleep(30)

@app.route('/api/ui_config', methods=['GET'])
def get_ui_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        ui_cfg = cfg.get("ui", {
            "profileA_Name": "PROFILE A",
            "profileA_Color": "#3498db",
            "profileB_Name": "PROFILE B",
            "profileB_Color": "#9b59b6"
        })
        return jsonify(ui_cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/config', methods=['GET'])
def get_config_api():
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    with open(CONFIG_PATH, 'r') as f:
        cfg = json.load(f)
    return jsonify({
        "update_schedule": cfg.get("update_schedule", "03:00"),
        "url_dmr": cfg.get("id_urls", {}).get("dmr", ""),
        "url_nxdn": cfg.get("id_urls", {}).get("nxdn", "")
    })

@app.route('/api/config', methods=['POST'])
def save_config_api():
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    new_data = request.json
    with open(CONFIG_PATH, 'r') as f:
        cfg = json.load(f)
    cfg["update_schedule"] = new_data.get("update_schedule", "03:00")
    if "id_urls" not in cfg: cfg["id_urls"] = {}
    cfg["id_urls"]["dmr"] = new_data.get("url_dmr", "")
    cfg["id_urls"]["nxdn"] = new_data.get("url_nxdn", "")
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=4)
    return jsonify({"success": True})

@app.route('/api/config_file/<cid>/<service>', methods=['GET'])
def get_config_file(cid, service):
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    cid = cid.lower()
    service = service.lower()
    config_data = device_configs.get(cid, {}).get(service)
    
    if not config_data:
        return jsonify({"error": "Configuration not received yet. Wait or send an UPDATE command."}), 404
    return jsonify({"success": True, "data": config_data})

@app.route('/api/config_file', methods=['POST'])
def save_config_file():
    if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    cid = d.get('clientId').lower()
    service = d.get('service').lower()
    new_config = d.get('config_data')
    topic_set = f"devices/{cid}/config_set/{service}"
    mqtt_backend.publish(topic_set, json.dumps(new_config))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO audit_logs (timestamp, username, client_id, command) VALUES (datetime('now','localtime'), ?, ?, ?)",
              (session.get('user'), cid, f"EDIT_CONFIG_{service.upper()}"))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('.', 'manifest.json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('.', 'sw.js')

@app.route('/icon-512.png')
def serve_icon():
    return send_from_directory('.', 'icon-512.png')

def broadcast_push_notification(title, body):
    wp_config = config.get('webpush')
    if not wp_config: return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, subscription FROM push_subscriptions")
    subs = c.fetchall()
    
    for sub_id, sub_json in subs:
        try:
            webpush(
                subscription_info=json.loads(sub_json),
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=wp_config['vapid_private_key'],
                vapid_claims={"sub": wp_config['vapid_claim_email']}
            )
        except WebPushException as ex:
            if ex.response and ex.response.status_code == 410: 
                c.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Generic Push Error: {e}")
    conn.close()

@app.route('/api/vapid_public_key')
def get_vapid_key():
    return jsonify({"public_key": config.get('webpush', {}).get('vapid_public_key', '')})

@app.route('/api/subscribe', methods=['POST'])
def subscribe_push():
    if not session.get('logged_in'): return jsonify({"error": "Unauthorized"}), 403
    sub_data = request.json
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO push_subscriptions (username, subscription) VALUES (?, ?)", 
              (session.get('user'), json.dumps(sub_data)))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

threading.Thread(target=auto_update_ids, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=9000)
