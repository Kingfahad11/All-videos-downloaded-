#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 ALL-IN-ONE ULTRA-FAST TELEGRAM DOWNLOADER BOT
 - Screen-matching "USER INFO" Card with Profile Photo
 - Realtime Multi-threaded Super Fast Downloads
 - Admin Alert on New User Join
 - Live Progress Bar & Quality/Audio Selector
================================================================================
"""

import os
import re
import sys
import json
import glob
import time
import uuid
import shutil
import base64
import logging
import sqlite3
import tempfile
import asyncio
import mimetypes
from io import BytesIO
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote

# Third-party libraries
from PIL import Image
import requests
import yt_dlp
import qrcode
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

MAX_FILE_MB = 48
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

# Wispbyte সেফটি কিউ
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)
PENDING_DOWNLOADS = 0
URL_CACHE = {}

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_downloader")

# ============================================================================
# ২. ডাটাবেজ (ইউজার ট্র্যাক করার জন্য)
# ============================================================================
DB_FILE = "users.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, joined_at TEXT)")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("DB Init warning: %s", e)

def register_user(user_id) -> bool:
    """নতুন ইউজার হলে True রিটার্ন করবে"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO users (user_id, joined_at) VALUES (?, ?)", 
                        (user_id, time.strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
            return True
        conn.close()
        return False
    except Exception:
        return False

def count_users() -> int:
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        cnt = cur.fetchone()[0]
        conn.close()
        return cnt
    except Exception:
        return 1

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
# ৩. ইউজার প্রোফাইল কার্ড বিল্ডার (স্ক্রিনশটের মতো)
# ============================================================================
async def send_user_info_card(bot, chat_id, user, is_admin_notify=False):
    """স্ক্রিনশটের ডিজাইনে প্রোফাইল কার্ড সেন্ড করে"""
    uname = f"@{user.username}" if user.username else "None"
    prefix = "🚨 <b>NEW USER NOTIFICATION</b>\n\n" if is_admin_notify else ""
    
    caption = (
        f"{prefix}"
        f"<b>USER INFO</b>\n\n"
        f"👤 <b>FIRST NAME :</b> {user.first_name or 'Unknown'}\n"
        f"🌀 <b>USERNAME :</b> {uname}\n"
        f"🆔 <b>User Id:</b> <code>{user.id}</code>\n"
    )

    profile_url = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 View Profile ↗", url=profile_url)]
    ])

    try:
        photos = await bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count > 0:
            photo_file_id = photos.photos[0][-1].file_id
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
    except Exception:
        pass

    # প্রোফাইল ছবি না থাকলে সাধারণ মেসেজ
    await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

# ============================================================================
# ৪. কিবোর্ড মেনু
# ============================================================================
BTN_STATS = "🌐 Statistics"
BTN_ACCOUNT = "👤 My Account"
BTN_HELP = "ℹ️ Help"
BTN_DEV = "💻 Dev Tools"

def get_main_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_STATS), KeyboardButton(BTN_ACCOUNT)],
        [KeyboardButton(BTN_HELP), KeyboardButton(BTN_DEV)]
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

# ============================================================================
# ৫. ইউটিলিটি, প্রোগ্রেস বার ও লিংক ক্লিনার
# ============================================================================
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

def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\r\n]', "_", name).strip()
    return name[:120] or "downloaded_file"

async def safe_edit_text(message, text, reply_markup=None):
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except (BadRequest, TelegramError):
        pass

class DownloadFailed(Exception):
    pass

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}

# ============================================================================
# ৬. সুপার ফাস্ট yt-dlp ডাউনলোড ইঞ্জিন (স্পিড অপ্টিমাইজড)
# ============================================================================
def _run_ytdlp(url, outtmpl, mode, progress_hook):
    cookie_file = "cookies.txt" if os.path.exists("cookies.txt") else None

    if mode == "audio":
        format_rule = "bestaudio/best"
    else:
        format_rule = (
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

        # ⚡ সুপার ফাস্ট স্পিড সেটিংস ⚡
        "concurrent_fragment_downloads": 8,  # প্যারালাল মাল্টি-থ্রেড ডাউনলোড
        "buffersize": 1048576,               # ১ MB মেমোরি বাফার
        "http_chunk_size": 10485760,         # ১০ MB চাংক সাইজ (রকেট স্পিড)

        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return info, filename

async def _download_via_ytdlp(url, mode, user_id, status_msg):
    loop = asyncio.get_running_loop()
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
                asyncio.run_coroutine_threadsafe(safe_edit_text(status_msg, text), loop)
            except Exception:
                pass

        elif d.get("status") == "finished":
            text = "⚙️ <b>ডাউনলোড শেষ! ফাইল প্রস্তুত করা হচ্ছে...</b>"
            try:
                asyncio.run_coroutine_threadsafe(safe_edit_text(status_msg, text), loop)
            except Exception:
                pass

    try:
        info, filename = await loop.run_in_executor(
            None, _run_ytdlp, url, outtmpl, mode, progress_hook
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
        candidates = [f for f in glob.glob(base + ".*") if not f.endswith(('.webp', '.jpg', '.png', '.part'))]
        if candidates:
            filename = candidates[0]
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise DownloadFailed("ডাউনলোডকৃত ফাইলটি পাওয়া যায়নি।")

    size = os.path.getsize(filename)
    if size > MAX_FILE_BYTES:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise DownloadFailed(f"ফাইলটি খুব বড় ({human_size(size)})! লিমিট {MAX_FILE_MB} MB।")

    thumb_path = None
    for ext in ['.webp', '.jpg', '.png']:
        possible_thumb = os.path.splitext(filename)[0] + ext
        if os.path.exists(possible_thumb):
            try:
                jpg_thumb = os.path.splitext(possible_thumb)[0] + "_thumb.jpg"
                with Image.open(possible_thumb) as img:
                    img.convert("RGB").save(jpg_thumb, "JPEG")
                thumb_path = jpg_thumb
            except Exception:
                thumb_path = possible_thumb
            break

    meta = {
        "title": info.get("title") or os.path.basename(filename),
        "duration": info.get("duration") or 0,
        "width": info.get("width") or 0,
        "height": info.get("height") or 0,
        "thumb_path": thumb_path
    }
    return filename, size, temp_dir, meta

# ============================================================================
# ৭. Google Drive, Dropbox ও Direct Link ইঞ্জিন
# ============================================================================
def is_google_drive_url(url: str) -> bool:
    return "drive.google.com" in url or "docs.google.com" in url

def extract_gdrive_file_id(url: str):
    m = re.search(r"/file/d/([a-zA-Z0-9_-]{10,})|id=([a-zA-Z0-9_-]{10,})", url)
    return m.group(1) or m.group(2) if m else None

def is_dropbox_url(url: str) -> bool:
    return "dropbox.com" in url

def normalize_dropbox_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["dl"] = ["1"]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

def _http_download_sync(url, dest_dir, progress_cb, referer=None):
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if referer:
        headers["Referer"] = referer

    resp = session.get(url, headers=headers, stream=True, timeout=25, allow_redirects=True)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" in content_type and "drive.google.com" in url:
        m = re.search(r"confirm=([0-9A-Za-z_-]+)", resp.text)
        if m:
            resp = session.get(f"{url}&confirm={m.group(1)}", headers=headers, stream=True, timeout=25)
            content_type = resp.headers.get("Content-Type", "")

    if "text/html" in content_type:
        resp.close()
        raise DownloadFailed("লিংকটি সরাসরি মিডিয়া ফাইল নয়।")

    total = int(resp.headers.get("Content-Length", 0) or 0)
    if total and total > MAX_FILE_BYTES:
        resp.close()
        raise DownloadFailed(f"ফাইলটি খুব বড় ({human_size(total)})! লিমিট {MAX_FILE_MB} MB।")

    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8\'\')?\"?([^\";]+)\"?", cd)
    fname = safe_filename(unquote(m.group(1))) if m else safe_filename(os.path.basename(urlparse(url).path) or "file")

    dest_path = os.path.join(dest_dir, fname)
    downloaded = 0
    last_update = {"t": time.time(), "bytes": 0}

    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=262144):  # 256KB Chunks
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if downloaded > MAX_FILE_BYTES:
                f.close()
                raise DownloadFailed(f"ফাইলটি {MAX_FILE_MB} MB লিমিট ছাড়িয়ে গেছে।")

            now = time.time()
            if now - last_update["t"] >= 2.0:
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
        filepath, size = await loop.run_in_executor(None, _http_download_sync, url, temp_dir, progress_cb, referer)
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise DownloadFailed(str(e)) from e

    meta = {"title": os.path.basename(filepath), "duration": 0, "width": 0, "height": 0, "thumb_path": None}
    return filepath, size, temp_dir, meta

async def perform_universal_download(url: str, mode: str, user_id: int, status_msg):
    if is_google_drive_url(url):
        file_id = extract_gdrive_file_id(url)
        if not file_id:
            raise DownloadFailed("Google Drive আইডি পাওয়া যায়নি।")
        direct_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
        await safe_edit_text(status_msg, "🔎 Google Drive থেকে ফাইল ফেচ করা হচ্ছে...")
        return await _download_via_http(direct_url, user_id, status_msg)

    if is_dropbox_url(url):
        direct_url = normalize_dropbox_url(url)
        await safe_edit_text(status_msg, "🔎 Dropbox থেকে ফাইল ফেচ করা হচ্ছে...")
        return await _download_via_http(direct_url, user_id, status_msg, referer=url)

    await safe_edit_text(status_msg, "🔎 মিডিয়া তথ্য সংগ্রহ করা হচ্ছে...")
    try:
        return await _download_via_ytdlp(url, mode, user_id, status_msg)
    except DownloadFailed as e:
        if str(e) != "UNSUPPORTED":
            raise
        await safe_edit_text(status_msg, "🔎 ডিরেক্ট লিঙ্ক ডাউনলোড চেষ্টা করা হচ্ছে...")
        try:
            return await _download_via_http(url, user_id, status_msg)
        except DownloadFailed as e2:
            raise DownloadFailed("লিংকটি ডাউনলোডের উপযুক্ত নয় বা প্রাইভেট।") from e2

# ============================================================================
# ৮. কিউ ও আল্ট্রা-ফাস্ট আপলোড লজিক
# ============================================================================
async def process_media_download(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, url: str, mode: str, status_msg):
    global PENDING_DOWNLOADS
    PENDING_DOWNLOADS += 1

    if DOWNLOAD_SEMAPHORE.locked():
        await safe_edit_text(status_msg, f"⏳ <b>সার্ভার ব্যস্ত...</b>\nলাইনে অবস্থান: <b>{PENDING_DOWNLOADS}</b> নম্বরে। অপেক্ষা করুন...")

    async with DOWNLOAD_SEMAPHORE:
        PENDING_DOWNLOADS -= 1
        temp_dir = None
        try:
            filepath, size, temp_dir, meta = await perform_universal_download(url, mode, user_id, status_msg)

            await safe_edit_text(status_msg, "📤 <b>টেলিগ্রামে আপলোড হচ্ছে...</b>")

            platform = detect_platform(url)
            duration_txt = f"\n⏱ <b>দৈর্ঘ্য:</b> {format_duration(meta['duration'])}" if meta['duration'] else ""
            caption = (
                f"🎬 <b>{meta['title']}</b>\n\n"
                f"🌐 <b>প্ল্যাটফর্ম:</b> {platform}{duration_txt}\n"
                f"📦 <b>সাইজ:</b> {human_size(size)}"
            )
            ext = os.path.splitext(filepath)[1].lower()

            sent = False
            with open(filepath, "rb") as media_file:
                thumb_file = open(meta["thumb_path"], "rb") if meta["thumb_path"] and os.path.exists(meta["thumb_path"]) else None
                try:
                    if mode == "audio" or ext in AUDIO_EXTS:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=media_file,
                            caption=caption,
                            title=meta["title"],
                            duration=int(meta["duration"]),
                            thumbnail=thumb_file,
                            parse_mode=ParseMode.HTML,
                            read_timeout=300,
                            write_timeout=300,
                        )
                        sent = True
                    elif ext in VIDEO_EXTS:
                        try:
                            await context.bot.send_video(
                                chat_id=chat_id,
                                video=media_file,
                                caption=caption,
                                duration=int(meta["duration"]),
                                width=int(meta["width"]) if meta["width"] else None,
                                height=int(meta["height"]) if meta["height"] else None,
                                thumbnail=thumb_file,
                                supports_streaming=True,
                                parse_mode=ParseMode.HTML,
                                read_timeout=300,
                                write_timeout=300,
                            )
                            sent = True
                        except Exception:
                            media_file.seek(0)
                    elif ext in PHOTO_EXTS:
                        await context.bot.send_photo(chat_id=chat_id, photo=media_file, caption=caption, parse_mode=ParseMode.HTML)
                        sent = True

                    if not sent:
                        media_file.seek(0)
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=media_file,
                            caption=caption,
                            thumbnail=thumb_file,
                            parse_mode=ParseMode.HTML,
                            read_timeout=300,
                            write_timeout=300,
                        )
                finally:
                    if thumb_file:
                        thumb_file.close()

            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error("Download Error: %s", e)
            await safe_edit_text(status_msg, f"❌ <b>ডাউনলোড ব্যর্থ হয়েছে:</b>\n{e}")

        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

# ============================================================================
# ৯. ইউজার ইনপুট ও বাটন হ্যান্ডলার
# ============================================================================
async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = register_user(user.id)
    if is_new and user.id != ADMIN_ID:
        try:
            await send_user_info_card(context.bot, ADMIN_ID, user, is_admin_notify=True)
        except Exception:
            pass

    raw_url = update.message.text.strip()
    if not is_valid_url(raw_url):
        await update.message.reply_text("🤔 দয়া করে একটি সঠিক ভিডিও লিংক পাঠান।")
        return

    url = clean_url(raw_url)
    platform = detect_platform(url)

    if is_google_drive_url(url) or is_dropbox_url(url):
        status_msg = await update.message.reply_text("🔎 <b>প্রসেসিং শুরু হচ্ছে...</b>", parse_mode=ParseMode.HTML)
        await process_media_download(context, update.effective_chat.id, user.id, url, "video", status_msg)
        return

    req_id = str(uuid.uuid4())[:8]
    URL_CACHE[req_id] = url

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video (MP4)", callback_data=f"dl:v:{req_id}"),
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"dl:a:{req_id}")
        ]
    ])

    text = (
        f"🎯 <b>লিংক শনাক্ত করা হয়েছে!</b>\n\n"
        f"🌐 <b>প্ল্যাটফর্ম:</b> {platform}\n"
        f"আপনি কীভাবে ডাউনলোড করতে চান?"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def handle_callback_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("dl:"):
        return

    _, mode_flag, req_id = data.split(":")
    url = URL_CACHE.get(req_id)
    if not url:
        await query.edit_message_text("❌ লিংকের মেয়াদ শেষ হয়ে গেছে। লিংকটি আবার পাঠান।")
        return

    mode = "video" if mode_flag == "v" else "audio"
    mode_text = "🎬 ভিডিও" if mode == "video" else "🎵 অডিও"

    status_msg = await query.edit_message_text(f"⏳ <b>{mode_text} রিকোয়েস্ট গ্রহণ করা হয়েছে...</b>", parse_mode=ParseMode.HTML)
    asyncio.create_task(
        process_media_download(context, update.effective_chat.id, update.effective_user.id, url, mode, status_msg)
    )

# ============================================================================
# ১০. বট কমান্ড ও বাটন রাউটার
# ============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = register_user(user.id)

    # ইউজারের প্রোফাইল কার্ড সেন্ড করা (স্ক্রিনশটের মতো)
    await send_user_info_card(context.bot, update.effective_chat.id, user, is_admin_notify=False)

    # অ্যাডমিনকে নোটিফিকেশন দেওয়া
    if is_new and user.id != ADMIN_ID:
        try:
            await send_user_info_card(context.bot, ADMIN_ID, user, is_admin_notify=True)
        except Exception:
            pass

    welcome_text = (
        f"👋 Welcome, <b>{user.first_name}</b>!\n\n"
        "🎬 যেকোনো <b>YouTube, Facebook, Instagram Reels, TikTok</b> বা ভিডিও লিংক পাঠান।\n"
        "সম্পূর্ণ ফ্রীতে সুপার ফাস্ট স্পিডে ডাউনলোড হয়ে যাবে!"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>বট ব্যবহারের নিয়ম:</b>\n\n"
        "১. যেকোনো ভিডিওর শেয়ার লিংক কপি করে পাঠিয়ে দিন।\n"
        "২. <b>Video (MP4)</b> অথবা <b>Audio (MP3)</b> সিলেক্ট করুন।\n"
        "৩. সুপার ফাস্ট ডাউনলোড হয়ে ভিডিও পেয়ে যাবেন।"
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
        return
    encoded = base64.b64encode(" ".join(context.args).encode()).decode()
    await update.message.reply_text(f"🔐 Base64:\n<code>{encoded}</code>", parse_mode=ParseMode.HTML)

async def cmd_b64d(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return
    try:
        decoded = base64.b64decode(" ".join(context.args).encode()).decode()
        await update.message.reply_text(f"🔓 Decoded:\n<code>{decoded}</code>", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("❌ ভুল Base64 টেক্সট।")

async def cmd_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        return
    try:
        parsed = json.loads(raw)
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        await update.message.reply_text(f"<pre>{pretty[:3500]}</pre>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ ভুল JSON: {e}")

async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return
    text = " ".join(context.args)
    img = qrcode.make(text)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    await update.message.reply_photo(photo=buf, caption=f"📱 QR Code: {text[:100]}")

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
# ১১. কিবোর্ড বাটন রাউটার
# ============================================================================
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user

    if text == BTN_STATS:
        total = count_users()
        # স্ক্রিনশটের স্টাইল
        await update.message.reply_text(f"📊 <b>Total members : {total} Users</b>", parse_mode=ParseMode.HTML)
    elif text == BTN_ACCOUNT:
        # স্ক্রিনশটের স্টাইলে অ্যাকাউন্ট কার্ড
        await send_user_info_card(context.bot, update.effective_chat.id, user, is_admin_notify=False)
    elif text == BTN_HELP:
        await cmd_help(update, context)
    elif text == BTN_DEV:
        await cmd_dev(update, context)
    elif is_valid_url(text):
        await handle_url_message(update, context)
    else:
        await update.message.reply_text("🤔 ডাউনলোড করতে সরাসরি কোনো ভিডিও লিংক পাঠান অথবা নিচের বাটন চাপুন।", reply_markup=get_main_keyboard())

# ============================================================================
# ১২. অ্যাপ্লিকেশন এক্সিকিউশন
# ============================================================================
def main():
    init_db()
    logger.info("Bot starting up...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dev", cmd_dev))
    app.add_handler(CommandHandler("b64", cmd_b64))
    app.add_handler(CommandHandler("b64d", cmd_b64d))
    app.add_handler(CommandHandler("json", cmd_json))
    app.add_handler(CommandHandler("qr", cmd_qr))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(handle_callback_choice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()