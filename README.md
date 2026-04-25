# 📡 Fleet Control Console (Server)

🌍 *[Read in English](#english) | 🇮🇹 [Leggi in Italiano](#italiano)*

---

<a name="english"></a>
## 🇬🇧 English

**Fleet Control Console** is a professional, real-time command and control (C2) dashboard designed for amateur radio repeater networks (MMDVM). 

![Dashboard Screenshot](images/dashboard.png)

### 🤖 Remote Agent
To monitor your remote nodes (Raspberry Pi), download the dedicated lightweight agent here:
`[Insert your Agent Repository URL here]`

### ✨ Features
* **Zero-Latency Real-Time UI:** Powered by WebSockets (Socket.IO).
* **Web Push Notifications:** Instant alerts on desktop or mobile.
* **Centralized Telemetry & Service Management.**
* **Global Operations:** Switch profiles instantly.

### 🚀 Installation & Setup
1. Read `install.txt` to install system prerequisites (compilers).
2. `python3 -m venv venv && source venv/bin/activate`
3. `pip install -r requirements.txt`
4. Configure `config.json` and generate VAPID keys.
5. Run via Gunicorn: `gunicorn -k "geventwebsocket.gunicorn.workers.GeventWebSocketWorker" -w 1 --bind 0.0.0.0:9000 app:app`

---

<a name="italiano"></a>
## 🇮🇹 Italiano

**Fleet Control Console** è una dashboard di comando e controllo (C2) professionale in tempo reale per le reti di ripetitori radioamatoriali (MMDVM).

![Schermata Dashboard](images/dashboard.png)

### 🤖 Agente Remoto
Per monitorare i tuoi nodi remoti (Raspberry Pi), scarica l'agente dedicato qui:
`[Inserisci qui l'URL del tuo Repository Agent]`

### ✨ Funzionalità
* **Interfaccia Real-Time a Latenza Zero** tramite WebSockets.
* **Notifiche Push Web** per allarmi critici.
* **Telemetria Centralizzata e Gestione Servizi.**
* **Operazioni Globali** su tutta la rete.

### 🚀 Installazione
1. Leggi `install.txt` per i requisiti di sistema (compilatori Linux).
2. `python3 -m venv venv && source venv/bin/activate`
3. `pip install -r requirements.txt`
4. Configura `config.json` con credenziali MQTT e chiavi VAPID.
5. Avvia con Gunicorn.

---
*Created by IV3JDV @ ARIFVG - 2026*
