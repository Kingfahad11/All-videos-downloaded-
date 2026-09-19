import os
import re
import ast
import sys
import time
import shutil
import psutil
import zipfile
import tempfile
import threading
import subprocess
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image

import telebot
from telebot import types
import yt_dlp
import requests

import admin

# ======================== টেলিগ্রাম বট ইনিশিয়ালাইজেশন ========================
# আপনার নির্ধারিত আসল BOT_TOKEN সেট করা হয়েছে
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8960102537:AAHXyvXEKXs8hleb4iRikgNKveTvmLwpo7Q")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", num_threads=15)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "user_projects")
os.makedirs(PROJECTS_DIR, exist_ok=True)

# প্রসেস ও মেমোরি স্টেট
active_processes = {}
user_states = {}
installed_packages_cache = set()
URL_CACHE = {}

MAX_FILE_MB = 48
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
DOWNLOAD_SEMAPHORE = threading.Semaphore(1)

PIP_ALIASES = {
    "telebot": "pyTelegramBotAPI",
    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "telegram": "python-telegram-bot",
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "discord": "discord.py",
    "dateutil": "python-dateutil",
    "jose": "python-jose",
    "jwt": "PyJWT",
    "dns": "dnspython",
    "magic": "python-magic",
    "nmap": "python-nmap"
}

# ----------------- Render Health Web Server & Self Keep-Alive -----------------
class RenderHealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"PyHost Cloud & Downloader Engine is Live & Healthy!")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), RenderHealthHandler)
        server.serve_forever()
    except Exception:
        pass

threading.Thread(target=start_health_server, daemon=True).start()

def self_keep_alive_pinger():
    port = int(os.environ.get("PORT", 10000))
    url = f"http://127.0.0.1:{port}/"
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass

threading.Thread(target=self_keep_alive_pinger, daemon=True).start()

# ----------------- ডাউনলোডার ইউটিলিটিস ও প্রোগ্রেস বার -----------------
URL_REGEX = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

def is_valid_url(text: str) -> bool:
    return bool(URL_REGEX.match(text.strip()))

def clean_url(url: str) -> str:
    parsed = urlparse(url)
    bad_params = {'si', 'utm_source', 'utm_medium', 'utm_campaign', 'fbclid', 'igsh', 'feature'}
    qs = parse_qs(parsed.query)
    clean_qs = {k: v for k, v in qs.items() if k.lower() not in bad_params}
    return urlunparse(parsed._replace(query=urlencode(clean_qs, doseq=True)))

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com/shorts" in u or ("youtu.be" in u and "shorts" in u):
        return "YouTube Shorts 🔴"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube 🔴"
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook 🔵"
    if "instagram.com" in u:
        return "Instagram 🟣"
    if "tiktok.com" in u:
        return "TikTok ⚫"
    if "twitter.com" in u or "x.com" in u:
        return "X (Twitter) 🐦"
    if "drive.google.com" in u:
        return "Google Drive 📁"
    if "dropbox.com" in u:
        return "Dropbox 📦"
    return "Direct Media Link 🌐"

def human_size(num_bytes):
    if not num_bytes:
        return "0 B"
    num_bytes = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"

def format_duration(seconds):
    if not seconds:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

def create_bar(percent_float: float) -> str:
    filled = int(percent_float / 10)
    filled = max(0, min(10, filled))
    return "█" * filled + "░" * (10 - filled)

# ----------------- ডাউনলোডার কোর ইঞ্জিন (yt-dlp) -----------------
def _run_ytdlp_download(url, outtmpl, mode, progress_hook):
    cookie_file = "cookies.txt" if os.path.exists("cookies.txt") else None
    format_rule = "bestaudio/best" if mode == "audio" else (
        "best[filesize<48M]/bestvideo[height<=720][filesize<40M]+bestaudio/"
        "best[height<=720]/best[height<=480]/best"
    )

    ydl_opts = {
        "format": format_rule,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [progress_hook],
        "max_filesize": MAX_FILE_BYTES,
        "socket_timeout": 15,
        "cookiefile": cookie_file,
        "source_address": "0.0.0.0",
        "legacy_server_connect": True,
        "writethumbnail": True,
        "concurrent_fragment_downloads": 8,
        "buffersize": 1048576,
        "http_chunk_size": 10485760,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return info, filename

def execute_media_download(chat_id, user_id, url, mode, status_msg_id):
    with DOWNLOAD_SEMAPHORE:
        temp_dir = tempfile.mkdtemp(prefix=f"dl_{user_id}_")
        outtmpl = os.path.join(temp_dir, "%(title).60s.%(ext)s")
        last_edit = {"time": 0.0}

        def progress_hook(d):
            if d.get("status") == "downloading":
                now = time.time()
                if now - last_edit["time"] < 2.0:
                    return
                last_edit["time"] = now

                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed = d.get("speed") or 0
                eta = d.get("eta")

                percent = (downloaded / total * 100) if total > 0 else 0.0
                bar = create_bar(percent)
                speed_txt = f"{human_size(speed)}/s" if speed else "হিসাব হচ্ছে..."
                eta_txt = f"{eta}s" if eta else "..."

                mode_label = "🎵 অডিও" if mode == "audio" else "🎬 ভিডিও"
                text = (
                    f"📥 <b>{mode_label} ডাউনলোড হচ্ছে...</b>\n\n"
                    f"<code>[{bar}] {percent:.1f}%</code>\n"
                    f"📦 সাইজ: <b>{human_size(downloaded)}</b> / <b>{human_size(total)}</b>\n"
                    f"⚡ স্পিড: <b>{speed_txt}</b> | ⏳ বাকি: <b>{eta_txt}</b>"
                )
                try:
                    bot.edit_message_text(text, chat_id, status_msg_id, parse_mode="HTML")
                except Exception:
                    pass

        try:
            info, filename = _run_ytdlp_download(url, outtmpl, mode, progress_hook)
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                cand = [f for f in glob.glob(base + ".*") if not f.endswith(('.webp', '.jpg', '.png', '.part'))]
                if cand:
                    filename = cand[0]

            size = os.path.getsize(filename)
            if size > MAX_FILE_BYTES:
                bot.edit_message_text(f"❌ ফাইলটি খুব বড় ({human_size(size)})! টেলিগ্রামের লিমিট {MAX_FILE_MB} MB।", chat_id, status_msg_id)
                return

            thumb_path = None
            for ext in ['.webp', '.jpg', '.png']:
                pos_thumb = os.path.splitext(filename)[0] + ext
                if os.path.exists(pos_thumb):
                    try:
                        jpg_thumb = os.path.splitext(pos_thumb)[0] + "_thumb.jpg"
                        with Image.open(pos_thumb) as img:
                            img.convert("RGB").save(jpg_thumb, "JPEG")
                        thumb_path = jpg_thumb
                    except Exception:
                        thumb_path = pos_thumb
                    break

            platform = detect_platform(url)
            duration = info.get("duration") or 0
            duration_txt = f"\n⏱ <b>দৈর্ঘ্য:</b> {format_duration(duration)}" if duration else ""
            caption = (
                f"🎬 <b>{info.get('title') or 'Media File'}</b>\n\n"
                f"🌐 <b>প্ল্যাটফর্ম:</b> {platform}{duration_txt}\n"
                f"📦 <b>সাইজ:</b> {human_size(size)}"
            )

            bot.edit_message_text("📤 <b>টেলিগ্রামে আপলোড করা হচ্ছে...</b>", chat_id, status_msg_id, parse_mode="HTML")

            with open(filename, "rb") as media_f:
                thumb_f = open(thumb_path, "rb") if thumb_path and os.path.exists(thumb_path) else None
                try:
                    if mode == "audio":
                        bot.send_audio(chat_id, media_f, caption=caption, title=info.get("title"), duration=int(duration), thumb=thumb_f, parse_mode="HTML")
                    else:
                        bot.send_video(chat_id, media_f, caption=caption, duration=int(duration), thumb=thumb_f, supports_streaming=True, parse_mode="HTML")
                finally:
                    if thumb_f:
                        thumb_f.close()

            try:
                bot.delete_message(chat_id, status_msg_id)
            except Exception:
                pass

        except Exception as e:
            bot.edit_message_text(f"❌ <b>ডাউনলোড ব্যর্থ হয়েছে:</b>\n<code>{e}</code>", chat_id, status_msg_id, parse_mode="HTML")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

# ----------------- ফাস্ট ডিপেন্ডেন্সি স্ক্যানার ও সিঙ্ক ইনস্টলার -----------------
def scan_python_imports(file_path):
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        code = f.read()

    detected = set()
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    detected.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    detected.add(node.module.split('.')[0])
    except Exception:
        raw_imports = re.findall(r"^(?:from|import)\s+([a-zA-Z0-9_]+)", code, re.MULTILINE)
        detected.update(raw_imports)

    stdlib = {
        'os', 'sys', 'time', 'json', 'sqlite3', 'math', 'random', 're', 'asyncio',
        'threading', 'subprocess', 'datetime', 'urllib', 'shutil', 'logging',
        'pathlib', 'itertools', 'functools', 'collections', 'hashlib', 'base64',
        'traceback', 'typing', 'tempfile', 'socket', 'signal', 'io', 'http'
    }
    
    packages = []
    for pkg in detected - stdlib:
        resolved = PIP_ALIASES.get(pkg, pkg)
        packages.append(resolved)
    
    return sorted(list(set(packages)))

def install_missing_packages_sync(pkg_list):
    needed = [pkg for pkg in pkg_list if pkg not in installed_packages_cache]
    if not needed:
        return True, []
    
    installed = []
    for pkg in needed:
        try:
            res = subprocess.run([sys.executable, "-m", "pip", "install", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
            if res.returncode == 0:
                installed_packages_cache.add(pkg)
                installed.append(pkg)
        except Exception:
            pass
    return True, installed

# ----------------- অপ্টিমাইজড প্রসেস এক্সিকিউশন -----------------
def execute_project_script(user_id: int, project_id: int, file_name: str = None, preserve_restarts: bool = False):
    project = admin.get_project_by_id(project_id)
    if not project:
        return False, "Project not found!"

    p_key = f"{user_id}_{project_id}"
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    os.makedirs(p_dir, exist_ok=True)

    if file_name:
        admin.update_project_main_file(project_id, file_name)
    else:
        file_name = project["main_file"]

    script_path = os.path.join(p_dir, file_name)
    log_path = os.path.join(p_dir, "app.log")

    if not os.path.exists(script_path):
        return False, f"<code>{file_name}</code> ফাইলটি পাওয়া যায়নি!"

    old_restarts = 0
    if p_key in active_processes:
        try:
            if preserve_restarts:
                old_restarts = active_processes[p_key].get("restarts", 0)
            active_processes[p_key]["process"].terminate()
            if "log_handle" in active_processes[p_key]:
                active_processes[p_key]["log_handle"].close()
        except Exception:
            pass

    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(f"\n--- [Bot Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---\n")
    log_file.flush()

    proc = subprocess.Popen(
        [sys.executable, "-u", script_path],
        cwd=p_dir,
        stdout=log_file,
        stderr=subprocess.STDOUT
    )

    active_processes[p_key] = {
        "process": proc,
        "start_time": time.time(),
        "file": file_name,
        "log_path": log_path,
        "log_handle": log_file,
        "user_id": user_id,
        "project_id": project_id,
        "project_name": project["project_name"],
        "restarts": old_restarts,
        "warning_sent": False
    }

    admin.update_project_status(project_id, "RUNNING")
    admin.increment_deploy_count(user_id)
    return True, "Success"

def stop_project_script(user_id: int, project_id: int):
    p_key = f"{user_id}_{project_id}"
    if p_key in active_processes:
        try:
            active_processes[p_key]["process"].terminate()
            active_processes[p_key]["log_handle"].close()
        except Exception:
            pass
        del active_processes[p_key]
    admin.update_project_status(project_id, "STOPPED")
    return True

def restart_all_active_bots():
    count = 0
    for p_key, data in list(active_processes.items()):
        uid = data["user_id"]
        pid = data["project_id"]
        fn = data["file"]
        execute_project_script(uid, pid, fn, preserve_restarts=False)
        count += 1
    return count

# ----------------- স্মার্ট ওয়াচডগ ও অটোমেটিক এক্সপায়ারি অ্যালার্ট -----------------
def hosting_watchdog_thread():
    while True:
        time.sleep(5)
        now = time.time()
        
        for p_key in list(active_processes.keys()):
            data = active_processes.get(p_key)
            if not data:
                continue

            uid = data["user_id"]
            pid = data["project_id"]
            proc = data["process"]
            start_t = data["start_time"]
            u_info = admin.get_user(uid)
            plan = u_info.get("plan", "FREE").upper()

            if proc.poll() is None and (now - start_t) > 60 and data.get("restarts", 0) > 0:
                data["restarts"] = 0

            if plan == "FREE":
                elapsed = now - start_t
                if elapsed >= admin.SYSTEM_CONFIG["free_warning_seconds"] and not data.get("warning_sent", False):
                    data["warning_sent"] = True
                    try:
                        bot.send_message(
                            uid,
                            f"⏳ <b>হোস্টিং সেশন অ্যালার্ট (৩০ মিনিট বাকি)!</b> ⚠️\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🤖 <b>বটের নাম:</b> <code>{data['project_name']}</code>\n"
                            f"⏱️ আপনার ১২ ঘণ্টার ফ্রি সেশনের মেয়াদ আর মাত্র <b>৩০ মিনিট</b> পর শেষ হবে।\n\n"
                            "💡 <b>বট নিরবচ্ছিন্ন সচল রাখতে:</b>\n"
                            "• মেয়াদ শেষ হলে <b>🎛️ Control Panel</b> থেকে রিস্টার্ট করুন (ফ্রি)\n"
                            "• অথবা ২৪/৭ নন-স্টপ হোস্টিং পেতে <b>💎 Upgrade Plan</b> থেকে আপগ্রেড করুন!\n"
                            "━━━━━━━━━━━━━━━━━━━━━━",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                if elapsed >= admin.SYSTEM_CONFIG["max_free_session_seconds"]:
                    try:
                        proc.terminate()
                        data["log_handle"].close()
                    except Exception:
                        pass
                    del active_processes[p_key]
                    admin.update_project_status(pid, "STOPPED")
                    try:
                        bot.send_message(
                            uid,
                            f"⏳ <b>১২ ঘণ্টার ফ্রি হোস্টিং সেশন পূর্ণ হয়েছে!</b>\n"
                            f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "👉 পুনরায় চালু করতে <b>🎛️ Control Panel</b> থেকে Start চাপুন অথবা ২৪/৭ আনলিমিটেড চালাতে <b>💎 Upgrade Plan</b> থেকে আপগ্রেড করুন।",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    continue

            if proc.poll() is not None:
                current_restarts = data.get("restarts", 0) + 1
                data["restarts"] = current_restarts

                if current_restarts <= 3:
                    time.sleep(1)
                    execute_project_script(uid, pid, data["file"], preserve_restarts=True)
                    try:
                        bot.send_message(
                            uid,
                            f"⚠️ <b>বট ক্র্যাশ করেছে:</b> <code>{data['project_name']}</code> স্বয়ংক্রিয়ভাবে রিস্টার্ট করা হয়েছে ({current_restarts}/3)।"
                        )
                    except Exception:
                        pass
                else:
                    last_err = "লগ ফাইলে কোনো বিস্তারিত পাওয়া যায়নি।"
                    try:
                        if os.path.exists(data["log_path"]):
                            with open(data["log_path"], "r", encoding="utf-8", errors="ignore") as lf:
                                lines = lf.readlines()
                                last_err = "".join(lines[-8:]) if lines else "Empty log"
                    except Exception:
                        pass

                    try:
                        bot.send_message(
                            uid,
                            f"🚨 <b>বট ক্র্যাশ অ্যালার্ট (Bot Stopped)!</b> 🔴\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🤖 <b>বটের নাম:</b> <code>{data['project_name']}</code>\n"
                            f"❌ কোডে মারাত্মক ভুলের কারণে ৩ বার রিস্টার্ট চেষ্টার পর বটটি বন্ধ করা হয়েছে।\n\n"
                            f"📜 <b>এরর বিবরণ:</b>\n<pre>{last_err}</pre>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "👉 কোড সংশোধন করে আবার <b>🎛️ Control Panel</b> থেকে Start দিন।",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    admin.update_project_status(pid, "STOPPED")
                    if p_key in active_processes:
                        del active_processes[p_key]

        # পেইড সাবস্ক্রিপশন অ্যালার্ট
        try:
            conn = admin.get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id, username, plan, plan_expiry, expiry_alert_24h, expiry_alert_2h FROM users WHERE plan IN ('PRO', 'PREMIUM', 'BUSINESS', 'VIP')")
            paid_users = cur.fetchall()
            now_dt = datetime.now()

            for pu in paid_users:
                exp_str = pu["plan_expiry"]
                if exp_str and exp_str != "LIFETIME":
                    try:
                        exp_dt = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
                        diff = exp_dt - now_dt
                        
                        if timedelta(hours=0) < diff <= timedelta(hours=24) and not pu["expiry_alert_24h"]:
                            cur.execute("UPDATE users SET expiry_alert_24h = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                f"🔔 <b>সাবস্ক্রিপশন রিনিউয়াল রিমাইন্ডার (২৪ ঘণ্টা বাকি)!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 <b>বর্তমান প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                f"⏳ আপনার প্ল্যানের মেয়াদ শেষ হবে: <code>{exp_str}</code>\n\n"
                                "👉 বট ২৪/৭ সচল রাখতে এখনই <b>💎 Upgrade Plan</b> থেকে রিনিউ করে নিন!",
                                parse_mode="HTML"
                            )

                        elif timedelta(hours=0) < diff <= timedelta(hours=2) and not pu["expiry_alert_2h"]:
                            cur.execute("UPDATE users SET expiry_alert_2h = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                f"⚠️ <b>জরুরি নোটিশ: প্ল্যানের মেয়াদ আর মাত্র ২ ঘণ্টা বাকি!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 <b>প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                "👉 এখনই <b>💎 Upgrade Plan</b> ট্যাপ করে মেয়াদ বাড়িয়ে নিন।",
                                parse_mode="HTML"
                            )

                        elif diff <= timedelta(seconds=0):
                            cur.execute("UPDATE users SET plan = 'FREE', plan_expiry = 'LIFETIME', expiry_alert_24h = 0, expiry_alert_2h = 0 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                "ℹ️ <b>আপনার প্রিমিয়াম সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে।</b>\nএকাউন্টটি ফ্রি প্ল্যানে (১২ ঘণ্টা/সেশন) ফিরিয়ে নেওয়া হয়েছে। ২৪/৭ চালাতে যেকোনো সময় রিনিউ করতে পারবেন।",
                                parse_mode="HTML"
                            )
                    except Exception:
                        pass
            conn.close()
        except Exception:
            pass

threading.Thread(target=hosting_watchdog_thread, daemon=True).start()

# ----------------- বাটন ও ইন্টারফেস বিল্ডার্স -----------------
def make_reply_button(text, style="primary"):
    try:
        return types.KeyboardButton(text, style=style)
    except Exception:
        return types.KeyboardButton(text)

def make_inline_button(text, callback_data=None, url=None, style="primary"):
    try:
        if url:
            return types.InlineKeyboardButton(text, url=url, style=style)
        return types.InlineKeyboardButton(text, callback_data=callback_data, style=style)
    except Exception:
        if url:
            return types.InlineKeyboardButton(text, url=url)
        return types.InlineKeyboardButton(text, callback_data=callback_data)

def add_home_button(markup, extra_text="🏠 Home Menu", extra_callback="go_home"):
    markup.add(make_inline_button(extra_text, callback_data=extra_callback, style="primary"))
    return markup

def build_deploy_success_card(project_id: int, file_name: str):
    proj = admin.get_project_by_id(project_id)
    text = (
        "🚀 <b>PyHost Cloud — Deployment Successful!</b> 🟢\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>বটের নাম:</b> <code>{proj['project_name']}</code>\n"
        f"📄 <b>মেইন ফাইল:</b> <code>{file_name}</code>\n"
        "⚡ <b>স্ট্যাটাস:</b> 🟢 Live & Running\n"
        "📦 <b>ডিপেন্ডেন্সি:</b> <code>All Packages Installed</code>\n"
        "🧠 <b>মেমোরি লিমিট:</b> <code>256 MB</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <i>বটটি পরিচালনা করতে নিচের বাটন ব্যবহার করুন:</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("🎛️ Control Panel", callback_data=f"manage_bot_{project_id}", style="primary"),
        make_inline_button("📜 View Logs", callback_data=f"bot_logs_{project_id}", style="primary")
    )
    markup.add(make_inline_button("⏹️ Stop Bot", callback_data=f"bot_stop_{project_id}", style="danger"))
    return text, markup

def get_user_keyboard(user_id=None):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    b1 = make_reply_button("🎛️ Control Panel", style="success")
    b2 = make_reply_button("🎬 Video Downloader", style="primary")
    b3 = make_reply_button("📁 File Manager", style="primary")
    b4 = make_reply_button("👤 My Account", style="primary")
    b5 = make_reply_button("💎 Upgrade Plan", style="primary")
    b6 = make_reply_button("🤖 Marketplace", style="primary")
    b7 = make_reply_button("📖 Documentation", style="primary")
    b8 = make_reply_button("📡 Support Hub", style="primary")
    
    if user_id and admin.is_admin(user_id):
        b_admin = make_reply_button("👑 Admin Panel", style="danger")
        markup.add(b1, b2, b3, b4, b5, b6, b7, b8, b_admin)
    else:
        markup.add(b1, b2, b3, b4, b5, b6, b7, b8)
    return markup

def get_admin_reply_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    b1 = make_reply_button("📊 Live Telemetry", style="primary")
    b2 = make_reply_button("🤖 Process Tracker", style="primary")
    b3 = make_reply_button("📋 Pending Requests", style="success")
    b4 = make_reply_button("🔄 Restart All Bots", style="primary")
    b5 = make_reply_button("📢 Global Broadcast", style="primary")
    b6 = make_reply_button("👥 User Directory", style="primary")
    b7 = make_reply_button("💎 Grant VIP (Manual)", style="success")
    b8 = make_reply_button("🚫 Ban / Security", style="danger")
    b9 = make_reply_button("💾 Database Backup", style="primary")
    b10 = make_reply_button("🧹 Server Cleanup", style="primary")
    b11 = make_reply_button("⚙️ Maintenance Mode", style="danger")
    b12 = make_reply_button("🔙 User Menu", style="primary")
    markup.add(b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12)
    return markup

def get_multi_bot_control_panel(user_id: int):
    projects = admin.get_user_projects(user_id)
    u_data = admin.get_user(user_id)
    plan = u_data.get("plan", "FREE").upper()
    max_bots = admin.get_max_bot_limit(plan)

    running_count = 0
    for p in projects:
        p_key = f"{user_id}_{p['project_id']}"
        if p_key in active_processes and active_processes[p_key]["process"].poll() is None:
            running_count += 1

    total_count = len(projects)
    stopped_count = total_count - running_count

    text = (
        "🎛️ <b>PYHOST MULTI-BOT CONTROL PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>প্ল্যান:</b> <code>{plan}</code> ({'24/7 Unlimited' if plan != 'FREE' else '12 Hours/Run'})\n"
        f"📦 <b>মোট বট স্লট:</b> <code>{total_count}/{max_bots}</code> টি\n"
        f"├ 🟢 <b>সক্রিয় বট:</b> <code>{running_count}</code> টি\n"
        f"└ 🔴 <b>বন্ধ বট:</b> <code>{stopped_count}</code> টি\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>যেকোনো বট পরিচালনা করতে নিচে নির্বাচন করুন:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in projects:
        pid = p["project_id"]
        p_key = f"{user_id}_{pid}"
        is_run = p_key in active_processes and active_processes[p_key]["process"].poll() is None
        status_icon = "🟢" if is_run else "🔴"
        btn_text = f"{status_icon} {p['project_name']} ({'Running' if is_run else 'Stopped'})"
        markup.add(make_inline_button(btn_text, callback_data=f"manage_bot_{pid}", style="primary"))

    if total_count < max_bots:
        markup.add(make_inline_button("➕ 🤖 Add New Bot", callback_data="add_new_bot", style="success"))
    else:
        markup.add(make_inline_button("💎 Upgrade Plan for More Slots", callback_data="open_upgrade_menu", style="success"))

    add_home_button(markup)
    return text, markup

def get_single_bot_management_markup(user_id: int, project_id: int):
    p_key = f"{user_id}_{project_id}"
    is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None

    markup = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        markup.add(
            make_inline_button("⏹️ Stop Bot", callback_data=f"bot_stop_{project_id}", style="danger"),
            make_inline_button("🔄 Restart Bot", callback_data=f"bot_restart_{project_id}", style="primary")
        )
    else:
        markup.add(
            make_inline_button("▶️ Start Bot", callback_data=f"bot_start_{project_id}", style="success")
        )

    markup.add(
        make_inline_button("📜 View Logs", callback_data=f"bot_logs_{project_id}", style="primary"),
        make_inline_button("📤 Update / Upload File", callback_data=f"bot_update_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("📁 File Manager", callback_data=f"fm_view_{project_id}", style="primary"),
        make_inline_button("🗑️ Delete Bot", callback_data=f"bot_delete_confirm_{project_id}", style="danger")
    )
    markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return markup

def get_file_manager_dashboard(user_id: int, project_id: int):
    project = admin.get_project_by_id(project_id)
    files, total_size = admin.get_project_files_info(user_id, project_id)
    
    file_lines = []
    if files:
        for f in files[:15]:
            sz_kb = max(1, f["size"] // 1024)
            icon = "⭐" if f["name"] == project.get("main_file", "main.py") else "📄"
            file_lines.append(f"{icon} <code>{f['name']}</code> ({sz_kb} KB)")
        if len(files) > 15:
            file_lines.append(f"<i>...এবং আরও {len(files)-15} টি ফাইল</i>")
        files_str = "\n".join(file_lines)
    else:
        files_str = "<i>কোনো ফাইল নেই (ফোল্ডার খালি)</i>"

    total_kb = total_size // 1024
    msg = (
        f"📁 <b>ফাইল ম্যানেজার: {project['project_name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>মেইন স্ক্রিপ্ট:</b> <code>{project['main_file']}</code>\n"
        f"💾 <b>মোট ফাইল সাইজ:</b> <code>{total_kb} KB</code> ({len(files)} টি ফাইল)\n\n"
        f"📋 <b>ফাইল তালিকা:</b>\n{files_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>ফাইল অ্যাকশন নির্বাচন করুন:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("📤 Upload / Replace File", callback_data=f"bot_update_{project_id}", style="primary"),
        make_inline_button("📥 Download Backup (.zip)", callback_data=f"fm_zip_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("🗑️ Delete File", callback_data=f"fm_delfile_{project_id}", style="danger"),
        make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{project_id}", style="primary")
    )
    return msg, markup

def get_my_account_card(user_id: int, user_obj):
    u_data = admin.get_user(user_id)
    projects = admin.get_user_projects(user_id)
    plan = u_data.get("plan", "FREE").upper()
    max_bots = admin.get_max_bot_limit(plan)
    
    active_instances = 0
    for p in projects:
        p_key = f"{user_id}_{p['project_id']}"
        if p_key in active_processes and active_processes[p_key]["process"].poll() is None:
            active_instances += 1

    time_rem = "— (Free Tier)" if plan == "FREE" else str(u_data.get("plan_expiry", "LIFETIME"))
    ref_link = f"https://t.me/{bot.get_me().username}?start=ref_{user_id}"

    card_text = (
        "╭━━━[ 👤 <b>ACCOUNT OVERVIEW</b> ]━━━╮\n"
        f"┣ 🏷 <b>Name:</b> {user_obj.first_name}\n"
        f"┣ 🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"┣ 👑 <b>Plan:</b> <code>{plan}</code>\n"
        f"┣ 🕐 <b>Time Remaining:</b> <code>{time_rem}</code>\n"
        "┣━━━[ 💳 <b>BILLING & REWARDS</b> ]━━━━┫\n"
        f"┣ ☁️ <b>Credits:</b> <code>{u_data.get('credits', 0.0)}</code>\n"
        f"┣ 🎁 <b>Referral Credits:</b> <code>{u_data.get('referral_credits', 0.0)}</code>\n"
        f"┣ 👥 <b>Total Referrals:</b> <code>{u_data.get('referral_count', 0)}</code> Users\n"
        "┣━━━[ 📊 <b>RESOURCE USAGE</b> ]━━━━━┫\n"
        f"┣ 🚀 <b>Active Instances:</b> <code>{active_instances}/{max_bots}</code> Bots\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🔗 <b>আপনার রেফারেল লিংক:</b>\n"
        f"<code>{ref_link}</code>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"),
        make_inline_button("📢 Share Referral", url=f"https://t.me/share/url?url={ref_link}&text=PyHost%20এ%20ফ্রিতে%20Python%20বট%20হোস্ট%20করুন!", style="primary")
    )
    markup.add(make_inline_button("🔄 Refresh Stats", callback_data="refresh_my_account", style="primary"))
    add_home_button(markup)
    return card_text, markup

def render_bot_dashboard(chat_id, message_id, user_id: int, pid: int):
    project = admin.get_project_by_id(pid)
    if not project:
        return

    p_key = f"{user_id}_{pid}"
    is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None
    status_str = "🟢 Running (Active)" if is_running else "🔴 Stopped (Inactive)"

    runtime_str = "0h 0m"
    mem_str = "0 MB"
    cpu_str = "0.0%"
    if is_running:
        elapsed = int(time.time() - active_processes[p_key]["start_time"])
        runtime_str = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
        mem, cpu = admin.get_bot_resource_usage(active_processes[p_key]["process"].pid)
        mem_str = f"{mem} MB"
        cpu_str = f"{cpu}%"

    msg = (
        f"🤖 <b>বট ড্যাশবোর্ড: {project['project_name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>স্ট্যাটাস:</b> <code>{status_str}</code>\n"
        f"📄 <b>মেইন ফাইল:</b> <code>{project['main_file']}</code>\n"
        f"🧠 <b>RAM Usage:</b> <code>{mem_str} / 256 MB</code>\n"
        f"💻 <b>CPU Load:</b> <code>{cpu_str}</code>\n"
        f"⏰ <b>রানিং টাইম:</b> <code>{runtime_str}</code>\n"
        f"📅 <b>তৈরি:</b> <code>{project['created_at']}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        bot.edit_message_text(
            msg, chat_id, message_id,
            reply_markup=get_single_bot_management_markup(user_id, pid),
            parse_mode="HTML"
        )
    except Exception:
        pass

def get_upload_guide_card():
    text = (
        "📤 <b>ফাইল আপলোড বা কোড পেস্ট গাইড</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "আপনি ফাইল পাঠাতে পারেন বা সরাসরি পাইথন কোড চ্যাটে পেস্ট করতে পারেন:\n\n"
        "✅ <code>main.py</code> / <code>bot.py</code> (প্রধান স্ক্রিপ্ট)\n"
        "✅ <code>requirements.txt</code> (অটো-ইনস্টল হবে)\n"
        "✅ <code>.env</code> / <code>.json</code> / <code>.txt</code>\n"
        "✅ <code>.zip</code> (সব ফাইল একসাথে জিপ করে)\n"
        "✅ <b>সরাসরি কোড:</b> চ্যাটে যেকোনো পাইথন কোড পেস্ট করুন!\n\n"
        "<i>এখন আপনার ফাইল বা কোডটি সেন্ড করুন:</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("◀️ Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return text, markup

def get_upgrade_plan_view(user_id: int):
    u_data = admin.get_user(user_id)
    curr_tier = u_data.get("plan", "FREE").upper()
    exp = u_data.get("plan_expiry", "LIFETIME")

    text = (
        "🚀 <b>PyHost Cloud — Premium Plans</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Current Tier:</b> <code>{curr_tier}</code> ({exp})\n\n"
        "⚡ <b>Pro:</b> 24/7 • Ad-Free • 1 Bot • Auto-Renew\n"
        "🌟 <b>Premium:</b> 24/7 • 3 Bots • GitHub Sync • Backup • DDoS Shield\n"
        "👑 <b>Business:</b> 24/7 • 10 Bots • Live Terminal • Max RAM • VIP Support\n\n"
        "👇 <b>Plan select:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    b1 = make_inline_button("⚡ Pro — ৩০৳/সপ্তাহ", callback_data="select_tier_PRO", style="primary")
    b2 = make_inline_button("🌟 Premium — ১০০৳/মাস", callback_data="select_tier_PREMIUM", style="success")
    b3 = make_inline_button("👑 Business Boss — ২৩০৳/মাস", callback_data="select_tier_BUSINESS", style="primary")
    markup.add(b1, b2, b3)
    add_home_button(markup)
    return text, markup

def get_order_summary_view(tier_key: str, discount_percent: int = 0):
    tdata = admin.TIER_PLANS.get(tier_key, admin.TIER_PLANS["PREMIUM"])
    
    price_val = tdata["price_num"]
    if discount_percent > 0:
        price_val = int(price_val * (1 - discount_percent / 100))
        price_text = f"<s>{tdata['price_num']} BDT</s> <b>{price_val} BDT</b> ({discount_percent}% OFF)"
    else:
        price_text = f"{price_val} BDT"

    text = (
        "🛒 <b>Order Summary</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>{tdata['title']}</b> | ⏱️ <b>{tdata['days']} Days</b>\n"
        f"💰 <b>{price_text}</b>\n\n"
        f"✨ <b>Features:</b>\n{tdata['features']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "পেমেন্ট মেথড বেছে নিন:"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = make_inline_button("💳 bKash", callback_data=f"paygw_{tier_key}_bkash_{price_val}", style="primary")
    b2 = make_inline_button("💳 Nagad", callback_data=f"paygw_{tier_key}_nagad_{price_val}", style="primary")
    b3 = make_inline_button("🪙 Crypto Pay (USDT/TRX)", callback_data=f"paygw_{tier_key}_crypto_{price_val}", style="primary")
    b4 = make_inline_button("🎁 Coupon Code", callback_data=f"apply_coupon_{tier_key}", style="success")
    b5 = make_inline_button("◀️ Back", callback_data="open_upgrade_menu", style="danger")
    
    markup.add(b1, b2)
    markup.add(b3)
    markup.add(b4)
    markup.add(b5)
    return text, markup

def get_support_hub_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = make_inline_button("❓ FAQ", callback_data="sup_faq", style="primary")
    b2 = make_inline_button("📢 Official Channel ↗️", url="https://t.me/+6eODSVuNdqUxMTNl", style="primary")
    b3 = make_inline_button("👥 Community ↗️", url="https://t.me/+6eODSVuNdqUxMTNl", style="primary")
    b4 = make_inline_button("💼 Dev Studio", callback_data="sup_devstudio", style="primary")
    b5 = make_inline_button("👨‍💼 Contact Admin ↗️", url="https://t.me/forhadkhandakar", style="success")
    markup.add(b1, b2, b3, b4)
    markup.add(b5)
    return markup

def build_pending_requests_view(page: int = 0):
    pending = admin.get_pending_vip_requests()
    if not pending:
        return "✅ <b>কোনো পেন্ডিং সাবস্ক্রিপশন রিকোয়েস্ট নেই।</b>", None

    items, page, total_pages = admin.paginate(pending, page, per_page=3)

    text_parts = [f"📋 <b>পেন্ডিং রিকোয়েস্ট</b> — মোট <code>{len(pending)}</code> টি (পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"]
    markup = types.InlineKeyboardMarkup(row_width=2)
    for req in items:
        req_id = req["request_id"]
        text_parts.append(
            f"📦 <b>Req #{req_id}</b> | UID: <code>{req['user_id']}</code> (@{req['username']})\n"
            f"Plan: <code>{req['plan_name']}</code> ({req.get('gateway','bKash')})\n"
            f"Price: <code>{req['price']}</code> | TrxID: <code>{req['trx_id']}</code>"
        )
        markup.add(
            make_inline_button(f"✅ Approve #{req_id}", callback_data=f"adm_app_vip_{req_id}", style="success"),
            make_inline_button(f"❌ Reject #{req_id}", callback_data=f"adm_rej_vip_{req_id}", style="danger")
        )

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_reqs_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_reqs_page_{page+1}", style="primary"))
    if nav_row:
        markup.add(*nav_row)

    return "\n\n".join(text_parts), markup

def build_user_directory_view(page: int = 0, query: str = ""):
    users = admin.search_users(query) if query else admin.get_all_users()
    total_all = len(admin.get_all_users())

    if not users:
        return f"👥 <b>কোনো ইউজার পাওয়া যায়নি</b> (মোট রেজিস্টার্ড: <code>{total_all}</code>)।", None

    items, page, total_pages = admin.paginate(users, page, per_page=8)
    header = f"👥 <b>ইউজার ডাইরেক্টরি</b> — মোট <code>{total_all}</code> ক্লায়েন্ট"
    if query:
        header += f"\n🔎 সার্চ ফলাফল: <code>{query}</code> ({len(users)} মিলেছে)"
    header += f"\n(পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"

    markup = types.InlineKeyboardMarkup(row_width=1)
    for u in items:
        ban_icon = "🚫" if u.get("is_banned") else "🟢"
        label = f"{ban_icon} @{u.get('username') or 'N/A'} • {u.get('plan','FREE')} • {u['user_id']}"
        markup.add(make_inline_button(label, callback_data=f"adm_view_user_{u['user_id']}", style="primary"))

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_users_page_{page-1}_{query or '-'}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_users_page_{page+1}_{query or '-'}", style="primary"))
    if nav_row:
        markup.add(*nav_row)

    tip = "\n\n💡 নির্দিষ্ট ইউজার খুঁজতে: <code>/find username_or_id</code>"
    return header + tip, markup

def build_admin_user_profile_card(target_uid: int):
    u = admin.get_user(target_uid)
    projects = admin.get_user_projects(target_uid)
    is_ban = bool(u.get("is_banned", 0))

    card = (
        "👤 <b>ADMIN CLIENT PROFILE CARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"👤 <b>Username:</b> @{u.get('username', 'None')}\n"
        f"💎 <b>Plan:</b> <code>{u.get('plan', 'FREE')}</code> (Exp: <code>{u.get('plan_expiry', 'LIFETIME')}</code>)\n"
        f"📦 <b>Total Bots:</b> <code>{len(projects)}</code>\n"
        f"👥 <b>Referrals:</b> <code>{u.get('referral_count', 0)}</code> Clients\n"
        f"🚫 <b>Status:</b> <code>{'🔴 BANNED' if is_ban else '🟢 ACTIVE'}</code>\n"
        f"📅 <b>Joined:</b> <code>{u.get('joined_date', 'Unknown')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("💎 Grant Pro (7d)", callback_data=f"adm_grant_pro_{target_uid}", style="success"),
        make_inline_button("💎 Grant Prem (30d)", callback_data=f"adm_grant_vip_{target_uid}", style="success")
    )
    markup.add(
        make_inline_button("🚫 Unban" if is_ban else "🚫 Ban", callback_data=f"adm_ban_confirm_{target_uid}", style="danger"),
        make_inline_button("📩 Send DM", callback_data=f"adm_dm_{target_uid}", style="primary")
    )
    return card, markup

# ----------------- স্টার্ট ও কমান্ড হ্যান্ডলারস -----------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    referrer_id = None
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1].replace("ref_", ""))
        except Exception:
            pass

    welcome_text = (
        "☁️ <b>স্বাগতম PyHost Multi-Bot Cloud & Downloader প্ল্যাটফর্মে!</b> 🚀\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>মাল্টি-বট হোস্টিং:</b> একই একাউন্টে একাধিক পাইথন বট একসাথে চালান\n"
        "🎬 <b>ভিডিও ও অডিও ডাউনলোডার:</b> YouTube, FB, Insta, TikTok থেকে ১-ক্লিকে ডাউনলোড\n"
        "📦 <b>স্মার্ট ফাইল আপডেট:</b> সরাসরি ফাইল রিপ্লেস, জিপ ব্যাকআপ ও রিয়েলটাইম লগ\n"
        "🛒 <b>১-ক্লিক মার্কেটপ্লেস:</b> কোনো কোডিং ছাড়াই প্রিমিয়াম রেডিমেড বট চালু করুন\n"
        "🎁 <b>রেফারেল বোনাস:</b> ৩ জন বন্ধুকে ইনভাইট করলেই ৩ দিন VIP ফ্রি!\n\n"
        "👇 <b>শুরু করতে নিচের মেনু বাটনগুলো ব্যবহার করুন:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_user_keyboard(user_id), parse_mode="HTML")

    def _async_reg():
        bonus_ref_id = admin.register_user(user_id, message.from_user.username, referrer_id)
        if bonus_ref_id:
            try:
                bot.send_message(
                    bonus_ref_id,
                    f"🎉 <b>অভিনন্দন!</b> ৩ জন সফল রেফারে আপনি <b>{admin.SYSTEM_CONFIG['referral_bonus_days']} দিনের ফ্রি Premium VIP প্ল্যান</b> বোনাস পেয়েছেন! 🚀",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    threading.Thread(target=_async_reg, daemon=True).start()

@bot.message_handler(commands=['user'])
def cmd_user_admin_search(message):
    user_id = message.from_user.id
    if not admin.is_admin(user_id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ <b>Usage:</b> <code>/user &lt;user_id&gt;</code>")
        return

    try:
        target_uid = int(args[1].strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid User ID!")
        return

    card, markup = build_admin_user_profile_card(target_uid)
    bot.send_message(message.chat.id, card, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=['find'])
def cmd_find_users(message):
    user_id = message.from_user.id
    if not admin.is_admin(user_id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ <b>Usage:</b> <code>/find &lt;username বা user_id&gt;</code>")
        return

    query = args[1].strip()
    text, markup = build_user_directory_view(page=0, query=query)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

# ----------------- টেক্সট, মিডিয়া লিংক ও স্টেট হ্যান্ডলার -----------------
@bot.message_handler(func=lambda msg: True)
def handle_all_text_navigation(message):
    user_id = message.from_user.id
    text = message.text

    u_data = admin.get_user(user_id)
    if u_data.get("is_banned"):
        bot.send_message(message.chat.id, "🚫 <b>আপনার একাউন্টটি ব্যান করা হয়েছে।</b>")
        return

    # স্টেট হ্যান্ডলারস
    if user_id in user_states:
        state_data = user_states[user_id]
        state_name = state_data if isinstance(state_data, str) else state_data.get("state")

        if state_name == "awaiting_broadcast" and admin.is_admin(user_id):
            del user_states[user_id]
            users = admin.get_all_users()
            total = len(users)
            prog = bot.send_message(message.chat.id, f"📢 <b>Broadcast শুরু হচ্ছে ({total} ইউজার)...</b>", parse_mode="HTML")
            
            def _run_broadcast():
                success, failed = 0, 0
                for u in users:
                    try:
                        bot.send_message(u["user_id"], f"📢 <b>PYHOST OFFICIAL ANNOUNCEMENT</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")
                        success += 1
                    except Exception:
                        failed += 1
                    time.sleep(0.04)
                bot.edit_message_text(f"✅ <b>Broadcast সম্পন্ন!</b>\nসফল: <code>{success}</code> | ব্যর্থ: <code>{failed}</code>", message.chat.id, prog.message_id, parse_mode="HTML")

            threading.Thread(target=_run_broadcast, daemon=True).start()
            return

        elif state_name == "awaiting_admin_dm" and admin.is_admin(user_id):
            target_uid = state_data.get("target_uid")
            del user_states[user_id]
            try:
                bot.send_message(target_uid, f"📩 <b>PyHost Support Message from Admin:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_uid}</code>-কে মেসেজ পাঠানো হয়েছে!")
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ মেসেজ পাঠানো যায়নি: {e}")
            return

        elif state_name == "awaiting_vip_id" and admin.is_admin(user_id):
            del user_states[user_id]
            try:
                target_id = int(text.strip())
                admin.set_user_plan(target_id, "PREMIUM", 30)
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_id}</code>-কে Premium (30 Days) দেওয়া হয়েছে!")
                try:
                    bot.send_message(target_id, "🎉 <b>অভিনন্দন! আপনার অ্যাকাউন্টটি Premium প্ল্যানে আপগ্রেড করা হয়েছে।</b> (24/7 Unlimited Run)")
                except Exception:
                    pass
            except ValueError:
                bot.send_message(message.chat.id, "❌ অনুগ্রহ করে সঠিক Telegram User ID দিন।")
            return

        elif state_name == "awaiting_ban_id" and admin.is_admin(user_id):
            del user_states[user_id]
            try:
                target_id = int(text.strip())
                u_info = admin.get_user(target_id)
                new_status = not bool(u_info.get("is_banned", 0))
                admin.set_user_ban(target_id, new_status)
                status_str = "BANNED" if new_status else "UNBANNED"
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_id}</code> স্ট্যাটাস: <b>{status_str}</b>!")
            except ValueError:
                bot.send_message(message.chat.id, "❌ অনুগ্রহ করে সঠিক Telegram User ID দিন।")
            return

        elif state_name == "awaiting_coupon_code":
            tier_key = state_data["tier_key"]
            coupon_input = text.strip().upper()
            del user_states[user_id]

            if coupon_input in admin.COUPONS:
                disc = admin.COUPONS[coupon_input]
                bot.send_message(message.chat.id, f"🎉 <b>কুপন সফল হয়েছে! আপনি {disc}% ডিসকাউন্ট পেয়েছেন!</b>")
                sum_text, sum_markup = get_order_summary_view(tier_key, discount_percent=disc)
                bot.send_message(message.chat.id, sum_text, reply_markup=sum_markup, parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, "❌ <b>অবৈধ বা মেয়াদোত্তীর্ণ কুপন কোড!</b>")
                sum_text, sum_markup = get_order_summary_view(tier_key, discount_percent=0)
                bot.send_message(message.chat.id, sum_text, reply_markup=sum_markup, parse_mode="HTML")
            return

        elif state_name == "awaiting_bot_name":
            bot_name = text.strip()[:30]
            pid = admin.create_user_project(user_id, bot_name)
            user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid}
            card_text, card_markup = get_upload_guide_card()
            bot.send_message(message.chat.id, f"✅ <b>'{bot_name}' প্রজেক্ট তৈরি হয়েছে!</b>\n\n" + card_text, reply_markup=card_markup, parse_mode="HTML")
            return

        elif state_name == "awaiting_file_for_bot" and ("import " in text or "def " in text or "bot =" in text or "print(" in text):
            pid = state_data.get("project_id")
            del user_states[user_id]
            p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
            os.makedirs(p_dir, exist_ok=True)
            main_path = os.path.join(p_dir, "main.py")
            with open(main_path, "w", encoding="utf-8") as cf:
                cf.write(text)
            
            prog_msg = bot.send_message(message.chat.id, "⏳ <b>কোড যাচাই ও লাইব্রেরি স্ক্যান করা হচ্ছে...</b>", parse_mode="HTML")
            detected = scan_python_imports(main_path)
            if detected:
                bot.edit_message_text(f"📦 <b>প্রয়োজনীয় লাইব্রেরি ইনস্টল হচ্ছে ({', '.join(detected)})...</b>", message.chat.id, prog_msg.message_id, parse_mode="HTML")
                install_missing_packages_sync(detected)

            execute_project_script(user_id, pid, "main.py")
            success_card, markup = build_deploy_success_card(pid, "main.py")
            bot.edit_message_text(success_card, message.chat.id, prog_msg.message_id, reply_markup=markup, parse_mode="HTML")
            return

        elif state_name == "awaiting_deploy_token":
            bot_token = text.strip()
            template_type = state_data["template"]
            del user_states[user_id]

            if not re.match(r"^\d{8,10}:[a-zA-Z0-9_-]{35}$", bot_token):
                bot.send_message(message.chat.id, "❌ <b>ভুল বট টোকেন ফরম্যাট!</b> @BotFather থেকে সঠিক টোকেন দিন।", parse_mode="HTML")
                return

            status_msg = bot.send_message(message.chat.id, "⏳ <b>বটের ফাইল তৈরি ও লাইভ পরিবেশ সেটআপ হচ্ছে...</b>", parse_mode="HTML")
            pid = admin.create_user_project(user_id, "Quick Bot")
            p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
            bot_code = f"import telebot\n\nbot = telebot.TeleBot('{bot_token}')\n@bot.message_handler(func=lambda m: True)\ndef echo(m):\n    bot.reply_to(m, 'Bot Active!')\nbot.infinity_polling()\n"
            with open(os.path.join(p_dir, "main.py"), "w", encoding="utf-8") as f:
                f.write(bot_code)

            execute_project_script(user_id, pid, "main.py")
            bot.edit_message_text("🎉 <b>অভিনন্দন! আপনার বট সফলভাবে ডেপ্লয় হয়েছে!</b>", message.chat.id, status_msg.message_id, parse_mode="HTML")
            return

        elif state_name == "awaiting_vip_trx":
            del user_states[user_id]
            trx_id = text.strip()
            plan_tier = state_data["plan_tier"]
            gateway = state_data.get("gateway", "bKash")
            price_text = state_data.get("price", "30 BDT")
            tier_info = admin.TIER_PLANS.get(plan_tier, admin.TIER_PLANS["PREMIUM"])
            days = tier_info["days"]
            plan_name = tier_info["name"]

            req_id = admin.create_vip_request(user_id, message.from_user.username, plan_name, days, price_text, gateway, trx_id)
            bot.send_message(
                message.chat.id,
                f"✅ <b>আপনার TrxID সফলভাবে জমা হয়েছে!</b> (Req #{req_id})\nঅ্যাডমিন ভেরিফাই করলেই প্ল্যান সক্রিয় হবে।",
                parse_mode="HTML"
            )
            return

    # --- সরাসরি কোনো মিডিয়া লিংক আসলে ডাউনলোডার ট্রিগার করা ---
    if is_valid_url(text):
        url = clean_url(text)
        platform = detect_platform(url)
        req_id = str(int(time.time()))[-6:]
        URL_CACHE[req_id] = url

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("🎬 Video (MP4)", callback_data=f"dl_v_{req_id}", style="primary"),
            make_inline_button("🎵 Audio (MP3)", callback_data=f"dl_a_{req_id}", style="primary")
        )
        bot.reply_to(
            message,
            f"🎯 <b>মিডিয়া লিংক শনাক্ত হয়েছে!</b>\n\n🌐 <b>প্ল্যাটফর্ম:</b> {platform}\nকী ফরম্যাটে ডাউনলোড করতে চান?",
            reply_markup=markup,
            parse_mode="HTML"
        )
        return

    # --- অ্যাডমিন বাটন অ্যাকশনস ---
    if text == "👑 Admin Panel" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        bot.send_message(message.chat.id, stats + "\n\n👇 <b>Select an admin control action:</b>", reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")

    elif text == "🔙 User Menu":
        bot.send_message(message.chat.id, "🔙 <b>ইউজার মেনুতে ফিরে আসা হয়েছে:</b>", reply_markup=get_user_keyboard(user_id), parse_mode="HTML")

    elif text == "📊 Live Telemetry" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh Telemetry", callback_data="adm_refresh_telem", style="primary"))
        bot.send_message(message.chat.id, stats, reply_markup=markup, parse_mode="HTML")

    elif text == "🤖 Process Tracker" and admin.is_admin(user_id):
        if not active_processes:
            bot.send_message(message.chat.id, "🤖 No active bot processes running.")
        else:
            lines = []
            for p_key, d in active_processes.items():
                elapsed = int(time.time() - d["start_time"])
                mem, cpu = admin.get_bot_resource_usage(d["process"].pid)
                lines.append(f"• <b>Bot:</b> <code>{d['project_name']}</code> (UID: <code>{d['user_id']}</code>)\n  └ 🧠 {mem} MB | 💻 {cpu}% | ⏱️ {elapsed//3600}h {(elapsed%3600)//60}m")
            bot.send_message(message.chat.id, "🤖 <b>Active Bots Process Directory:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines), parse_mode="HTML")

    elif text == "📋 Pending Requests" and admin.is_admin(user_id):
        req_text, req_markup = build_pending_requests_view(page=0)
        bot.send_message(message.chat.id, req_text, reply_markup=req_markup, parse_mode="HTML")

    elif text == "🔄 Restart All Bots" and admin.is_admin(user_id):
        count = restart_all_active_bots()
        bot.send_message(message.chat.id, f"🔄 সফলভাবে <b>{count}</b> টি বট রিস্টার্ট করা হয়েছে!", parse_mode="HTML")

    elif text == "👥 User Directory" and admin.is_admin(user_id):
        dir_text, dir_markup = build_user_directory_view(page=0)
        bot.send_message(message.chat.id, dir_text, reply_markup=dir_markup, parse_mode="HTML")

    elif text == "📢 Global Broadcast" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_broadcast"
        bot.send_message(message.chat.id, "📢 <b>Broadcast Notice:</b> Type the announcement message to broadcast to all clients:")

    elif text == "💎 Grant VIP (Manual)" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_vip_id"
        bot.send_message(message.chat.id, "💎 <b>Grant VIP:</b> Enter the Telegram User ID to upgrade:")

    elif text == "🚫 Ban / Security" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_ban_id"
        bot.send_message(message.chat.id, "🚫 <b>Ban/Unban:</b> Enter the Telegram User ID to toggle ban status:")

    elif text == "💾 Database Backup" and admin.is_admin(user_id):
        b_path = admin.generate_system_backup()
        with open(b_path, "rb") as bf:
            bot.send_document(message.chat.id, bf, caption="💾 <b>PyHost Full Database Backup</b>")

    elif text == "🧹 Server Cleanup" and admin.is_admin(user_id):
        kb = admin.execute_server_cleanup()
        bot.send_message(message.chat.id, f"🧹 Cleanup finished! <code>{kb} KB</code> of logs & cache purged.", parse_mode="HTML")

    elif text == "⚙️ Maintenance Mode" and admin.is_admin(user_id):
        admin.SYSTEM_CONFIG["maintenance_mode"] = not admin.SYSTEM_CONFIG["maintenance_mode"]
        status = "🔴 ACTIVE (ON)" if admin.SYSTEM_CONFIG["maintenance_mode"] else "🟢 INACTIVE (OFF)"
        bot.send_message(message.chat.id, f"⚙️ Maintenance Mode is now: <b>{status}</b>.", parse_mode="HTML")

    # --- রেগুলার ইউজার বাটনস ---
    elif text == "🎛️ Control Panel":
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.send_message(message.chat.id, cp_text, reply_markup=cp_markup, parse_mode="HTML")

    elif text == "🎬 Video Downloader":
        bot.send_message(
            message.chat.id,
            "🎬 <b>Universal Video & Audio Downloader</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "👉 যেকোনো <b>YouTube, Facebook, Instagram, TikTok</b> বা সরাসরি ভিডিও লিংক এখানে মেসেজ হিসেবে পাঠান।\n"
            "বট আপনাকে সুপার ফাস্ট স্পিডে ভিডিও বা MP3 অডিও ডাউনলোড করে দেবে!",
            parse_mode="HTML"
        )

    elif text == "📁 File Manager":
        projects = admin.get_user_projects(user_id)
        if not projects:
            markup = types.InlineKeyboardMarkup()
            markup.add(make_inline_button("➕ Create Bot & Upload File", callback_data="add_new_bot", style="success"))
            bot.send_message(message.chat.id, "⚠️ আপনার কোনো বট প্রজেক্ট নেই।", reply_markup=markup, parse_mode="HTML")
        else:
            markup = types.InlineKeyboardMarkup(row_width=1)
            for p in projects:
                markup.add(make_inline_button(f"📁 {p['project_name']}", callback_data=f"fm_view_{p['project_id']}", style="primary"))
            markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
            bot.send_message(message.chat.id, "📂 <b>স্মার্ট ফাইল ম্যানেজার:</b> প্রজেক্ট সিলেক্ট করুন:", reply_markup=markup, parse_mode="HTML")

    elif text == "👤 My Account":
        card_text, markup = get_my_account_card(user_id, message.from_user)
        bot.send_message(message.chat.id, card_text, reply_markup=markup, parse_mode="HTML")

    elif text == "💎 Upgrade Plan":
        plan_text, plan_markup = get_upgrade_plan_view(user_id)
        bot.send_message(message.chat.id, plan_text, reply_markup=plan_markup, parse_mode="HTML")

    elif text == "🤖 Marketplace":
        markup = types.InlineKeyboardMarkup(row_width=1)
        b1 = make_inline_button("📦 Premium Group Automator", callback_data="mp_view_group", style="primary")
        b2 = make_inline_button("📦 Advanced File Locker Bot", callback_data="mp_view_locker", style="primary")
        b3 = make_inline_button("📦 Basic Echo Support Bot", callback_data="mp_view_echo", style="primary")
        markup.add(b1, b2, b3)
        add_home_button(markup)
        bot.send_message(message.chat.id, "🛒 <b>PyHost Bot Store</b>\nকোনো কোডিং ছাড়া ১-ক্লিকে বট ডেপ্লয় করুন:", reply_markup=markup, parse_mode="HTML")

    elif text == "📡 Support Hub":
        support_markup = get_support_hub_markup()
        add_home_button(support_markup)
        bot.send_message(message.chat.id, "🛠️ <b>Support Centre:</b> নিচের অপশন বেছে নিন:", reply_markup=support_markup, parse_mode="HTML")

    elif text == "📖 Documentation":
        bot.send_message(message.chat.id, "📖 <b>গাইড:</b>\n1. পাইথন ফাইল বা জিপ পাঠালে বট হোস্ট হবে।\n2. ভিডিও লিংক পাঠালে ডাউনলোড হবে।", parse_mode="HTML")

# ----------------- ফাইল আপলোড ও লাইভ ডেপ্লয়মেন্ট হ্যান্ডলার -----------------
@bot.message_handler(content_types=['document'])
def handle_incoming_documents(message):
    user_id = message.from_user.id
    u_data = admin.get_user(user_id)
    if u_data.get("is_banned"):
        bot.send_message(message.chat.id, "🚫 আপনার অ্যাকাউন্ট ব্যান করা হয়েছে।")
        return

    doc = message.document
    file_name = doc.file_name

    target_pid = None
    if user_id in user_states and isinstance(user_states[user_id], dict):
        if user_states[user_id].get("state") == "awaiting_file_for_bot":
            target_pid = user_states[user_id].get("project_id")

    projects = admin.get_user_projects(user_id)
    if not target_pid:
        if projects:
            target_pid = projects[-1]["project_id"]
        else:
            target_pid = admin.create_user_project(user_id, "My Bot")

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{target_pid}")
    os.makedirs(p_dir, exist_ok=True)
    file_path = os.path.join(p_dir, file_name)

    status_msg = bot.reply_to(message, f"⏳ <b>ফাইল গ্রহণ করা হয়েছে!</b>\n🔍 <code>{file_name}</code> সংরক্ষণ হচ্ছে...", parse_mode="HTML")
    file_info = bot.get_file(doc.file_id)
    downloaded = bot.download_file(file_info.file_path)

    with open(file_path, "wb") as f:
        f.write(downloaded)

    if file_name.endswith(".zip"):
        bot.edit_message_text("📦 <b>ZIP ফাইল আনপ্যাক হচ্ছে...</b>", message.chat.id, status_msg.message_id, parse_mode="HTML")
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(p_dir)
        os.remove(file_path)

        req_file = os.path.join(p_dir, "requirements.txt")
        if os.path.exists(req_file):
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        py_files = [f for f in os.listdir(p_dir) if f.endswith('.py')]
        if py_files:
            main_f = "main.py" if "main.py" in py_files else py_files[0]
            detected = scan_python_imports(os.path.join(p_dir, main_f))
            if detected:
                install_missing_packages_sync(detected)
            execute_project_script(user_id, target_pid, main_f)
            success_card, markup = build_deploy_success_card(target_pid, main_f)
            bot.edit_message_text(success_card, message.chat.id, status_msg.message_id, reply_markup=markup, parse_mode="HTML")
        else:
            bot.edit_message_text("✅ <b>ZIP ফাইল এক্সট্র্যাক্ট হয়েছে!</b>", message.chat.id, status_msg.message_id, parse_mode="HTML")
        return

    if file_name == "requirements.txt":
        bot.edit_message_text("📦 <code>requirements.txt</code> ইনস্টল হচ্ছে...", message.chat.id, status_msg.message_id, parse_mode="HTML")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bot.edit_message_text("✅ <code>requirements.txt</code> সফলভাবে ইনস্টল হয়েছে!", message.chat.id, status_msg.message_id, parse_mode="HTML")
        return

    if file_name.endswith(('.db', '.sqlite', '.sqlite3', '.json', '.env', '.txt')):
        bot.edit_message_text(f"✅ <b>ফাইল ({file_name}) ফোল্ডারে সংরক্ষিত হয়েছে!</b>", message.chat.id, status_msg.message_id, parse_mode="HTML")
        return

    if file_name.endswith('.py'):
        detected_packages = scan_python_imports(file_path)
        if detected_packages:
            install_missing_packages_sync(detected_packages)

        execute_project_script(user_id, target_pid, file_name)
        success_card, markup = build_deploy_success_card(target_pid, file_name)
        bot.edit_message_text(success_card, message.chat.id, status_msg.message_id, reply_markup=markup, parse_mode="HTML")

# ----------------- ইনলাইন কলব্যাক কুয়েরি হ্যান্ডলার -----------------
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    # ডাউনলোডার বাটন হ্যান্ডলার
    if data.startswith("dl_"):
        parts = data.split("_")
        mode_flag = parts[1]
        req_id = parts[2]
        url = URL_CACHE.get(req_id)
        if not url:
            bot.answer_callback_query(call.id, "❌ লিংকটির মেয়াদ শেষ!", show_alert=True)
            return

        mode = "video" if mode_flag == "v" else "audio"
        mode_text = "🎬 ভিডিও" if mode == "video" else "🎵 অডিও"
        msg = bot.edit_message_text(f"⏳ <b>{mode_text} ডাউনলোড শুরু হচ্ছে...</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        threading.Thread(target=execute_media_download, args=(call.message.chat.id, user_id, url, mode, msg.message_id), daemon=True).start()
        bot.answer_callback_query(call.id)
        return

    if data == "go_home":
        if user_id in user_states:
            del user_states[user_id]
        home_markup = types.InlineKeyboardMarkup(row_width=2)
        home_markup.add(
            make_inline_button("🎛️ Control Panel", callback_data="back_to_cp", style="success"),
            make_inline_button("👤 My Account", callback_data="refresh_my_account", style="primary")
        )
        home_markup.add(
            make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="primary"),
            make_inline_button("🤖 Marketplace", callback_data="open_mp_menu", style="primary")
        )
        home_markup.add(make_inline_button("🛠️ Support Hub", callback_data="back_to_support", style="primary"))
        bot.edit_message_text("🏠 <b>PyHost Cloud — Quick Home</b>", call.message.chat.id, call.message.message_id, reply_markup=home_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "back_to_cp":
        if user_id in user_states:
            del user_states[user_id]
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.edit_message_text(cp_text, call.message.chat.id, call.message.message_id, reply_markup=cp_markup, parse_mode="HTML")

    elif data == "add_new_bot":
        projects = admin.get_user_projects(user_id)
        u_data = admin.get_user(user_id)
        plan = u_data.get("plan", "FREE").upper()
        max_bots = admin.get_max_bot_limit(plan)

        if len(projects) >= max_bots:
            bot.answer_callback_query(call.id, "আপনার বটের লিমিট শেষ! প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return

        user_states[user_id] = "awaiting_bot_name"
        bot.send_message(call.message.chat.id, "🤖 <b>নতুন বটের নাম লিখুন:</b>")
        bot.answer_callback_query(call.id)

    elif data.startswith("manage_bot_"):
        pid = int(data.replace("manage_bot_", ""))
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_start_"):
        pid = int(data.replace("bot_start_", ""))
        execute_project_script(user_id, pid, preserve_restarts=False)
        bot.answer_callback_query(call.id, "⚡ বট চালু করা হয়েছে!")
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_stop_"):
        pid = int(data.replace("bot_stop_", ""))
        stop_project_script(user_id, pid)
        bot.answer_callback_query(call.id, "বট থামানো হয়েছে!")
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_restart_"):
        pid = int(data.replace("bot_restart_", ""))
        execute_project_script(user_id, pid, preserve_restarts=False)
        bot.answer_callback_query(call.id, "বট রিস্টার্ট করা হয়েছে!")
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_logs_"):
        pid = int(data.replace("bot_logs_", ""))
        p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
        log_path = os.path.join(p_dir, "app.log")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                logs = f.readlines()
            last_logs = "".join(logs[-25:]) if logs else "লগ ফাইল খালি।"
            bot.send_message(call.message.chat.id, f"📜 <b>লাইভ লগ (শেষ ২৫ লাইন):</b>\n<pre>{last_logs}</pre>")
        else:
            bot.answer_callback_query(call.id, "কোনো লগ পাওয়া যায়নি!", show_alert=True)

    elif data.startswith("bot_update_"):
        pid = int(data.replace("bot_update_", ""))
        user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid}
        card_text, card_markup = get_upload_guide_card()
        bot.edit_message_text(card_text, call.message.chat.id, call.message.message_id, reply_markup=card_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_delete_confirm_"):
        pid = int(data.replace("bot_delete_confirm_", ""))
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ হ্যাঁ, ডিলিট", callback_data=f"bot_delete_{pid}", style="danger"),
            make_inline_button("❌ বাতিল", callback_data=f"manage_bot_{pid}", style="primary")
        )
        bot.edit_message_text("⚠️ <b>আপনি কি নিশ্চিতভাবে এই বটটি মুছে ফেলতে চান?</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_delete_"):
        pid = int(data.replace("bot_delete_", ""))
        stop_project_script(user_id, pid)
        admin.delete_user_project(pid)
        bot.answer_callback_query(call.id, "বট ডিলিট করা হয়েছে!")
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.edit_message_text(cp_text, call.message.chat.id, call.message.message_id, reply_markup=cp_markup, parse_mode="HTML")

    elif data.startswith("fm_view_"):
        pid = int(data.replace("fm_view_", ""))
        msg, markup = get_file_manager_dashboard(user_id, pid)
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("fm_zip_"):
        pid = int(data.replace("fm_zip_", ""))
        bot.answer_callback_query(call.id, "📦 ব্যাকআপ জিপ তৈরি হচ্ছে...")
        zip_file = admin.zip_project_folder(user_id, pid)
        if zip_file and os.path.exists(zip_file):
            with open(zip_file, "rb") as zf:
                bot.send_document(call.message.chat.id, zf, caption="📦 প্রজেক্ট ব্যাকআপ")
        else:
            bot.send_message(call.message.chat.id, "❌ ব্যাকআপ তৈরি হয়নি।")

    elif data == "refresh_my_account":
        card_text, markup = get_my_account_card(user_id, call.from_user)
        try:
            bot.edit_message_text(card_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Updated!")

    elif data == "open_upgrade_menu":
        p_text, p_markup = get_upgrade_plan_view(user_id)
        bot.edit_message_text(p_text, call.message.chat.id, call.message.message_id, reply_markup=p_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("select_tier_"):
        tier = data.replace("select_tier_", "")
        sum_text, sum_markup = get_order_summary_view(tier, discount_percent=0)
        bot.edit_message_text(sum_text, call.message.chat.id, call.message.message_id, reply_markup=sum_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("paygw_"):
        parts = data.split("_")
        tier, gateway, price_val = parts[1], parts[2], parts[3]
        user_states[user_id] = {
            "state": "awaiting_vip_trx",
            "plan_tier": tier,
            "gateway": gateway.upper(),
            "price": f"{price_val} BDT"
        }
        pay_msg = f"🧾 <b>{gateway.upper()} Payment:</b> Send {price_val} BDT to <code>01700000000</code>.\nপাঠানোর পর প্রাপ্ত TrxID লিখুন:"
        bot.send_message(call.message.chat.id, pay_msg, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_app_vip_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_app_vip_", ""))
        req = admin.get_vip_request(req_id)
        if req and req["status"] == "PENDING":
            admin.set_user_plan(req["user_id"], "PREMIUM", req["days"])
            admin.update_vip_request_status(req_id, "APPROVED")
            bot.edit_message_text(f"✅ Approved Request #{req_id}", call.message.chat.id, call.message.message_id)
            bot.send_message(req["user_id"], "🎉 আপনার সাবস্ক্রিপশন চালু হয়েছে!")
        bot.answer_callback_query(call.id)

if __name__ == "__main__":
    print("PyHost Cloud 24/7 Multi-Bot & Downloader Engine initiated...")
    try:
        bot.remove_webhook()
        bot.delete_my_commands()
    except Exception:
        pass

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception:
            time.sleep(3)