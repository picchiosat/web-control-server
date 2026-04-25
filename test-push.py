import sqlite3
import json
from app import broadcast_push_notification, init_db

#  Proviamo a inviare una notifica a tutti gli iscritti nel DB
print("🚀 Starting notification test...")
broadcast_push_notification(
    "⚠️ ALLERTA FLOTTA", 
    "Test Push Notification: System Online!"
)
print("✅ Command sent.")
