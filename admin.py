import os
import re
import sys
import time
import json
import psutil
import shutil
import sqlite3
import hashlib
import zipfile
import threading
import subprocess
from datetime import datetime, timedelta
from cryptography.fernet import Fernet, InvalidToken

# ======================== ডিরেক্টরি ও পাথ কনফিগারেশন ========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pyhost_system.db")
PROJECTS_DIR = os.path.join(BASE_DIR, "user_projects")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(PROJECTS_DIR, exist_ok=True)

# আপনার দেওয়া আসল ADMIN_ID সেট করা হয়েছে
_DEFAULT_ADMIN_IDS = [5504272381]
_env_admin_ids = os.environ.get("ADMIN_IDS", "").strip()
if _env_admin_ids:
    try:
        ADMIN_IDS = [int(x.strip()) for x in _env_admin_ids.split(",") if x.strip()]
    except Exception:
        ADMIN_IDS = _DEFAULT_ADMIN_IDS
else:
    ADMIN_IDS = _DEFAULT_ADMIN_IDS

STATIC_KEY = os.environ.get("ENCRYPTION_KEY", "a8StDhnfpthgsr89cJ4l6tuLgOmZY0IIPDqWFkkRbHA=").encode("utf-8")
try:
    cipher_suite = Fernet(STATIC_KEY)
except Exception:
    cipher_suite = Fernet(Fernet.generate_key())

SYSTEM_CONFIG = {
    "maintenance_mode": False,
    "max_cpu_per_process": 80.0,
    "max_ram_mb_per_process": 256,
    "allow_new_deployments": True,
    "max_free_session_seconds": 43200,     # ১২ ঘণ্টা (ফ্রি ইউজার)
    "free_warning_seconds": 41400,         # ১১ ঘণ্টা ৩০ মিনিট
    "free_bot_limit": 2,
    "pro_bot_limit": 1,
    "premium_bot_limit": 3,
    "business_bot_limit": 10,
    "referral_target": 3,
    "referral_bonus_days": 3
}

TIER_PLANS = {
    "PRO": {
        "name": "⚡ Pro Plan",
        "title": "Pro",
        "days": 7,
        "price_num": 30,
        "price": "30 BDT",
        "bot_limit": SYSTEM_CONFIG["pro_bot_limit"],
        "features": "• 1 Bot Active\n• 24/7 Running • Fast Boot\n• 100% Ad-Free • Auto-Restart"
    },
    "PREMIUM": {
        "name": "🌟 Premium Plan",
        "title": "Premium",
        "days": 30,
        "price_num": 100,
        "price": "100 BDT",
        "bot_limit": SYSTEM_CONFIG["premium_bot_limit"],
        "features": "• 3 Bots + Automation\n• 24/7 Non-Stop • Auto-Restart Watchdog\n• 1-Click Backup Shield • Fast NVMe"
    },
    "BUSINESS": {
        "name": "👑 Business Boss",
        "title": "Business Boss",
        "days": 30,
        "price_num": 230,
        "price": "230 BDT",
        "bot_limit": SYSTEM_CONFIG["business_bot_limit"],
        "features": "• 10 Bots + Full Automation\n• 24/7 Non-Stop • Max RAM Resource\n• Live Logs • VIP Priority Support"
    }
}

COUPONS = {
    "PYHOST20": 20,
    "WELCOME50": 50,
    "SPECIAL10": 10
}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_admin_database():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            plan TEXT DEFAULT 'FREE',
            plan_expiry TEXT DEFAULT 'LIFETIME',
            joined_date TEXT,
            last_active TEXT,
            total_deploys INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            referred_by INTEGER DEFAULT 0,
            referral_count INTEGER DEFAULT 0,
            referral_claimed INTEGER DEFAULT 0,
            credits REAL DEFAULT 0.0,
            referral_credits REAL DEFAULT 0.0,
            expiry_alert_24h INTEGER DEFAULT 0,
            expiry_alert_2h INTEGER DEFAULT 0
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            project_name TEXT,
            main_file TEXT DEFAULT 'main.py',
            status TEXT DEFAULT 'STOPPED',
            created_at TEXT,
            last_run TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            plan_name TEXT,
            days INTEGER,
            price TEXT,
            gateway TEXT DEFAULT 'bKash',
            trx_id TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

init_admin_database()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_max_bot_limit(plan: str) -> int:
    plan = (plan or "FREE").upper()
    if plan == "PRO":
        return SYSTEM_CONFIG["pro_bot_limit"]
    elif plan == "PREMIUM":
        return SYSTEM_CONFIG["premium_bot_limit"]
    elif plan in ("BUSINESS", "VIP"):
        return SYSTEM_CONFIG["business_bot_limit"]
    return SYSTEM_CONFIG["free_bot_limit"]

def register_user(user_id: int, username: str, referrer_id: int = None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reward_user_id = None

    if not row:
        ref_id = referrer_id if (referrer_id and referrer_id != user_id) else 0
        cur.execute("""
            INSERT INTO users (user_id, username, plan, plan_expiry, joined_date, last_active, total_deploys, is_banned, ban_reason, referred_by, referral_count, referral_claimed, credits, referral_credits)
            VALUES (?, ?, 'FREE', 'LIFETIME', ?, ?, 0, 0, '', ?, 0, 0, 0.0, 0.0)
        """, (user_id, username or "Anonymous", now, now, ref_id))

        if ref_id:
            cur.execute("SELECT user_id, referral_count, referral_claimed FROM users WHERE user_id = ?", (ref_id,))
            ref_data = cur.fetchone()
            if ref_data:
                new_count = ref_data["referral_count"] + 1
                cur.execute("UPDATE users SET referral_count = ? WHERE user_id = ?", (new_count, ref_id))
                target = SYSTEM_CONFIG["referral_target"]
                if new_count >= (ref_data["referral_claimed"] + 1) * target:
                    cur.execute("UPDATE users SET referral_claimed = referral_claimed + 1 WHERE user_id = ?", (ref_id,))
                    reward_user_id = ref_id
    else:
        cur.execute("UPDATE users SET username = ?, last_active = ? WHERE user_id = ?",
                    (username or "Anonymous", now, user_id))
    
    conn.commit()
    conn.close()

    if reward_user_id:
        set_user_plan(reward_user_id, "PREMIUM", SYSTEM_CONFIG["referral_bonus_days"])
        return reward_user_id
    return None

def get_user(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "user_id": user_id,
        "username": "Unknown",
        "plan": "FREE",
        "plan_expiry": "LIFETIME",
        "total_deploys": 0,
        "is_banned": 0,
        "ban_reason": "",
        "referred_by": 0,
        "referral_count": 0,
        "referral_claimed": 0,
        "credits": 0.0,
        "referral_credits": 0.0
    }

def get_all_users():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY joined_date DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_users(query: str, limit: int = 50):
    conn = get_db_connection()
    cur = conn.cursor()
    q = (query or "").strip()
    like = f"%{q}%"
    if q.isdigit():
        cur.execute("SELECT * FROM users WHERE user_id = ? OR username LIKE ? ORDER BY joined_date DESC LIMIT ?", (int(q), like, limit))
    else:
        cur.execute("SELECT * FROM users WHERE username LIKE ? ORDER BY joined_date DESC LIMIT ?", (like, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def paginate(items: list, page: int, per_page: int = 6):
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    return items[start:end], page, total_pages

def set_user_plan(user_id: int, plan: str, days: int = 30):
    conn = get_db_connection()
    cur = conn.cursor()
    plan = plan.upper()
    now = datetime.now()
    expiry = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("UPDATE users SET plan = ?, plan_expiry = ? WHERE user_id = ?", (plan, expiry, user_id))
    conn.commit()
    conn.close()

def set_user_ban(user_id: int, ban: bool, reason: str = ""):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned = ?, ban_reason = ? WHERE user_id = ?", (1 if ban else 0, reason, user_id))
    conn.commit()
    conn.close()

def increment_deploy_count(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET total_deploys = total_deploys + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_user_projects(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE user_id = ? ORDER BY project_id ASC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_project_by_id(project_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def create_user_project(user_id: int, project_name: str, main_file: str = "main.py"):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO projects (user_id, project_name, main_file, status, created_at, last_run)
        VALUES (?, ?, ?, 'STOPPED', ?, ?)
    """, (user_id, project_name, main_file, now, "Never"))
    pid = cur.lastrowid
    conn.commit()
    conn.close()

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    os.makedirs(p_dir, exist_ok=True)
    return pid

def delete_user_project(project_id: int):
    p = get_project_by_id(project_id)
    if not p:
        return
    user_id = p["user_id"]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    conn.commit()
    conn.close()

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    if os.path.exists(p_dir):
        shutil.rmtree(p_dir, ignore_errors=True)

def update_project_status(project_id: int, status: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("UPDATE projects SET status = ?, last_run = ? WHERE project_id = ?", (status, now, project_id))
    conn.commit()
    conn.close()

def update_project_main_file(project_id: int, main_file: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET main_file = ? WHERE project_id = ?", (main_file, project_id))
    conn.commit()
    conn.close()

def get_project_files_info(user_id: int, project_id: int):
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    if not os.path.exists(p_dir):
        return [], 0
    file_list = []
    total_size = 0
    for root, _, files in os.walk(p_dir):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, p_dir)
            size = os.path.getsize(full_path)
            total_size += size
            file_list.append({"name": rel_path, "size": size})
    return file_list, total_size

def zip_project_folder(user_id: int, project_id: int):
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    if not os.path.exists(p_dir):
        return None
    project = get_project_by_id(project_id)
    proj_name = re.sub(r'[^a-zA-Z0-9_]', '_', project["project_name"]) if project else f"bot_{project_id}"
    zip_path = os.path.join(BACKUP_DIR, f"{proj_name}_{project_id}_backup.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(p_dir):
            for file in files:
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, p_dir)
                zf.write(full_p, arcname=rel_p)
    return zip_path

def create_vip_request(user_id: int, username: str, plan_name: str, days: int, price: str, gateway: str, trx_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO vip_requests (user_id, username, plan_name, days, price, gateway, trx_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
    """, (user_id, username or "Anonymous", plan_name, days, price, gateway, trx_id, now))
    req_id = cur.lastrowid
    conn.commit()
    conn.close()
    return req_id

def get_vip_request(req_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM vip_requests WHERE request_id = ?", (req_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_pending_vip_requests():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM vip_requests WHERE status = 'PENDING' ORDER BY request_id DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_vip_request_status(req_id: int, status: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE vip_requests SET status = ? WHERE request_id = ?", (status, req_id))
    conn.commit()
    conn.close()

def get_bot_resource_usage(pid_num: int):
    try:
        p = psutil.Process(pid_num)
        mem_mb = p.memory_info().rss // (1024 * 1024)
        cpu = p.cpu_percent(interval=0.05)
        return mem_mb, cpu
    except Exception:
        return 0, 0.0

def get_comprehensive_server_stats(active_processes: dict) -> str:
    cpu_percent = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory()
    users = get_all_users()
    return (
        f"📊 <b>SERVER STATS:</b>\n"
        f"💻 CPU: <code>{cpu_percent}%</code>\n"
        f"🧠 RAM: <code>{ram.used // (1024*1024)}MB / {ram.total // (1024*1024)}MB</code>\n"
        f"👥 Users: <code>{len(users)}</code>\n"
        f"🤖 Active Bots: <code>{len(active_processes)}</code>"
    )

def generate_system_backup():
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filepath = os.path.join(BACKUP_DIR, f"backup_{now_str}.zip")
    with zipfile.ZipFile(backup_filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(DB_PATH):
            zf.write(DB_PATH, arcname="pyhost_system.db")
    return backup_filepath

def execute_server_cleanup():
    deleted_bytes = 0
    for root, dirs, _ in os.walk(BASE_DIR):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return 1024