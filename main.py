#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 ALL-IN-ONE TELEGRAM DOWNLOADER BOT (FULL & COMPLETE PRODUCTION CODE)
 - কোনো VIP বা রেফারেল নেই (সম্পূর্ণ আনলিমিটেড ফ্রী)
 - লাইভ প্রোগ্রেস বার (রিয়েলটাইম শতাংশ, স্পিড, সাইজ)
 - YouTube, Facebook, Instagram, TikTok, Google Drive, Dropbox & Direct Link
================================================================================
"""

import os
import re
import sys
import json
import glob
import time
import base64
import shutil
import logging
import sqlite3
import tempfile
import asyncio
import mimetypes
from io import BytesIO
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote

# Third-party libraries
import requests
import yt_dlp
import qrcode
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, BadRequest

# ============================================================================
# ১. কনফিগারেশন ও টোকেন
# ============================================================================
BOT_TOKEN_HARDCODED = "8960102537:AAHXyvXEKXs8hleb4iRikgNKveTvmLwpo7Q"
ADMIN_ID_HARDCODED = 5504272381

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip() or BOT_TOKEN_HARDCODED.strip()
_ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip() or str(ADMIN_ID_HARDCODED)
ADMIN_ID = int(_ADMIN_ID_RAW)

# টেলিগ্রাম সাধারণ বটের ফাইল পাঠানোর সাইজ লিমিট ৫০ MB
MAX_FILE_MB = 48
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_downloader")
BOT_USERNAME = ""

# ============================================================================
# ২. সাধারণ ডাটাবেজ (ক্র্যাশ-প্রুফ: শুধুমাত্র ব্রডকাস্ট ও ইউজারের জন্য)
# ============================================================================
DB_FILE = "users.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("DB Init bypassed: %s", e)

def register_user(user_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_all_users():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []

# ============================================================================
# ৩. ইউটিলিটি ও ভিজ্যুয়াল প্রোগ্রেস বার
# ============================================================================
URL_REGEX = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

def is_valid_url(text: str) -> bool:
    return bool(URL_REGEX.match(text.strip()))

def human_size(num_bytes):
    if not num_bytes:
        return "0 B"
    num_bytes = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"

def create_bar(percent_float: float) -> str:
    """সুন্দর ভিজ্যুয়াল প্রোগ্রেস বার"""
    filled = int(percent_float / 10)
    filled = max(0, min(10, filled))
    return "█" * filled + "░" * (10 - filled)

def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\r\n]', "_", name).strip()
    return name[:150] or "downloaded_file"

async def safe_edit_text(message, text):
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    except (BadRequest, TelegramError):
        pass

class DownloadFailed(Exception):
    pass

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}

# ============================================================================
# ৪. কিবোর্ড মেনু
# ============================================================================
BTN_HELP = "ℹ️ Help"
BTN_DEV = "💻 Dev Tools"
BTN_STATS = "📊 Stats"

def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_HELP), KeyboardButton(BTN_DEV)]
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton(BTN_STATS)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

# ============================================================================
# ৫. ইঞ্জিন ১: yt-dlp ডাউনলোডার (YouTube, FB, Insta, TikTok ইত্যাদি)
# ============================================================================
def _run_ytdlp(url, outtmpl, progress_hook):
    cookie_file = "cookies.txt" if os.path.exists("cookies.txt") else None

    ydl_opts = {
        # সর্বোচ্চ 720p এবং ৪৮ MB এর মধ্যে সাইজ রাখার চেষ্টা করবে
        "format": (
            "best[filesize<48M]/bestvideo[height<=720][filesize<40M]+bestaudio/"
            "best[height<=720]/best[height<=480]/best"
        ),
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [progress_hook],
        "max_filesize": MAX_FILE_BYTES,
        "socket_timeout": 30,
        "cookiefile": cookie_file,
        "source_address": "0.0.0.0",
        "legacy_server_connect": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"]
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return info, filename

async def _download_via_ytdlp(url, user_id, status_msg):
    loop = asyncio.get_running_loop()
    temp_dir = tempfile.mkdtemp(prefix=f"dl_{user_id}_")
    outtmpl = os.path.join(temp_dir, "%(title).60s.%(ext)s")
    last_edit = {"time": 0.0}

    # লাইভ প্রোগ্রেস হুক
    def progress_hook(d):
        if d.get("status") == "downloading":
            now = time.time()
            if now - last_edit["time"] < 2.5:
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

            text = (
                f"📥 <b>ডাউনলোড হচ্ছে...</b>\n\n"
                f"<code>[{bar}] {percent:.1f}%</code>\n"
                f"📦 সাইজ: <b>{human_size(downloaded)}</b> / <b>{human_size(total)}</b>\n"
                f"⚡ স্পিড: <b>{speed_txt}</b> | ⏳ বাকি: <b>{eta_txt}</b>"
            )
            try:
                asyncio.run_coroutine_threadsafe(safe_edit_text(status_msg, text), loop)
            except Exception:
                pass

        elif d.get("status") == "finished":
            text = "⚙️ <b>ডাউনলোড সম্পন্ন! প্রসেস করা হচ্ছে...</b>"
            try:
                asyncio.run_coroutine_threadsafe(safe_edit_text(status_msg, text), loop)
            except Exception:
                pass

    try:
        info, filename = await loop.run_in_executor(
            None, _run_ytdlp, url, outtmpl, progress_hook
        )
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        err_msg = str(e)
        if "confirm you're not a bot" in err_msg.lower():
            raise DownloadFailed("ইউটিউব আইপি রেস্ট্রিকশন দিয়েছে। (cookies.txt প্রয়োজন)")
        raise DownloadFailed("UNSUPPORTED") from e
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise DownloadFailed(f"এরর: {e}") from e

    if not os.path.exists(filename):
        base, _ = os.path.splitext(filename)
        candidates = glob.glob(base + ".*")
        if candidates:
            filename = candidates[0]
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise DownloadFailed("ডাউনলোডকৃত ফাইলটি খুঁজে পাওয়া যায়নি।")

    size = os.path.getsize(filename)
    if size > MAX_FILE_BYTES:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise DownloadFailed(f"ফাইলটি অনেক বড় ({human_size(size)})! লিমিট {MAX_FILE_MB} MB।")

    title = info.get("title") or os.path.basename(filename)
    return title, filename, size, temp_dir

# ============================================================================
# ৬. ইঞ্জিন ২: Google Drive, Dropbox ও ডিরেক্ট HTTP ডাউনলোডার (লাইভ প্রোগ্রেস সহ)
# ============================================================================
def is_google_drive_url(url: str) -> bool:
    return "drive.google.com" in url or "docs.google.com" in url

def extract_gdrive_file_id(url: str):
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]{10,})",
        r"/document/d/([a-zA-Z0-9_-]{10,})",
        r"/spreadsheets/d/([a-zA-Z0-9_-]{10,})",
        r"/presentation/d/([a-zA-Z0-9_-]{10,})",
        r"/d/([a-zA-Z0-9_-]{10,})",
        r"[?&]id=([a-zA-Z0-9_-]{10,})",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None

def build_gdrive_direct_url(file_id: str, original_url: str) -> str:
    if "/document/d/" in original_url:
        return f"https://docs.google.com/document/d/{file_id}/export?format=pdf"
    if "/spreadsheets/d/" in original_url:
        return f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx"
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"

def is_dropbox_url(url: str) -> bool:
    return "dropbox.com" in url

def normalize_dropbox_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["dl"] = ["1"]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

def _http_download_sync(url, dest_dir, progress_cb, referer=None):
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if referer:
        headers["Referer"] = referer

    resp = session.get(url, headers=headers, stream=True, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    # Google Drive Virus Warning Interstitial Handle
    content_type = resp.headers.get("Content-Type", "")
    if "text/html" in content_type and "drive.google.com" in url:
        m = re.search(r"confirm=([0-9A-Za-z_-]+)", resp.text)
        if m:
            confirm = m.group(1)
            sep = "&" if "?" in url else "?"
            resp = session.get(f"{url}{sep}confirm={confirm}", headers=headers, stream=True, timeout=30)
            content_type = resp.headers.get("Content-Type", "")

    if "text/html" in content_type:
        resp.close()
        raise DownloadFailed("লিংকটি কোনো সরাসরি ফাইল বা মিডিয়া নয় (অথবা প্রাইভেট ফাইল)।")

    total = int(resp.headers.get("Content-Length", 0) or 0)
    if total and total > MAX_FILE_BYTES:
        resp.close()
        raise DownloadFailed(f"ফাইলটি খুব বড় ({human_size(total)})! লিমিট {MAX_FILE_MB} MB।")

    # ফাইল নাম নির্ধারণ
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8\'\')?\"?([^\";]+)\"?", cd)
    if m:
        fname = safe_filename(unquote(m.group(1)))
    else:
        path_name = os.path.basename(urlparse(url).path)
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        fname = safe_filename((path_name if "." in path_name else "file") + ext)

    dest_path = os.path.join(dest_dir, fname)
    downloaded = 0
    last_update = {"t": time.time(), "bytes": 0}

    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=131072):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if downloaded > MAX_FILE_BYTES:
                f.close()
                raise DownloadFailed(f"ফাইলটি {MAX_FILE_MB} MB লিমিট ছাড়িয়ে গেছে।")

            now = time.time()
            if now - last_update["t"] >= 2.5:
                speed = (downloaded - last_update["bytes"]) / (now - last_update["t"])
                last_update["t"] = now
                last_update["bytes"] = downloaded
                progress_cb(downloaded, total, speed)

    return dest_path, downloaded

async def _download_via_http(url, user_id, status_msg, referer=None):
    loop = asyncio.get_running_loop()
    temp_dir = tempfile.mkdtemp(prefix=f"dl_{user_id}_")

    def progress_cb(downloaded, total, speed):
        percent = (downloaded / total * 100) if total > 0 else 0.0
        bar = create_bar(percent)
        speed_txt = f"{human_size(speed)}/s" if speed else "..."
        text = (
            f"📥 <b>ডাউনলোড হচ্ছে...</b>\n\n"
            f"<code>[{bar}] {percent:.1f}%</code>\n"
            f"📦 সাইজ: <b>{human_size(downloaded)}</b> / <b>{human_size(total)}</b>\n"
            f"⚡ স্পিড: <b>{speed_txt}</b>"
        )
        try:
            asyncio.run_coroutine_threadsafe(safe_edit_text(status_msg, text), loop)
        except Exception:
            pass

    try:
        filepath, size = await loop.run_in_executor(
            None, _http_download_sync, url, temp_dir, progress_cb, referer
        )
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise DownloadFailed(str(e)) from e

    title = os.path.basename(filepath)
    return title, filepath, size, temp_dir

# ============================================================================
# ৭. অল-ইন-ওয়ান ডাউনলোড রাউটার
# ============================================================================
async def perform_universal_download(url: str, user_id: int, status_msg):
    if is_google_drive_url(url):
        file_id = extract_gdrive_file_id(url)
        if not file_id:
            raise DownloadFailed("Google Drive ফাইলের আইডি পাওয়া যায়নি।")
        direct_url = build_gdrive_direct_url(file_id, url)
        await safe_edit_text(status_msg, "🔎 Google Drive থেকে ফাইল ফেচ করা হচ্ছে...")
        return await _download_via_http(direct_url, user_id, status_msg)

    if is_dropbox_url(url):
        direct_url = normalize_dropbox_url(url)
        await safe_edit_text(status_msg, "🔎 Dropbox থেকে ফাইল ফেচ করা হচ্ছে...")
        return await _download_via_http(direct_url, user_id, status_msg, referer=url)

    # yt-dlp দিয়ে চেষ্টা করা
    await safe_edit_text(status_msg, "🔎 মিডিয়া তথ্য যাচাই করা হচ্ছে...")
    try:
        return await _download_via_ytdlp(url, user_id, status_msg)
    except DownloadFailed as e:
        if str(e) != "UNSUPPORTED":
            raise
        # Direct HTTP fallback
        await safe_edit_text(status_msg, "🔎 ডিরেক্ট ডাউনলোড চেষ্টা করা হচ্ছে...")
        try:
            return await _download_via_http(url, user_id, status_msg)
        except DownloadFailed as e2:
            raise DownloadFailed("লিংকটি সাপোর্টেড নয় বা ফাইলটি পাবলিক নয়।") from e2

# ============================================================================
# ৮. মেসেজ হ্যান্ডলার ও আপলোড লজিক
# ============================================================================
async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id)

    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text("🤔 ডাউনলোড করতে সরাসরি কোনো ভিডিও বা ছবি লিংক পাঠান।")
        return

    status_msg = await update.message.reply_text("🔎 <b>প্রসেসিং শুরু হচ্ছে...</b>", parse_mode=ParseMode.HTML)

    temp_dir = None
    try:
        title, filepath, size, temp_dir = await perform_universal_download(url, user.id, status_msg)

        await safe_edit_text(status_msg, "📤 <b>টেলিগ্রামে আপলোড করা হচ্ছে...</b>")

        caption = f"🎬 <b>{title}</b>\n📦 <b>সাইজ:</b> {human_size(size)}"
        ext = os.path.splitext(filepath)[1].lower()

        sent = False
        with open(filepath, "rb") as media_file:
            if ext in VIDEO_EXTS:
                try:
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=media_file,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                    )
                    sent = True
                except Exception:
                    media_file.seek(0)
            elif ext in PHOTO_EXTS:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                sent = True
            elif ext in AUDIO_EXTS:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                sent = True

            # ভিডিও সেন্ড ফেইল হলে ডকুমেন্ট হিসেবে নিশ্চিত পাঠানো
            if not sent:
                media_file.seek(0)
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=media_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    read_timeout=300,
                    write_timeout=300,
                )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error("Download Error: %s", e)
        await safe_edit_text(status_msg, f"❌ <b>ব্যর্থ হয়েছে:</b>\n{e}")

    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# ============================================================================
# ৯. ইউজার ও ডেভেলপার কমান্ড হ্যান্ডলার
# ============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id)
    text = (
        f"👋 হ্যালো, <b>{user.first_name}</b>!\n\n"
        "🎬 যেকোনো <b>YouTube, Facebook, Instagram Reels, TikTok</b> বা সরাসরি ভিডিওর লিংক পাঠান।\n"
        "সম্পূর্ণ ফ্রিতে আনলিমিটেড লাইভ স্পিডে ডাউনলোড হয়ে যাবে!"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard(user.id)
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>কীভাবে ব্যবহার করবেন:</b>\n\n"
        "১. যেকোনো ভিডিওর শেয়ার লিংক কপি করে এই বটে পাঠিয়ে দিন।\n"
        "২. সাথে সাথে লাইভ ডাউনলোড প্রোগ্রেস দেখতে পাবেন।\n"
        "৩. টেলিগ্রামের নিয়ম অনুযায়ী সর্বোচ্চ ৪৮ MB সাইজের ফাইল সাপোর্ট করবে।"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_dev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💻 <b>Developer Tools:</b>\n\n"
        "/b64 TEXT - Base64 Encode\n"
        "/b64d TEXT - Base64 Decode\n"
        "/json JSON - Pretty Print JSON\n"
        "/qr TEXT - Generate QR Code"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_b64(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ব্যবহার: /b64 টেক্সট")
        return
    text = " ".join(context.args)
    encoded = base64.b64encode(text.encode()).decode()
    await update.message.reply_text(f"🔐 Base64:\n<code>{encoded}</code>", parse_mode=ParseMode.HTML)

async def cmd_b64d(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ব্যবহার: /b64d টেক্সট")
        return
    try:
        decoded = base64.b64decode(" ".join(context.args).encode()).decode()
        await update.message.reply_text(f"🔓 Decoded:\n<code>{decoded}</code>", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("❌ ভুল Base64 টেক্সট।")

async def cmd_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        await update.message.reply_text('ব্যবহার: /json {"key": "value"}')
        return
    try:
        parsed = json.loads(raw)
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        await update.message.reply_text(f"<pre>{pretty[:3500]}</pre>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ ভুল JSON: {e}")

async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ব্যবহার: /qr টেক্সট")
        return
    text = " ".join(context.args)
    img = qrcode.make(text)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    await update.message.reply_photo(photo=buf, caption=f"📱 QR Code: {text[:100]}")

# ============================================================================
# ১০. অ্যাডমিন কমান্ডস
# ============================================================================
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = get_all_users()
    await update.message.reply_text(f"📊 মোট ইউজার: <b>{len(users)}</b> জন", parse_mode=ParseMode.HTML)

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = update.message.text.partition(" ")[2].strip()
    if not msg:
        await update.message.reply_text("ব্যবহার: /broadcast আপনার মেসেজ")
        return
    users = get_all_users()
    sent = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ {sent} জন ইউজারের কাছে মেসেজ পৌঁছেছে।")

# ============================================================================
# ১১. ফ্রি টেক্সট রাউটার
# ============================================================================
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if text == BTN_HELP:
        await cmd_help(update, context)
    elif text == BTN_DEV:
        await cmd_dev(update, context)
    elif text == BTN_STATS and user_id == ADMIN_ID:
        await cmd_stats(update, context)
    elif is_valid_url(text):
        await handle_url_message(update, context)
    else:
        await update.message.reply_text(
            "🤔 ডাউনলোড করতে সরাসরি কোনো ভিডিও লিংক পাঠান অথবা নিচের বাটন চাপুন।",
            reply_markup=get_main_keyboard(user_id),
        )

# ============================================================================
# ১২. অ্যাপ্লিকেশন শুরু
# ============================================================================
def main():
    init_db()
    logger.info("Bot starting up...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # User & Dev Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dev", cmd_dev))
    app.add_handler(CommandHandler("b64", cmd_b64))
    app.add_handler(CommandHandler("b64d", cmd_b64d))
    app.add_handler(CommandHandler("json", cmd_json))
    app.add_handler(CommandHandler("qr", cmd_qr))

    # Admin Commands
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Message Router (Links and Buttons)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()