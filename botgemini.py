import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from google import genai
from google.genai import types

# ================= КОНФИГУРАЦИЯ =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8792265592:AAEJUxeTbIGMsBOhPdUHJWgFeElPfW4B3pE")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JTSFF6oi6CDtvyBk_-AeSIZzuY_aEa-4rKqatXhtTFMA")
ADMIN_USERNAME = "Belugahgfgh"

BUFFER_WAIT_SECONDS = 5.0
MAX_MESSAGES_IN_BUFFER = 8

# Максимальные лимиты памяти для Gemini 3.6 Flash (окно 1 млн токенов)
MAX_HISTORY_STORAGE = 5000       # Храним до 5000 сообщений в базе без сжатия
CHAT_CONTEXT_LIMIT = 150         # Видит последние 150 сообщений текущего чата
USER_PERSONAL_CONTEXT_LIMIT = 50 # Видит последние 50 личных обменов репликами именно с этим собеседником
CROSS_CHAT_CONTEXT_LIMIT = 80    # Видит до 80 сообщений из других групп и ЛС

GEMINI_MAIN_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
GEMINI_FAST_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]
GEMINI_SMART_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
NANO_BANANA_MODELS = ["gemini-3.1-flash-lite-image", "gemini-nano-banana-2.1", "gemini-3.1-flash-image"]

BOT_ID = int(TELEGRAM_TOKEN.split(":")[0]) if ":" in TELEGRAM_TOKEN else 0
MEMORY_FILE = "bot_memory.json"
CONFIG_FILE = "bot_config.json"
LOG_FILE = "bot_errors.log"

START_TIME = time.time()
nano_banana_free_tier_blocked = False

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

SAFETY_SETTINGS = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
]

message_buffer = {}
buffer_lock = threading.Lock()
memory_lock = threading.Lock()
topic_lock = threading.Lock()
git_lock = threading.Lock()

active_conversations = {}
last_chat_sender = {}
last_chat_media = {}
last_bot_image = {}

ram_msg_timestamps = {}
ram_reply_timestamps = {}
user_generating_lock = set()
enriched_user_ids = set()

GLOBAL_MEMORY = None
GLOBAL_CONFIG = None

minsk_weather_cache = {"text": "прохладно, осенняя погода", "updated_at": 0}

# ================= ТОЧНОЕ ВРЕМЯ, ДАВНОСТЬ СООБЩЕНИЙ И ПОГОДА В МИНСКЕ =================

MONTHS_RU = {
    1: "января (зима)", 2: "февраля (зима)", 3: "марта (весна)", 4: "апреля (весна)",
    5: "мая (весна)", 6: "июня (лето)", 7: "июля (лето)", 8: "августа (лето)",
    9: "сентября (осень)", 10: "октября (осень)", 11: "ноября (осень)", 12: "декабря (зима)",
}
WEEKDAYS_RU = {
    0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
    4: "пятница", 5: "суббота", 6: "воскресенье",
}
WMO_CODES = {
    0: "ясно, без осадков", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "густой туман", 51: "легкая морось", 53: "морось", 55: "сильная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 71: "небольшой снег", 73: "снег",
    75: "сильный снегопад", 80: "ливень", 95: "гроза",
}

def get_minsk_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def format_time_ago(msg_ts, now_ts):
    """Вычисляет точную давность сообщения относительно текущего момента."""
    if not msg_ts:
        return "старая запись (ранее)"
    diff = max(0, int(now_ts - msg_ts))
    if diff < 45:
        return f"ТОЛЬКО ЧТО ({diff} сек. назад)"
    if diff < 3600:
        return f"{diff // 60} мин. назад"
    if diff < 86400:
        h = diff // 3600
        m = (diff % 3600) // 60
        return f"{h} ч. {m} мин. назад"
    days = diff // 86400
    return f"{days} дн. назад"

def format_history_entry_with_time(h, now_dt, now_ts):
    """Форматирует сообщение из истории с точной датой, временем и пометкой 'сколько времени прошло'."""
    today_str = now_dt.strftime("%d.%m.%Y")
    yesterday_str = (now_dt - timedelta(days=1)).strftime("%d.%m.%Y")
    msg_date = h.get("date_str", "")
    msg_time = h.get("time_short", "")
    msg_ts = h.get("ts")

    if msg_date == today_str:
        day_label = f"СЕГОДНЯ {msg_date}"
    elif msg_date == yesterday_str:
        day_label = f"ВЧЕРА {msg_date}"
    elif msg_date:
        day_label = f"ДАТА {msg_date}"
    else:
        day_label = "АРХИВНАЯ ЗАПИСЬ (не сегодня)"

    ago_str = format_time_ago(msg_ts, now_ts)
    stamp = f"[{day_label}, в {msg_time} — {ago_str}]"
    line = f"{stamp} {h.get('user', 'User')}: {h.get('text', '')}"
    if h.get("bot_reply"):
        line += f"\n   ↳ {stamp} Толик ответил: {h['bot_reply']}"
    return line

def get_minsk_live_weather():
    now_ts = time.time()
    if now_ts - minsk_weather_cache["updated_at"] < 1200:
        return minsk_weather_cache["text"]
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast?latitude=53.9006&longitude=27.5590"
            "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m&timezone=Europe%2FMinsk"
        )
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            cur = r.json().get("current", {})
            temp = cur.get("temperature_2m")
            feels = cur.get("apparent_temperature")
            code = cur.get("weather_code", 3)
            wind = cur.get("wind_speed_10m")
            desc = WMO_CODES.get(code, "облачно")
            sign = "+" if temp and temp > 0 else ""
            f_sign = "+" if feels and feels > 0 else ""
            w_str = f"{sign}{temp}°C (ощущается как {f_sign}{feels}°C), {desc}, ветер {wind} км/ч"
            minsk_weather_cache["text"] = w_str
            minsk_weather_cache["updated_at"] = now_ts
            return w_str
    except Exception:
        pass
    return minsk_weather_cache["text"]

def get_minsk_datetime_and_weather_str():
    now = get_minsk_time()
    wday = WEEKDAYS_RU.get(now.weekday(), "")
    month_str = MONTHS_RU.get(now.month, "")
    hour = now.hour
    part_of_day = "ночь" if hour < 6 else ("утро" if hour < 12 else ("день" if hour < 18 else "вечер"))
    weather = get_minsk_live_weather()
    return f"{now.strftime('%H:%M:%S')} ({part_of_day}), {wday}, {now.day} {month_str} {now.year} года. Погода в Минске сейчас: {weather}"

# ================= БАЗА ДАННЫХ И АВТОСОХРАНЕНИЕ В GITHUB =================

def git_autosave_memory():
    def _push():
        with git_lock:
            try:
                os.system('git config --local user.email "bot@github.com" >/dev/null 2>&1')
                os.system('git config --local user.name "Tolik AutoSave" >/dev/null 2>&1')
                os.system('git add bot_memory.json bot_config.json >/dev/null 2>&1')
                os.system('git diff --staged --quiet || (git commit -m "💾 AutoSave Memory" >/dev/null 2>&1 && git pull --rebase -X theirs >/dev/null 2>&1 && git push >/dev/null 2>&1)')
            except Exception:
                pass
    threading.Thread(target=_push, daemon=True).start()

def load_config():
    global GLOBAL_CONFIG
    with memory_lock:
        if GLOBAL_CONFIG is not None:
            return GLOBAL_CONFIG
        default_cfg = {
            "main_chat_id": None, "diary_topic_id": None, "admin_user_id": None,
            "last_diary_date": "", "current_plan": "Планов пока нет.",
            "known_chats": {}, "selfies": [], "stickers": {},
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    default_cfg.update(json.load(f))
            except Exception:
                pass
        GLOBAL_CONFIG = default_cfg
        return GLOBAL_CONFIG

def save_config(config=None, push_git=False):
    global GLOBAL_CONFIG
    with memory_lock:
        if config is not None:
            GLOBAL_CONFIG = config
        if GLOBAL_CONFIG is not None:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(GLOBAL_CONFIG, f, ensure_ascii=False, indent=2)
    if push_git:
        git_autosave_memory()

def load_memory():
    global GLOBAL_MEMORY
    with memory_lock:
        if GLOBAL_MEMORY is not None:
            return GLOBAL_MEMORY
        default_mem = {"history": [], "users": {}, "ignored_users": {}, "eternal_facts": []}
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    default_mem.update(json.load(f))
            except Exception:
                pass
        GLOBAL_MEMORY = default_mem
        return GLOBAL_MEMORY

def save_memory(memory=None, push_git=False):
    global GLOBAL_MEMORY
    with memory_lock:
        if memory is not None:
            GLOBAL_MEMORY = memory
        if GLOBAL_MEMORY is not None:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(GLOBAL_MEMORY, f, ensure_ascii=False, indent=2)
    if push_git:
        git_autosave_memory()

def get_chat_display_name(chat_id):
    if not chat_id or chat_id > 0:
        return "ЛС"
    cfg = load_config()
    c_info = cfg.get("known_chats", {}).get(str(chat_id), {})
    title = c_info.get("title")
    return f"Группа «{title}»" if title else f"Группа ({chat_id})"

def find_canonical_user_key(memory, user_name, user_id=None):
    users = memory.setdefault("users", {})
    if user_name in users:
        return user_name
    if user_id:
        for k, v in users.items():
            if v.get("user_id") == user_id:
                return k
    u_low = (user_name or "").lower().lstrip("@")
    for k, v in users.items():
        if k.lower() == u_low or (v.get("username") or "").lower() == u_low:
            return k
    return user_name

def find_user_by_topic_id(memory, thread_id):
    if not thread_id:
        return None, None
    for k, v in memory.get("users", {}).items():
        if v.get("topic_id") == thread_id:
            return k, v.get("user_id")
    return None, None

def safe_request(url, json_data=None, method="POST"):
    try:
        r = requests.post(url, json=json_data, timeout=15) if method == "POST" else requests.get(url, timeout=15)
        return r.json()
    except Exception as e:
        logging.error(f"TG API Error ({url}): {e}")
        return {"ok": False}

def fetch_tg_user_bio_and_names(user_id):
    if not user_id or user_id in enriched_user_ids:
        return {}
    enriched_user_ids.add(user_id)
    res = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat?chat_id={user_id}", method="GET")
    if res.get("ok") and "result" in res:
        r = res["result"]
        return {
            "first_name": r.get("first_name", ""),
            "last_name": r.get("last_name", ""),
            "username": r.get("username", ""),
            "bio": r.get("bio", ""),
        }
    return {}

def register_user_metadata(memory, user_obj, chat_id, custom_title=""):
    if not user_obj or user_obj.get("id") == BOT_ID:
        return None
    u_id = user_obj.get("id")
    username = user_obj.get("username") or ""
    first_name = user_obj.get("first_name") or ""
    last_name = user_obj.get("last_name") or ""
    raw_name = username if username else (first_name if first_name else f"User_{u_id}")

    if username.lower() == ADMIN_USERNAME.lower():
        cfg = load_config()
        if cfg.get("admin_user_id") != u_id:
            cfg["admin_user_id"] = u_id
            save_config()

    existing_key = find_canonical_user_key(memory, raw_name, u_id)
    if existing_key in memory["users"] and username and existing_key != username:
        old_data = memory["users"].pop(existing_key)
        memory["users"].setdefault(username, {}).update(old_data)
        key_name = username
    else:
        key_name = existing_key

    u_data = memory["users"].setdefault(key_name, {"profile": "Пока мало данных", "msg_count": 0, "tolik_msg_count": 0})
    u_data["user_id"] = u_id
    if username:
        u_data["username"] = username
    if first_name:
        u_data["first_name"] = first_name
    if last_name:
        u_data["last_name"] = last_name
    if custom_title:
        u_data["custom_title"] = custom_title

    extra = fetch_tg_user_bio_and_names(u_id)
    if extra.get("first_name"):
        u_data["first_name"] = extra["first_name"]
    if extra.get("last_name"):
        u_data["last_name"] = extra["last_name"]
    if extra.get("bio"):
        u_data["bio"] = extra["bio"]

    chats = u_data.setdefault("chats", [])
    if chat_id and chat_id not in chats:
        chats.append(chat_id)
    return key_name

def sync_chat_admins(chat_id, memory):
    if not chat_id or chat_id > 0:
        return
    res = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatAdministrators?chat_id={chat_id}", method="GET")
    if res.get("ok"):
        for adm in res.get("result", []):
            register_user_metadata(memory, adm.get("user", {}), chat_id, custom_title=adm.get("custom_title", ""))

# ================= УПРАВЛЕНИЕ ЧАТАМИ, ССЫЛКАМИ И ОДОБРЕНИЕМ =================

def fetch_chat_invite_link(chat_id, chat_obj=None):
    if chat_obj and chat_obj.get("username"):
        return f"https://t.me/{chat_obj['username']}"
    res = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat?chat_id={chat_id}", method="GET")
    if res.get("ok") and "result" in res:
        r = res["result"]
        if r.get("username"):
            return f"https://t.me/{r['username']}"
        if r.get("invite_link"):
            return r["invite_link"]
    exp = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/exportChatInviteLink", {"chat_id": chat_id})
    if exp.get("ok") and exp.get("result"):
        return exp["result"]
    cr = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createChatInviteLink", {"chat_id": chat_id})
    if cr.get("ok") and cr.get("result", {}).get("invite_link"):
        return cr["result"]["invite_link"]
    return "Нет прав админа для создания ссылки (приватный чат)"

def check_and_register_group_chat(chat_obj):
    chat_id = chat_obj.get("id")
    if not chat_id or chat_id > 0:
        return True
    cfg = load_config()
    if chat_id == cfg.get("main_chat_id"):
        return False

    cid_str = str(chat_id)
    known = cfg.setdefault("known_chats", {})
    title = chat_obj.get("title") or f"Чат {chat_id}"

    if cid_str not in known:
        invite_link = fetch_chat_invite_link(chat_id, chat_obj)
        known[cid_str] = {
            "title": title,
            "invite_link": invite_link,
            "status": "pending",
            "added_at": get_minsk_time().strftime("%d.%m.%Y %H:%M"),
        }
        save_config(push_git=True)
        notify_admin_chat_approval(chat_id, title, invite_link)
        return False

    known[cid_str]["title"] = title
    if known[cid_str].get("invite_link", "").startswith("Нет прав"):
        new_link = fetch_chat_invite_link(chat_id, chat_obj)
        if not new_link.startswith("Нет прав"):
            known[cid_str]["invite_link"] = new_link
            save_config()

    return known[cid_str].get("status") == "approved"

def notify_admin_chat_approval(chat_id, title, invite_link):
    cfg = load_config()
    text = (
        f"🔔 <b>Толика добавили в новый чат!</b>\n\n"
        f"🏷 <b>Название:</b> {title}\n"
        f"🆔 <b>ID чата:</b> <code>{chat_id}</code>\n"
        f"🔗 <b>Ссылка:</b> {invite_link}\n\n"
        f"Разрешить Толику отвечать в этом чате?"
    )
    kb = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить (Отвечать всем)", "callback_data": f"chat_approve:{chat_id}"},
                {"text": "🔇 Отказать (Молчать)", "callback_data": f"chat_mute:{chat_id}"},
            ],
            [{"text": "👑 Открыть Панель Владельца", "callback_data": "panel:main"}],
        ]
    }
    admin_id = cfg.get("admin_user_id")
    main_chat = cfg.get("main_chat_id")
    if admin_id:
        send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
    if main_chat and main_chat != admin_id:
        send_message(main_chat, text, parse_mode="HTML", reply_markup=kb)

# ================= ЗАЩИТА ОТ СПАМА И ФЛУДА =================

def check_flood_on_incoming(user_id):
    now = time.time()
    memory = load_memory()
    ignore_list = memory.get("ignored_users", {})
    if str(user_id) in ignore_list:
        if now < ignore_list[str(user_id)]:
            return "MUTED"
        ignore_list.pop(str(user_id), None)
        save_memory()

    times = ram_msg_timestamps.setdefault(user_id, [])
    times.append(now)
    ram_msg_timestamps[user_id] = [t for t in times if now - t < 12]

    if len(ram_msg_timestamps[user_id]) > 9:
        memory.setdefault("ignored_users", {})[str(user_id)] = now + 120
        save_memory()
        with buffer_lock:
            for k in [k for k in message_buffer if k.endswith(f"_{user_id}")]:
                message_buffer.pop(k, None)
        return "TRIGGER_MUTE"
    return "OK"

def check_reply_rate_spam(user_id):
    now = time.time()
    r_times = ram_reply_timestamps.setdefault(user_id, [])
    r_times.append(now)
    ram_reply_timestamps[user_id] = [t for t in r_times if now - t < 25]
    return len(ram_reply_timestamps[user_id]) > 4

# ================= ПОИСК В ИНТЕРНЕТЕ =================

def search_web(query, max_results=3):
    results = []
    try:
        w_url = f"https://ru.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&utf8=&format=json&srlimit=2"
        wr = requests.get(w_url, headers={"User-Agent": "TolikBot/1.0"}, timeout=8)
        if wr.status_code == 200:
            for item in wr.json().get("query", {}).get("search", []):
                clean_s = re.sub(r"<.*?>", "", item.get("snippet", "")).strip()
                if clean_s:
                    results.append(f"{item.get('title')}: {clean_s}")
    except Exception:
        pass

    try:
        fallback_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        rf = requests.get(fallback_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if rf.status_code == 200:
            snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', rf.text, re.DOTALL)
            for s in snippets[:max_results]:
                cl = re.sub(r"<.*?>", "", s).strip()
                if cl:
                    results.append(cl)
    except Exception:
        pass
    return "\n".join(results[:max_results]) if results else ""

# ================= TELEGRAM API И МЕДИА =================

def send_message(chat_id, text, message_thread_id=None, reply_to_message_id=None, parse_mode=None, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if message_thread_id:
        data["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = reply_markup
    return safe_request(url, data)

def edit_message_text(chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return safe_request(url, data)

def answer_callback_query(callback_query_id, text="", show_alert=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    return safe_request(url, {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert})

def send_photo(chat_id, photo, caption="", message_thread_id=None, reply_to_message_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    data = {"chat_id": chat_id, "caption": caption}
    if message_thread_id:
        data["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    try:
        if isinstance(photo, bytes):
            r = requests.post(url, data=data, files={"photo": ("image.jpg", photo)}, timeout=35)
        else:
            data["photo"] = photo
            r = requests.post(url, json=data, timeout=15)
        return r.json().get("ok", False)
    except Exception:
        return False

def send_sticker(chat_id, sticker_id, message_thread_id=None, reply_to_message_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendSticker"
    data = {"chat_id": chat_id, "sticker": sticker_id}
    if message_thread_id:
        data["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    safe_request(url, data)

def create_forum_topic(chat_id, name):
    res = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createForumTopic", {"chat_id": chat_id, "name": name})
    return res["result"]["message_thread_id"] if res.get("ok") else None

def get_file_bytes(file_id, expected_mime="image/jpeg"):
    try:
        r = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}", method="GET")
        if not r.get("ok"):
            return None, None
        file_path = r["result"]["file_path"]
        file_data = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=35).content
        ext = file_path.lower().split(".")[-1]
        mime = (
            "audio/ogg" if ext in ["oga", "ogg"]
            else "video/mp4" if ext in ["mp4", "mov", "webm"]
            else "image/webp" if ext == "webp"
            else "image/png" if ext == "png"
            else expected_mime
        )
        return (file_data, mime) if len(file_data) <= 20 * 1024 * 1024 else (None, None)
    except Exception:
        return None, None

def download_video_or_thumb(vid_obj):
    if vid_obj.get("file_size", 0) <= 20 * 1024 * 1024:
        v_bytes, v_mime = get_file_bytes(vid_obj["file_id"], "video/mp4")
        if v_bytes:
            return v_bytes, v_mime, False
    thumb = vid_obj.get("thumbnail") or vid_obj.get("thumb")
    if thumb and thumb.get("file_id"):
        t_bytes, t_mime = get_file_bytes(thumb["file_id"], "image/jpeg")
        if t_bytes:
            return t_bytes, t_mime, True
    return None, None, False

def download_sticker_visual(st):
    if not st.get("is_animated") and not st.get("is_video"):
        s_bytes, s_mime = get_file_bytes(st["file_id"], "image/webp")
        if s_bytes:
            return s_bytes, s_mime
    thumb = st.get("thumbnail") or st.get("thumb")
    if thumb and thumb.get("file_id"):
        t_bytes, t_mime = get_file_bytes(thumb["file_id"], "image/webp")
        if t_bytes:
            return t_bytes, t_mime
    if st.get("is_video"):
        v_bytes, _ = get_file_bytes(st["file_id"], "video/mp4")
        if v_bytes:
            return v_bytes, "video/mp4"
    return None, None

# ================= ГЕНЕРАЦИЯ КАРТИНОК И СТИКЕРЫ =================

def generate_image_nano_banana(prompt, reference_images=None):
    global nano_banana_free_tier_blocked
    img_refs = [(b, m) for (b, m) in (reference_images or []) if m and m.startswith("image/")]

    if not nano_banana_free_tier_blocked:
        contents = [types.Part.from_bytes(data=b, mime_type=m or "image/jpeg") for b, m in img_refs if b] + [prompt]
        for model_name in NANO_BANANA_MODELS:
            try:
                response = gemini_client.models.generate_content(
                    model=model_name, contents=contents, config=types.GenerateContentConfig(safety_settings=SAFETY_SETTINGS)
                )
                parts = getattr(response, "parts", None) or (
                    response.candidates[0].content.parts if response.candidates and response.candidates[0].content else []
                )
                for part in parts or []:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        raw = part.inline_data.data
                        return base64.b64decode(raw) if isinstance(raw, str) else raw
            except Exception as e:
                if "free_tier" in str(e).lower() and "limit: 0" in str(e).lower():
                    nano_banana_free_tier_blocked = True
                    break
                continue

    final_flux_prompt = prompt
    if img_refs:
        try:
            v_contents = [types.Part.from_bytes(data=b, mime_type=m or "image/jpeg") for b, m in img_refs if b] + [
                f"Write a detailed English image prompt recreating this image while applying: '{prompt}'. Output ONLY the English prompt."
            ]
            for v_model in GEMINI_FAST_MODELS:
                try:
                    v_resp = gemini_client.models.generate_content(
                        model=v_model, contents=v_contents,
                        config=types.GenerateContentConfig(temperature=0.4, max_output_tokens=250, safety_settings=SAFETY_SETTINGS),
                    )
                    if v_resp and v_resp.text:
                        final_flux_prompt = v_resp.text.strip()
                        break
                except Exception:
                    continue
        except Exception:
            pass

    try:
        print(f"🎨 [ГЕНЕРАЦИЯ ФОТО]: Промпт Толика -> {final_flux_prompt}")
        enc = urllib.parse.quote(final_flux_prompt)
        seed = random.randint(1000, 99999999)
        for flux_model in ["flux", "turbo"]:
            url = f"https://image.pollinations.ai/prompt/{enc}?model={flux_model}&width=1024&height=1024&nologo=true&seed={seed}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
    except Exception:
        pass
    return None

def analyze_sticker(img_data, mime_type):
    for model_name in GEMINI_FAST_MODELS:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=img_data, mime_type=mime_type),
                    "Опиши эмоцию, действие или мем на стикере 1-3 словами на русском со строчной буквы без точек.",
                ],
                config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=50, safety_settings=SAFETY_SETTINGS),
            )
            if response and response.text:
                return response.text.strip().lower().rstrip(".")
        except Exception:
            continue
    return "неизвестно"

# ================= GEMINI 3.6 FLASH: МЫШЛЕНИЕ, ДИАЛОГ, ДОСЬЕ =================

def transcribe_media_gemini(media_bytes, mime_type="audio/ogg", is_video_note=False):
    prompt = (
        "Это видеокружочек. Напиши кратко: 1) что человек говорит (дословно), 2) что видно на видео и эмоцию."
        if is_video_note
        else "Распознай речь из голосового сообщения максимально точно на русском. Выведи только текст."
    )
    for model_name in GEMINI_FAST_MODELS:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=[types.Part.from_bytes(data=media_bytes, mime_type=mime_type), prompt],
                config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=300, safety_settings=SAFETY_SETTINGS),
            )
            if response and response.text:
                return response.text.strip()
        except Exception:
            continue
    return None

def generate_main_dialogue(prompt_text, system_instruction, image_parts=None, retries=2):
    contents = [types.Part.from_bytes(data=b, mime_type=m or "image/jpeg") for b, m in (image_parts or []) if b] + [prompt_text]
    for attempt in range(retries):
        for model_name in GEMINI_MAIN_MODELS:
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.75,
                        max_output_tokens=900,
                        safety_settings=SAFETY_SETTINGS,
                    ),
                )
                if response and response.text:
                    raw_reply = response.text.strip()
                    thought_match = re.search(r"\[THOUGHT:\s*(.*?)\]", raw_reply, flags=re.DOTALL | re.IGNORECASE)
                    if thought_match:
                        print(f"💭 [МЫСЛИ ТОЛИКА ({model_name})]: {thought_match.group(1).strip()}")
                        raw_reply = re.sub(r"\[THOUGHT:\s*.*?\]", "", raw_reply, flags=re.DOTALL | re.IGNORECASE).strip()
                    print(f"✨ [ОТВЕТ ТОЛИКА ({model_name})]: {raw_reply}")
                    return raw_reply
            except Exception as e:
                if "blocked" in str(e).lower() or "safety" in str(e).lower():
                    return "ты че? я такое делать не буду."
                continue
        time.sleep(2)
    return "блин, инет лагает... сорян"

def generate_smart_summary(prompt_text, system_instruction, retries=2):
    for _ in range(retries):
        for model_name in GEMINI_SMART_MODELS:
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt_text,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction, temperature=0.25, max_output_tokens=800, safety_settings=SAFETY_SETTINGS
                    ),
                )
                if response and response.text:
                    return response.text.strip()
            except Exception:
                continue
        time.sleep(1)
    return None

# ================= СИСТЕМА ДОСЬЕ И ЛОГОВ ПО КОНКРЕТНЫМ ЧАТАМ =================

def get_or_create_user_topic(main_chat_id, user_name, user_id):
    if not main_chat_id:
        return None
    with topic_lock:
        memory = load_memory()
        canon_key = find_canonical_user_key(memory, user_name, user_id)
        u_data = memory["users"].setdefault(canon_key, {})
        if user_id:
            u_data["user_id"] = user_id

        topic_id = u_data.get("topic_id")
        if not topic_id and user_id:
            for k, v in memory["users"].items():
                if v.get("user_id") == user_id and v.get("topic_id"):
                    topic_id = v["topic_id"]
                    u_data["topic_id"] = topic_id
                    break

        if not topic_id:
            new_topic_id = create_forum_topic(main_chat_id, f"{canon_key} ({user_id})")
            if new_topic_id:
                u_data["topic_id"] = new_topic_id
                save_memory(push_git=True)
                send_message(
                    main_chat_id,
                    f"📁 Создано досье для <b>@{canon_key}</b> (ID: <code>{user_id}</code>).\n"
                    f"💡 <i>Написать ему в ЛС от лица Толика:</i> <code>/send текст</code>",
                    message_thread_id=new_topic_id,
                    parse_mode="HTML",
                )
                return new_topic_id
        return topic_id

def log_dialogue_to_topic(user_name, user_id, user_text, bot_reply, chat_title):
    config = load_config()
    main_chat_id = config.get("main_chat_id")
    if not main_chat_id:
        return
    topic_id = get_or_create_user_topic(main_chat_id, user_name, user_id)
    if not topic_id:
        return
    now_str = get_minsk_time().strftime("%d.%m %H:%M")
    log_msg = f"🕒 <i>[{now_str}]</i> 💬 <b>{user_name} ({chat_title}):</b> {user_text}"
    if bot_reply:
        log_msg += f"\n🦊 <b>Толик:</b> {bot_reply}"
    res = send_message(main_chat_id, log_msg, message_thread_id=topic_id, parse_mode="HTML")
    if not res.get("ok") and "thread" in str(res.get("description", "")).lower():
        memory = load_memory()
        canon_key = find_canonical_user_key(memory, user_name, user_id)
        memory["users"].get(canon_key, {}).pop("topic_id", None)
        save_memory()

def update_dossier(user_name, user_id=None, force=False):
    memory = load_memory()
    canon_key = find_canonical_user_key(memory, user_name, user_id)
    user_history = [h for h in memory["history"] if h.get("user", "").lower() == canon_key.lower()]
    if not user_history:
        return

    now_dt = get_minsk_time()
    now_ts = time.time()
    lines = [format_history_entry_with_time(h, now_dt, now_ts) for h in user_history[-100:]]

    u_info = memory.get("users", {}).get(canon_key, {})
    old_profile = u_info.get("profile", "Пока мало данных")
    if len(old_profile) < 35:
        old_profile = "Пока мало данных"

    fn = f"{u_info.get('first_name', '')} {u_info.get('last_name', '')}".strip()
    bio = u_info.get("bio", "")
    meta_str = f"Ник: @{canon_key} | Имя в ТГ: {fn or 'не указано'} | О себе (bio): {bio or 'нет'}"

    prompt = (
        f"Данные профиля: {meta_str}\n\n"
        f"ПРЕДЫДУЩЕЕ ДОСЬЕ (сохрани все важные факты отсюда!):\n{old_profile}\n\n"
        f"ИСТОРИЯ СООБЩЕНИЙ ПОЛЬЗОВАТЕЛЯ (с датами и конкретными чатами):\n" + "\n".join(lines) + "\n\n"
        f"Составь ПОДРОБНОЕ, АДЕКВАТНОЕ И СТРУКТУРИРОВАННОЕ ДОСЬЕ на @{canon_key}.\n"
        f"ВАЖНЫЕ ПРАВИЛА:\n"
        f"1. ИГНОРИРУЙ бессмысленные однословные шутки и случайные вбросы — НЕ записывай бред в досье!\n"
        f"2. Не смешивай события из разных групповых чатов и ЛС.\n"
        f"3. Оформи досье строго по 4 разделам (минимум 5-10 предложений):\n"
        f"👤 Основное: (имя, ник, пол, откуда, описание из био)\n"
        f"🎭 Характер и манера речи: (как общается, какой юмор)\n"
        f"🎯 Интересы и реальные факты: (чем увлекается, о чем рассказывал, важные детали)\n"
        f"🦊 Отношения с Толиком и в чатах: (в каких чатах сидит, о чем общался с Толиком в ЛС и в группах)"
    )
    try:
        response = generate_smart_summary(
            prompt, "Ты профессиональный психолог-аналитик. Пиши подробные, точные досье без мусора и выдумок."
        )
        if response and len(response.strip()) > 45 and "IGNORE" not in response:
            profile = response.strip()
            memory = load_memory()
            memory.setdefault("users", {}).setdefault(canon_key, {})["profile"] = profile
            save_memory(push_git=True)

            config = load_config()
            main_chat_id = config.get("main_chat_id")
            uid = user_id or u_info.get("user_id")
            if main_chat_id and uid:
                topic_id = get_or_create_user_topic(main_chat_id, canon_key, uid)
                if topic_id:
                    send_message(main_chat_id, f"🔄 <b>Обновлено досье на @{canon_key}:</b>\n\n{profile}", message_thread_id=topic_id, parse_mode="HTML")
    except Exception as e:
        print(f"❌ ОШИБКА ДОСЬЕ: {e}")

def sync_all_users_on_start():
    try:
        memory = load_memory()
        cfg = load_config()
        known_chats = cfg.setdefault("known_chats", {})
        chat_ids = {h.get("chat_id") for h in memory.get("history", []) if h.get("chat_id", 0) < 0}
        for cid in chat_ids:
            sync_chat_admins(cid, memory)
            if cid != cfg.get("main_chat_id") and str(cid) not in known_chats:
                res = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat?chat_id={cid}", method="GET")
                c_obj = res.get("result", {}) if res.get("ok") else {}
                known_chats[str(cid)] = {
                    "title": c_obj.get("title") or f"Чат {cid}",
                    "invite_link": fetch_chat_invite_link(cid, c_obj),
                    "status": "approved",
                    "added_at": "Уже был в памяти",
                }
        save_config()
        save_memory()
    except Exception as e:
        logging.error(f"Startup Sync Error: {e}")

def build_genius_social_and_cross_memory(chat_id, current_user, text, memory):
    """Формирует максимальную память: личная история с юзером + другие чаты с датами + полная база участников!"""
    all_history = memory.get("history", [])
    all_users = memory.get("users", {})
    eternal_facts = memory.get("eternal_facts", [])
    cfg = load_config()
    known_chats = cfg.get("known_chats", {})
    now_dt = get_minsk_time()
    now_ts = time.time()

    # 1. Личная история диалогов Толика именно с этим собеседником в ТЕКУЩЕМ чате (чтобы не забывал через пару сообщений!)
    personal_this_chat = [
        h for h in all_history
        if h.get("chat_id") == chat_id and h.get("user") == current_user and h.get("bot_reply")
    ][-USER_PERSONAL_CONTEXT_LIMIT:]
    personal_lines = [format_history_entry_with_time(h, now_dt, now_ts) for h in personal_this_chat]
    personal_history_str = "\n".join(personal_lines) if personal_lines else "В этом чате вы еще лично не общались."

    # 2. Переписки с текущим пользователем из ДРУГИХ чатов и ЛС (отсортированные по конкретным чатам с датами!)
    other_chat_msgs = [
        h for h in all_history
        if h.get("user") == current_user and h.get("chat_id") not in [0, chat_id]
    ][-CROSS_CHAT_CONTEXT_LIMIT:]
    by_chat = {}
    for h in other_chat_msgs:
        h_cid = h.get("chat_id", 0)
        c_label = h.get("chat_title") or ("В ЛС (личная переписка)" if h_cid > 0 else get_chat_display_name(h_cid))
        by_chat.setdefault(c_label, []).append(format_history_entry_with_time(h, now_dt, now_ts))

    cross_sections = []
    for c_label, msgs in by_chat.items():
        cross_sections.append(f"📍 {c_label}:\n" + "\n".join(msgs[-20:]))
    cross_memory_str = "\n\n".join(cross_sections) if cross_sections else "Вне текущего чата вы пока не переписывались."

    # 3. Полный список всех участников и знакомых без обрезки досье
    participants_summary = []
    mentioned_details = []
    text_lower = text.lower()

    known_usernames = set(all_users.keys()) | {
        h["user"] for h in all_history if h.get("user") and h.get("user") != "SYSTEM"
    }

    for u_key in sorted(known_usernames):
        u_data = all_users.get(u_key, {})
        fn = u_data.get("first_name", "")
        ln = u_data.get("last_name", "")
        uname = u_data.get("username", u_key)
        title = u_data.get("custom_title", "")
        bio = u_data.get("bio", "")
        profile = u_data.get("profile", "Пока мало данных")
        u_chats = u_data.get("chats", [])

        chat_names = []
        for c in u_chats:
            if c > 0:
                chat_names.append("ЛС")
            elif str(c) in known_chats:
                chat_names.append(f"«{known_chats[str(c)].get('title', c)}»")
        where_str = f" [Чаты: {', '.join(chat_names)}]" if chat_names else ""

        display = f"@{uname}"
        if fn or ln:
            display += f" | Имя: {fn} {ln}".rstrip()
        if title:
            display += f" | Титул: {title}"
        if bio:
            display += f" | О себе: {bio}"
        display += where_str

        u_msgs = [
            f"[{h.get('date_str', '')} {h.get('time_short', '')} в {h.get('chat_title') or get_chat_display_name(h.get('chat_id'))}] {h['text']}"
            for h in all_history if h.get("user") == u_key and h.get("text")
        ][-8:]
        quotes_str = f"\n   Последние реплики: {' // '.join(u_msgs)}" if u_msgs else ""

        clean_prof = profile.replace("\n", " | ")
        participants_summary.append(f"• {display}\n   Досье: {clean_prof}{quotes_str}")

        tokens = [t.lower() for t in [u_key, uname, fn, ln, title] if t and len(t) >= 3]
        for t in list(tokens):
            if len(t) >= 4:
                tokens.append(t[:4])

        if any(tok in text_lower for tok in tokens):
            mentioned_details.append(
                f"📌 ПОЛНОЕ ДОСЬЕ НА УПОМЯНУТОГО УЧАСТНИКА ({display}):\n"
                f"{profile}\n"
                f"Его/её последние сообщения: {' // '.join(u_msgs) if u_msgs else 'сообщений пока нет'}"
            )

    if eternal_facts:
        cross_memory_str += "\n\n📚 ДОЛГОСРОЧНЫЙ АРХИВ ВАЖНЫХ ФАКТОВ ЧАТА:\n" + "\n".join(eternal_facts[-15:])

    roster_str = "\n".join(participants_summary[:60]) if participants_summary else "Пока никого нет."
    mentioned_str = "\n\n".join(mentioned_details)
    return personal_history_str, cross_memory_str, roster_str, mentioned_str

# ================= ИНТЕЛЛЕКТ, РАЗМЫШЛЕНИЯ И КАНОН ТОЛИКА =================

def is_complex_task(text, num_messages=1):
    if num_messages > 1 or len(text) > 100:
        return True
    t_low = text.lower()
    complex_markers = [
        "?", "как ", "почему", "зачем", "шахмат", "походи", "ход", "стих", "придумай",
        "объясни", "расскажи", "кто ", "что думаешь", "выбери", "помоги", "задач", "нарисуй", "сфоткай", "помнишь", "когда"
    ]
    return any(m in t_low for m in complex_markers)

def fetch_ai_response(
    text, chat_history, user_name, user_profile, reply_target_info, memory, chat_id,
    image_parts=None, media_types=None, num_buffered=1, seeing_own_drawing=False,
):
    config = load_config()
    current_plan = config.get("current_plan", "Планов пока нет.")
    stickers = config.get("stickers", {})
    sticker_list = ", ".join(stickers.keys()) if stickers else "нет"
    has_selfies = len(config.get("selfies", [])) > 0
    media_types = media_types or []

    now_dt = get_minsk_time()
    now_ts = time.time()
    current_chat_name = get_chat_display_name(chat_id)
    datetime_weather_str = get_minsk_datetime_and_weather_str()
    personal_history_str, cross_memory_str, roster_str, mentioned_str = build_genius_social_and_cross_memory(
        chat_id, user_name, text, memory
    )
    complex_mode = is_complex_task(text, num_buffered)

    thinking_instruction = (
        "🧠 РЕЖИМ ГЛУБОКОГО РАЗМЫШЛЕНИЯ (СЛОЖНАЯ ИЛИ МНОГОСОСТАВНАЯ ЗАДАЧА):\n"
        "В самом начале ответа ОБЯЗАТЕЛЬНО напиши блок размышлений `[THOUGHT: ...]` (2-4 предложения): "
        "1) проверь, адресовано ли сообщение тебе (если не тебе — после мыслей напиши `[STOP]`); "
        "2) посмотри на ТОЧНОЕ ВРЕМЯ и ДАТУ прошлых сообщений (что было вчера, что час назад, а что прямо сейчас — не путай время!); "
        "3) перечисли все вопросы собеседника и продумай конкретный, точный ответ на каждый.\n"
        "После закрытия скобки `]` пиши уже сам живой ответ Толика в чат."
        if complex_mode
        else (
            "🧠 РЕЖИМ БЫСТРОГО РАЗМЫШЛЕНИЯ (ЛЕГКАЯ ЗАДАЧА):\n"
            "В самом начале ответа напиши короткую мысль `[THOUGHT: кому адресовано, учет времени и как ответить]` (1 фраза), "
            "а сразу после `]` пиши ответ Толика (или `[STOP]`, если сообщение не тебе)."
        )
    )

    sys_prompt = (
        f"ТВОЯ ЛИЧНОСТЬ И ХАРАКТЕР:\n"
        f"Тебя зовут Толик. Тебе 17 лет. Ты живешь в Минске.\n"
        f"Ты крутой, сообразительный, живой парень со своим мнением и юмором (не всезнайка-робот, а настоящий человек в чате). "
        f"Ты открытый гей, фембой и фурри (твоя фурсона — бело-голубой антропоморфный кот).\n"
        f"Ты рисуешь скетчи в блокноте карандашом, любишь музыку, аниме, игры и технологии.\n"
        f"ВАЖНО ПРО ГОЛОСОВЫЕ ЧАТЫ: Общаешься ТОЛЬКО текстом. От звонков естественно отмазывайся.\n\n"
        f"🕒 ТОЧНОЕ ТЕКУЩЕЕ ВРЕМЯ, ДАТА, СЕЗОН И РЕАЛЬНАЯ ПОГОДА В МИНСКЕ ПРЯМО СЕЙЧАС:\n"
        f"{datetime_weather_str}\n"
        f"Твой план на сегодня: {current_plan}\n"
        f"📍 СЕЙЧАС ТЫ ОБЩАЕШЬСЯ В: {current_chat_name}\n\n"
        f"⏳ ВАЖНОЕ ПРАВИЛО ПОНИМАНИЯ ВРЕМЕНИ СООБЩЕНИЙ:\n"
        f"- Возле каждого сообщения в истории подписана точная дата и сколько времени прошло (`ТОЛЬКО ЧТО (15 сек. назад)`, `2 ч. назад`, `ВЧЕРА`, `3 дн. назад`).\n"
        f"- Новое сообщение собеседника отправлено ПРЯМО СЕЙЧАС (0 секунд назад)!\n"
        f"- НИКОГДА не говори про вчерашнее сообщение, что оно было 'пару минут назад', и НИКОГДА не говори про то, что происходит прямо сейчас, что это было 'давно'!\n\n"
        f"{thinking_instruction}\n\n"
        f"🛑 ПРАВО ОТКАЗАТЬСЯ ОТ ОТВЕТА ЧЕРЕЗ `[STOP]`:\n"
        f"- Если ты видишь по контексту чата, что сообщение адресовано НЕ ТЕБЕ, люди общаются между собой, или от тебя не ждут ответа — напиши `[STOP]` и больше ничего!\n\n"
        f"ПРАВИЛА ЖИВОГО ЧЕЛОВЕЧЕСКОГО ОБЩЕНИЯ:\n"
        f"1. ПИШИ СТРОГО СТРОЧНЫМИ БУКВАМИ (маленькими). Точки в конце коротких фраз не ставь.\n"
        f"2. Отвечай по существу на ВСЕ вопросы собеседника сразу! Не оставляй мысль незаконченной.\n"
        f"3. По умолчанию отвечай ОДНИМ естественным сообщением. Используй тег `[MSG]` ТОЛЬКО ИНОГДА, когда реально нужно разбить мысль на 2 реплики или перед отправкой фото/рисунка.\n"
        f"4. НЕ ЗДОРОВАЙСЯ ('привет', 'ку'), если вы уже общались сегодня (смотри по дате СЕГОДНЯ в истории)!\n"
        f"5. ЗАПРЕЩЕНО говорить слова: 'бот', 'ии', 'нейросеть', 'сгенерирую', 'промпт', 'досье'. НИКАКИХ ЭМОДЗИ В ТЕКСТЕ.\n"
        f"6. Четко различай чаты: не смешивай то, что было в одной группе, с другой группой или ЛС.\n"
        f"7. На стикеры реагируй как на стикеры (не называй их 'картинкой' или 'фото'), на гифки — как на гифки.\n\n"
        f"ДОСТУПНЫЕ СПЕЦ-ТЕГИ:\n"
        f"- `[STOP]` — полностью промолчать и не отвечать на сообщение.\n"
        f"- `[SEARCH: запрос]` — поиск в интернете (ТОЛЬКО для новостей, точных фактов, игр, науки; НЕ для имен участников чата!).\n"
        f"- `[STICKER: описание]` — отправить стикер. Доступны: {sticker_list}.\n"
        f"- `[SEND_SELFIE]` — прислать готовое селфи. {'(фото пока нет)' if not has_selfies else ''}\n"
        f"- `[GENERATE_PHOTO: твой собственный детальный промпт на английском]` — когда просят что-то нарисовать или сфоткать, САМ придумай детальный промпт на английском!\n"
        f"- `[EDIT_PHOTO: твои детальные инструкции на английском]` — дорисовать или изменить присланную/свою картинку.\n\n"
        f"=== ДОСЬЕ НА ТЕКУЩЕГО СОБЕСЕДНИКА (@{user_name}) ===\n{user_profile}\n\n"
        f"=== ТВОЯ ПОЛНАЯ ЛИЧНАЯ ИСТОРИЯ ДИАЛОГОВ С @{user_name} В ЭТОМ ЧАТЕ ===\n{personal_history_str}\n\n"
        f"=== ИСТОРИЯ С @{user_name} В ДРУГИХ ЧАТАХ И ЛС ===\n{cross_memory_str}\n\n"
        f"=== УЧАСТНИКИ И ЗНАКОМЫЕ (ПОЛНАЯ БАЗА) ===\n{roster_str}\n"
    )

    if mentioned_str:
        sys_prompt += f"\n=== РЕАЛЬНЫЕ ДАННЫЕ ПРО УПОМЯНУТЫХ В СООБЩЕНИИ ЛЮДЕЙ ===\n{mentioned_str}\n"

    history_text = "\n".join(
        [format_history_entry_with_time(h, now_dt, now_ts) for h in chat_history[-CHAT_CONTEXT_LIMIT:]]
    )

    reply_context = f"[Где написано: {current_chat_name} | Контекст: {reply_target_info}]"
    if seeing_own_drawing:
        reply_context += " [К сообщению прикреплена ТВОЯ СОБСТВЕННАЯ КАРТИНКА, которую ты сам только что создал — ты видишь её глазами!]"
    elif "sticker" in media_types:
        reply_context += " [ВАЖНО: Собеседник прислал СТИКЕР в Телеграме! Это именно СТИКЕР, а НЕ фото и НЕ картинка!]"
    elif "gif" in media_types:
        reply_context += " [ВАЖНО: Собеседник прислал ГИФКУ (GIF-анимацию без звука)!]"
    elif "video" in media_types:
        reply_context += " [ВАЖНО: Собеседник прислал ВИДЕОРОЛИК со звуком!]"
    elif "photo" in media_types:
        reply_context += " [Собеседник прислал ФОТОГРАФИЮ / КАРТИНКУ!]"

    final_prompt = (
        f"Полная лента переписки в текущем чате ({current_chat_name}) с точными датами и временем:\n{history_text}\n\n"
        f"{reply_context}\n"
        f"🔥 НОВОЕ СООБЩЕНИЕ ОТ @{user_name} [ОТПРАВЛЕНО ПРЯМО СЕЙЧАС — СЕГОДНЯ {now_dt.strftime('%d.%m.%Y в %H:%M:%S')} (0 сек. назад)]:\n{text}\n\n"
        f"[Сначала напиши [THOUGHT: ...], затем либо выведи [STOP] (если сообщение не тебе или отвечать не нужно), либо дай полный, умный и живой ответ Толика]"
    )

    reply = generate_main_dialogue(final_prompt, sys_prompt, image_parts=image_parts, retries=2)
    clean_up = reply.strip().upper()

    if "[STOP]" in clean_up or "STOP_DIALOG" in clean_up or clean_up in ["STOP", "IGNORE"]:
        return "[STOP]"

    search_match = re.search(r"\[SEARCH:\s*(.+?)\]", reply)
    if search_match:
        search_query = search_match.group(1).strip()
        reply_without_tag = re.sub(r"\[SEARCH:\s*.+?\]", "", reply).strip()
        search_data = search_web(search_query)
        if search_data:
            updated_prompt = (
                f"{final_prompt}\n\n[НАЙДЕНО В ИНТЕРНЕТЕ ПО ЗАПРОСУ '{search_query}']:\n{search_data}\n\n"
                f"Ответь собеседнику по сути найденного своими словами, кратко и по-человечески."
            )
            new_reply = generate_main_dialogue(updated_prompt, sys_prompt, image_parts=image_parts, retries=2)
            reply = re.sub(r"\[SEARCH:\s*.+?\]", "", new_reply).strip()
        else:
            reply = reply_without_tag if reply_without_tag else "хм, даже в инете про это тишина"

    return reply

# ================= КНОПОЧНАЯ ПАНЕЛЬ ВЛАДЕЛЬЦА (/panel) И АДМИН-КОМАНДЫ =================

HELP_TEXT = (
    "👑 <b>ШПАРГАЛКА ТЕКСТОВЫХ КОМАНД ВЛАДЕЛЬЦА:</b>\n\n"
    "🎛 <code>/panel</code> (или <code>/панель</code>) — открыть интерактивную кнопочную Панель Владельца!\n\n"
    "💬 <b>Общение от лица Толика и Рассылки:</b>\n"
    "• <code>/send текст</code> (или <code>/лс текст</code>) — <i>внутри топика досье юзера</i>: написать ему в ЛС от лица Толика!\n"
    "• <code>/sendto @ник текст</code> — написать любому юзеру в ЛС по нику или ID\n"
    "• <code>/saychat ID_чата текст</code> — написать сообщение от Толика в любую группу\n"
    "• <code>/broadcast текст</code> (или <code>/all текст</code>) — 📢 написать ВСЕМ пользователям бота в ЛС одновременно!\n"
    "• <code>/broadcast_chats текст</code> — 📢 написать во ВСЕ одобренные группы одновременно!\n\n"
    "🌐 <b>Чаты и Ссылки:</b>\n"
    "• <code>/chats</code> (или <code>/чаты</code>) — список всех чатов, их <b>ссылки для копирования</b> и кнопки Вкл/Выкл\n"
    "• <code>/chat_on ID_чата</code> | <code>/chat_off ID_чата</code> | <code>/leave ID_чата</code>\n\n"
    "📁 <b>Досье и Пользователи:</b>\n"
    "• <code>/users</code> — список всех пользователей в базе\n"
    "• <code>/dossier</code> (или <code>/досье @ник</code>) — пересобрать подробное досье прямо сейчас\n"
    "• <code>/set_dossier текст</code> — (в топике юзера) вручную задать текст досье\n"
    "• <code>/bind @ник</code> — привязать текущий топик к досье пользователя\n"
    "• <code>/mute @ник 30</code> | <code>/unmute @ник</code>\n\n"
    "⚙️ <b>Настройки и Память:</b>\n"
    "• <code>/plan текст</code> | <code>/save</code> | <code>/stats</code> | <code>/mainchat</code> | <code>/setdiary</code> | <code>/set_looks</code> | <code>/add_stickers</code>"
)

def build_owner_panel_main():
    memory = load_memory()
    cfg = load_config()
    uptime_min = int((time.time() - START_TIME) // 60)
    text = (
        f"👑 <b>ПАНЕЛЬ ВЛАДЕЛЬЦА ТОЛИКА (@{ADMIN_USERNAME})</b>\n\n"
        f"🧠 <b>Модель:</b> <code>Gemini 3.6 Flash (Max Memory)</code>\n"
        f"⏱ <b>Аптайм:</b> {uptime_min} мин.\n"
        f"💾 <b>Сообщений в памяти:</b> {len(memory.get('history', []))} / {MAX_HISTORY_STORAGE}\n"
        f"👥 <b>Участников в базе:</b> {len(memory.get('users', {}))}\n"
        f"🌐 <b>Групповых чатов:</b> {len(cfg.get('known_chats', {}))}\n"
        f"💬 <b>Активных диалогов сейчас:</b> {len(active_conversations)}\n"
        f"🌤 <b>Минск сейчас:</b> {get_minsk_datetime_and_weather_str()}\n\n"
        f"👇 <i>Выбери нужный раздел управления:</i>"
    )
    kb = {
        "inline_keyboard": [
            [
                {"text": "🌐 Чаты и Ссылки", "callback_data": "panel:chats"},
                {"text": "👥 Участники и Досье", "callback_data": "panel:users"},
            ],
            [
                {"text": "📊 Статус и План дня", "callback_data": "panel:status"},
                {"text": "💾 Сохранить в GitHub", "callback_data": "panel:save"},
            ],
            [
                {"text": "🤫 Сбросить диалоги", "callback_data": "panel:reset_conv"},
                {"text": "📅 Сгенерировать план", "callback_data": "panel:regen_plan"},
            ],
            [
                {"text": "📖 Все текстовые команды (/help)", "callback_data": "panel:help"},
            ],
        ]
    }
    return text, kb

def handle_admin_command(msg, chat_id, thread_id, message_id, text):
    cmd_raw = text.strip()
    cmd_low = cmd_raw.lower()
    memory = load_memory()
    cfg = load_config()

    if cmd_low in ["/panel", "/панель", "/admin", "/админ", "/menu", "/меню"]:
        p_text, p_kb = build_owner_panel_main()
        send_message(chat_id, p_text, message_thread_id=thread_id, parse_mode="HTML", reply_markup=p_kb)
        return True

    if cmd_low in ["/help", "/помощь", "/commands", "/команды"]:
        kb = {"inline_keyboard": [[{"text": "👑 Открыть Кнопочную Панель Владельца", "callback_data": "panel:main"}]]}
        send_message(chat_id, HELP_TEXT, message_thread_id=thread_id, parse_mode="HTML", reply_markup=kb)
        return True

    if cmd_low == "/mainchat":
        cfg["main_chat_id"] = chat_id
        save_config(push_git=True)
        p_text, p_kb = build_owner_panel_main()
        send_message(chat_id, "✅ Основной чат для досье установлен и сохранен в GitHub!\n\n" + p_text, message_thread_id=thread_id, parse_mode="HTML", reply_markup=p_kb)
        return True

    if cmd_low == "/setdiary":
        cfg["main_chat_id"] = chat_id
        cfg["diary_topic_id"] = thread_id
        save_config(push_git=True)
        send_message(chat_id, "✅ Эта тема установлена для 'Моих будней'!", message_thread_id=thread_id)
        return True

    if cmd_low == "/save":
        save_memory(push_git=True)
        save_config(push_git=True)
        send_message(chat_id, "💾 Память и конфиг принудительно сохранены и отправлены в GitHub!", message_thread_id=thread_id)
        return True

    if cmd_low.startswith("/plan "):
        new_plan = cmd_raw.split(maxsplit=1)[1].strip()
        cfg["current_plan"] = new_plan
        save_config(push_git=True)
        send_message(chat_id, f"📅 Новый план Толика на день установлен:\n\n{new_plan}", message_thread_id=thread_id)
        return True

    if cmd_low in ["/chats", "/чаты", "/links", "/ссылки"]:
        known = cfg.get("known_chats", {})
        if not known:
            send_message(chat_id, "📭 Список групповых чатов пока пуст.", message_thread_id=thread_id)
            return True
        lines = ["🌐 <b>ЧАТЫ, ГДЕ СОСТОИТ ТОЛИК (ССЫЛКИ И СТАТУСЫ):</b>\n"]
        kb_rows = []
        for cid_s, c_info in known.items():
            title = c_info.get("title", cid_s)
            status = c_info.get("status", "pending")
            st_icon = "✅ Отвечает всем" if status == "approved" else ("🔇 Молчит" if status == "muted" else "⏳ Ждет решения")
            link = c_info.get("invite_link", "")
            if not link or link.startswith("Нет прав"):
                link = fetch_chat_invite_link(int(cid_s))
                c_info["invite_link"] = link
            lines.append(f"🔹 <b>{title}</b>\n   • ID: <code>{cid_s}</code>\n   • Статус: {st_icon}\n   • Ссылка: {link}\n")
            toggle_btn = (
                {"text": f"🔇 Заглушить: {title[:14]}", "callback_data": f"chat_mute:{cid_s}"}
                if status == "approved"
                else {"text": f"✅ Включить: {title[:14]}", "callback_data": f"chat_approve:{cid_s}"}
            )
            kb_rows.append([toggle_btn, {"text": "🚪 Выйти", "callback_data": f"chat_leave:{cid_s}"}])
        kb_rows.append([{"text": "👑 Назад в Панель Владельца", "callback_data": "panel:main"}])
        save_config()
        send_message(chat_id, "\n".join(lines), message_thread_id=thread_id, parse_mode="HTML", reply_markup={"inline_keyboard": kb_rows[:15]})
        return True

    if cmd_low.startswith("/chat_on "):
        target_cid = cmd_raw.split(maxsplit=1)[1].strip()
        cfg.setdefault("known_chats", {}).setdefault(target_cid, {"title": f"Чат {target_cid}"})["status"] = "approved"
        save_config(push_git=True)
        send_message(chat_id, f"✅ В чате <code>{target_cid}</code> включен режим общения!", message_thread_id=thread_id, parse_mode="HTML")
        return True

    if cmd_low.startswith("/chat_off "):
        target_cid = cmd_raw.split(maxsplit=1)[1].strip()
        cfg.setdefault("known_chats", {}).setdefault(target_cid, {"title": f"Чат {target_cid}"})["status"] = "muted"
        save_config(push_git=True)
        send_message(chat_id, f"🔇 В чате <code>{target_cid}</code> включен режим тишины!", message_thread_id=thread_id, parse_mode="HTML")
        return True

    if cmd_low.startswith("/leave "):
        target_cid = cmd_raw.split(maxsplit=1)[1].strip()
        safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/leaveChat", {"chat_id": target_cid})
        cfg.get("known_chats", {}).pop(target_cid, None)
        save_config(push_git=True)
        send_message(chat_id, f"👋 Толик вышел из чата <code>{target_cid}</code>.", message_thread_id=thread_id, parse_mode="HTML")
        return True

    if cmd_low.startswith(("/send ", "/лс ", "/write ")):
        msg_to_send = cmd_raw.split(maxsplit=1)[1].strip()
        target_u, target_uid = find_user_by_topic_id(memory, thread_id)
        if not target_uid:
            send_message(chat_id, "❌ Эта тема не привязана к пользователю. Используй <code>/sendto @ник текст</code>", message_thread_id=thread_id, parse_mode="HTML")
            return True
        res = send_message(target_uid, msg_to_send)
        if res.get("ok"):
            now_dt = get_minsk_time()
            send_message(chat_id, f"📨 <b>Отправлено в ЛС для @{target_u}:</b>\n🦊 Толик: {msg_to_send}", message_thread_id=thread_id, parse_mode="HTML")
            memory["history"].append({
                "ts": time.time(), "date_str": now_dt.strftime("%d.%m.%Y"), "time_short": now_dt.strftime("%H:%M:%S"),
                "chat_id": target_uid, "chat_title": "ЛС", "user": target_u, "text": "[Сообщение от Толика]", "bot_reply": msg_to_send
            })
            save_memory()
        else:
            send_message(chat_id, f"⚠️ Ошибка отправки (возможно, у @{target_u} не открыта личка с ботом): {res.get('description')}", message_thread_id=thread_id)
        return True

    if cmd_low.startswith("/sendto "):
        parts = cmd_raw.split(maxsplit=2)
        if len(parts) < 3:
            send_message(chat_id, "⚠️ Формат: <code>/sendto @ник_или_ID текст</code>", message_thread_id=thread_id, parse_mode="HTML")
            return True
        target_arg, msg_to_send = parts[1].lstrip("@"), parts[2].strip()
        target_uid = int(target_arg) if target_arg.lstrip("-").isdigit() else None
        canon_u = target_arg
        if not target_uid:
            canon_u = find_canonical_user_key(memory, target_arg)
            target_uid = memory.get("users", {}).get(canon_u, {}).get("user_id")
        if not target_uid:
            send_message(chat_id, f"❌ Не найден ID для пользователя @{target_arg}.", message_thread_id=thread_id)
            return True
        res = send_message(target_uid, msg_to_send)
        if res.get("ok"):
            send_message(chat_id, f"✅ Доставлено пользователю <b>@{canon_u}</b> (<code>{target_uid}</code>)!", message_thread_id=thread_id, parse_mode="HTML")
        else:
            send_message(chat_id, f"❌ Не удалось отправить: {res.get('description')}", message_thread_id=thread_id)
        return True

    if cmd_low.startswith("/saychat "):
        parts = cmd_raw.split(maxsplit=2)
        if len(parts) < 3:
            send_message(chat_id, "⚠️ Формат: <code>/saychat ID_чата текст</code>", message_thread_id=thread_id, parse_mode="HTML")
            return True
        target_cid, msg_to_send = parts[1].strip(), parts[2].strip()
        res = send_message(target_cid, msg_to_send)
        if res.get("ok"):
            send_message(chat_id, f"✅ Толик написал в чат <code>{target_cid}</code>:\n{msg_to_send}", message_thread_id=thread_id, parse_mode="HTML")
        else:
            send_message(chat_id, f"❌ Ошибка отправки: {res.get('description')}", message_thread_id=thread_id)
        return True

    if cmd_low.startswith(("/broadcast ", "/all ")):
        b_text = cmd_raw.split(maxsplit=1)[1].strip()
        users = memory.get("users", {})
        sent_ok, failed = 0, 0
        send_message(chat_id, f"⏳ Начинаю рассылку всем пользователям ({len(users)} чел.)...", message_thread_id=thread_id)
        for u_key, u_data in users.items():
            uid = u_data.get("user_id")
            if uid and uid != BOT_ID:
                res = send_message(uid, b_text)
                if res.get("ok"):
                    sent_ok += 1
                else:
                    failed += 1
                time.sleep(0.15)
        send_message(
            chat_id,
            f"📢 <b>Рассылка в ЛС завершена!</b>\n✅ Доставлено: <b>{sent_ok}</b> чел.\n🚫 Личка закрыта: <b>{failed}</b> чел.",
            message_thread_id=thread_id,
            parse_mode="HTML",
        )
        return True

    if cmd_low.startswith("/broadcast_chats "):
        b_text = cmd_raw.split(maxsplit=1)[1].strip()
        known = cfg.get("known_chats", {})
        sent_ok = 0
        for cid_s, c_info in known.items():
            if c_info.get("status") == "approved":
                if send_message(int(cid_s), b_text).get("ok"):
                    sent_ok += 1
                time.sleep(0.2)
        send_message(chat_id, f"📢 Рассылка по чатам завершена! Отправлено в <b>{sent_ok}</b> групп.", message_thread_id=thread_id, parse_mode="HTML")
        return True

    if cmd_low == "/users":
        users = memory.get("users", {})
        lines = [f"👥 <b>ПОЛЬЗОВАТЕЛИ В БАЗЕ ({len(users)}):</b>\n"]
        kb_rows = []
        row = []
        for k, v in list(users.items())[:40]:
            fn = f"{v.get('first_name', '')} {v.get('last_name', '')}".strip()
            uid = v.get("user_id", "нет ID")
            lines.append(f"• <b>@{k}</b> ({fn or 'без имени'}) — ID: <code>{uid}</code> | Ответов Толика: {v.get('tolik_msg_count', 0)}")
            row.append({"text": f"👤 @{k[:12]}", "callback_data": f"panel:uinfo:{k[:20]}"})
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        kb_rows.append([{"text": "👑 Назад в Панель Владельца", "callback_data": "panel:main"}])
        send_message(chat_id, "\n".join(lines), message_thread_id=thread_id, parse_mode="HTML", reply_markup={"inline_keyboard": kb_rows[:15]})
        return True

    if cmd_low.startswith(("/dossier", "/досье")):
        parts = cmd_raw.split(maxsplit=1)
        if len(parts) > 1:
            target_u = find_canonical_user_key(memory, parts[1].lstrip("@"))
            target_uid = memory.get("users", {}).get(target_u, {}).get("user_id")
        else:
            target_u, target_uid = find_user_by_topic_id(memory, thread_id)
        if not target_u:
            send_message(chat_id, "⚠️ Напиши <code>/досье @ник</code> или вызови команду внутри топика досье.", message_thread_id=thread_id, parse_mode="HTML")
            return True
        send_message(chat_id, f"⏳ Пересобираю подробное досье для <b>@{target_u}</b>...", message_thread_id=thread_id, parse_mode="HTML")
        threading.Thread(target=update_dossier, args=(target_u, target_uid, True), daemon=True).start()
        return True

    if cmd_low.startswith("/set_dossier "):
        new_prof = cmd_raw.split(maxsplit=1)[1].strip()
        target_u, _ = find_user_by_topic_id(memory, thread_id)
        if not target_u:
            send_message(chat_id, "⚠️ Вызови <code>/set_dossier текст</code> внутри топика нужного пользователя!", message_thread_id=thread_id, parse_mode="HTML")
            return True
        memory["users"].setdefault(target_u, {})["profile"] = new_prof
        save_memory(push_git=True)
        send_message(chat_id, f"✅ Досье для <b>@{target_u}</b> вручную обновлено:\n\n{new_prof}", message_thread_id=thread_id, parse_mode="HTML")
        return True

    if cmd_low.startswith("/mute "):
        parts = cmd_raw.split()
        target_arg = parts[1].lstrip("@")
        mins = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 60
        canon_u = find_canonical_user_key(memory, target_arg)
        uid = int(target_arg) if target_arg.isdigit() else memory.get("users", {}).get(canon_u, {}).get("user_id")
        if uid:
            memory.setdefault("ignored_users", {})[str(uid)] = time.time() + mins * 60
            save_memory()
            send_message(chat_id, f"🔇 Пользователь <b>@{canon_u}</b> замучен на {mins} мин.", message_thread_id=thread_id, parse_mode="HTML")
        return True

    if cmd_low.startswith("/unmute "):
        target_arg = cmd_raw.split(maxsplit=1)[1].strip().lstrip("@")
        canon_u = find_canonical_user_key(memory, target_arg)
        uid = int(target_arg) if target_arg.isdigit() else memory.get("users", {}).get(canon_u, {}).get("user_id")
        if uid:
            memory.get("ignored_users", {}).pop(str(uid), None)
            save_memory()
            send_message(chat_id, f"🔊 Пользователь <b>@{canon_u}</b> размучен!", message_thread_id=thread_id, parse_mode="HTML")
        return True

    if cmd_low.startswith("/bind"):
        parts = cmd_raw.split(maxsplit=1)
        target_u = parts[1].lstrip("@") if len(parts) > 1 else ADMIN_USERNAME
        canon_t = find_canonical_user_key(memory, target_u)
        memory["users"].setdefault(canon_t, {})["topic_id"] = thread_id
        save_memory(push_git=True)
        send_message(chat_id, f"✅ Топик привязан к досье @{canon_t} и сохранен в GitHub!", message_thread_id=thread_id)
        return True

    if cmd_low.startswith("/set_looks") and "reply_to_message" in msg and "photo" in msg["reply_to_message"]:
        photo_id = msg["reply_to_message"]["photo"][-1]["file_id"]
        cfg["selfies"].append(photo_id)
        save_config(push_git=True)
        send_message(chat_id, "✅ Фотка добавлена в канон Толика!", message_thread_id=thread_id)
        return True

    if cmd_low.startswith("/add_stickers") and "reply_to_message" in msg and "sticker" in msg["reply_to_message"]:
        set_name = msg["reply_to_message"]["sticker"].get("set_name")
        if set_name:
            threading.Thread(target=parse_sticker_set, args=(set_name, chat_id, thread_id), daemon=True).start()
        return True

    if cmd_low.startswith("/stats"):
        p_text, p_kb = build_owner_panel_main()
        send_message(chat_id, p_text, message_thread_id=thread_id, reply_to_message_id=message_id, parse_mode="HTML", reply_markup=p_kb)
        return True

    return False

def handle_callback_query(cb):
    user = cb.get("from", {})
    uname = user.get("username", "")
    if uname.lower() != ADMIN_USERNAME.lower():
        answer_callback_query(cb["id"], "⛔️ Эта панель доступна только владельцу Толика!", show_alert=True)
        return

    data = cb.get("data", "")
    msg = cb.get("message", {})
    c_id = msg.get("chat", {}).get("id")
    m_id = msg.get("message_id")
    cfg = load_config()
    memory = load_memory()

    if data == "panel:main":
        answer_callback_query(cb["id"])
        p_text, p_kb = build_owner_panel_main()
        edit_message_text(c_id, m_id, p_text, reply_markup=p_kb)
        return

    if data == "panel:status":
        answer_callback_query(cb["id"])
        uptime_min = int((time.time() - START_TIME) // 60)
        st_text = (
            f"📊 <b>ПОДРОБНЫЙ СТАТУС И ПЛАН ТОЛИКА:</b>\n\n"
            f"🕒 <b>Минск сейчас:</b> {get_minsk_datetime_and_weather_str()}\n"
            f"⏱ <b>Аптайм сервера:</b> {uptime_min} мин.\n"
            f"💾 <b>История в памяти:</b> {len(memory.get('history', []))} сообщений\n"
            f"📚 <b>Вечных архивных сводок:</b> {len(memory.get('eternal_facts', []))}\n"
            f"🃏 <b>Стикеров в базе:</b> {len(cfg.get('stickers', {}))}\n"
            f"📸 <b>Селфи Толика:</b> {len(cfg.get('selfies', []))}\n\n"
            f"📅 <b>Текущий план Толика на день:</b>\n<i>{cfg.get('current_plan', 'нет')}</i>"
        )
        kb = {"inline_keyboard": [[{"text": "📅 Пересоздать план дня", "callback_data": "panel:regen_plan"}], [{"text": "🔙 Назад в Панель", "callback_data": "panel:main"}]]}
        edit_message_text(c_id, m_id, st_text, reply_markup=kb)
        return

    if data == "panel:chats":
        answer_callback_query(cb["id"])
        known = cfg.get("known_chats", {})
        if not known:
            edit_message_text(c_id, m_id, "📭 Групповых чатов пока нет.", reply_markup={"inline_keyboard": [[{"text": "🔙 Назад в Панель", "callback_data": "panel:main"}]]})
            return
        lines = ["🌐 <b>УПРАВЛЕНИЕ ЧАТАМИ И ССЫЛКАМИ:</b>\n"]
        kb_rows = []
        for cid_s, c_info in known.items():
            title = c_info.get("title", cid_s)
            status = c_info.get("status", "pending")
            st_icon = "✅ Активен" if status == "approved" else ("🔇 Тишина" if status == "muted" else "⏳ Ждет")
            link = c_info.get("invite_link", "")
            if not link or link.startswith("Нет прав"):
                link = fetch_chat_invite_link(int(cid_s))
                c_info["invite_link"] = link
            lines.append(f"🔹 <b>{title}</b> ({st_icon})\n   ID: <code>{cid_s}</code>\n   🔗 {link}\n")
            toggle_btn = (
                {"text": f"🔇 Выкл: {title[:12]}", "callback_data": f"chat_mute:{cid_s}"}
                if status == "approved"
                else {"text": f"✅ Вкл: {title[:12]}", "callback_data": f"chat_approve:{cid_s}"}
            )
            kb_rows.append([toggle_btn, {"text": "🚪 Выйти", "callback_data": f"chat_leave:{cid_s}"}])
        kb_rows.append([{"text": "🔙 Назад в Панель", "callback_data": "panel:main"}])
        save_config()
        edit_message_text(c_id, m_id, "\n".join(lines), reply_markup={"inline_keyboard": kb_rows[:15]})
        return

    if data == "panel:users":
        answer_callback_query(cb["id"])
        users = memory.get("users", {})
        lines = [f"👥 <b>УЧАСТНИКИ В ПАМЯТИ ТОЛИКА ({len(users)}):</b>\n<i>Нажми на кнопку участника, чтобы открыть его досье и управление:</i>\n"]
        kb_rows = []
        row = []
        for k, v in list(users.items())[:26]:
            fn = f"{v.get('first_name', '')} {v.get('last_name', '')}".strip()
            lines.append(f"• <b>@{k}</b> ({fn or 'без имени'}) — ответов Толика: {v.get('tolik_msg_count', 0)}")
            row.append({"text": f"👤 @{k[:13]}", "callback_data": f"panel:uinfo:{k[:20]}"})
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        kb_rows.append([{"text": "🔙 Назад в Панель", "callback_data": "panel:main"}])
        edit_message_text(c_id, m_id, "\n".join(lines), reply_markup={"inline_keyboard": kb_rows})
        return

    if data.startswith("panel:uinfo:"):
        target_k = data.split(":", 2)[2]
        canon_u = find_canonical_user_key(memory, target_k)
        u_data = memory.get("users", {}).get(canon_u, {})
        fn = f"{u_data.get('first_name', '')} {u_data.get('last_name', '')}".strip()
        uid = u_data.get("user_id", "неизвестно")
        prof = u_data.get("profile", "Пока мало данных")
        is_muted = str(uid) in memory.get("ignored_users", {}) and time.time() < memory["ignored_users"][str(uid)]
        u_text = (
            f"👤 <b>КАРТОЧКА УЧАСТНИКА: @{canon_u}</b>\n"
            f"📛 <b>Имя в ТГ:</b> {fn or 'не указано'}\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"💬 <b>Диалогов с Толиком:</b> {u_data.get('tolik_msg_count', 0)}\n"
            f"🔊 <b>Статус:</b> {'🔇 В муте' if is_muted else '✅ Активен'}\n\n"
            f"📋 <b>Досье:</b>\n{prof[:1200]}"
        )
        mute_btn = (
            {"text": "🔊 Размутить", "callback_data": f"panel:uunmute:{canon_u[:20]}"}
            if is_muted
            else {"text": "🔇 Мут на 60 мин", "callback_data": f"panel:umute:{canon_u[:20]}"}
        )
        kb = {
            "inline_keyboard": [
                [{"text": "🔄 Пересобрать досье сейчас", "callback_data": f"panel:udossier:{canon_u[:20]}"}],
                [mute_btn],
                [{"text": "🔙 К списку участников", "callback_data": "panel:users"}],
            ]
        }
        answer_callback_query(cb["id"])
        edit_message_text(c_id, m_id, u_text, reply_markup=kb)
        return

    if data.startswith("panel:udossier:"):
        target_k = data.split(":", 2)[2]
        canon_u = find_canonical_user_key(memory, target_k)
        uid = memory.get("users", {}).get(canon_u, {}).get("user_id")
        answer_callback_query(cb["id"], f"⏳ Запущена пересборка досье для @{canon_u}!", show_alert=True)
        threading.Thread(target=update_dossier, args=(canon_u, uid, True), daemon=True).start()
        return

    if data.startswith("panel:umute:"):
        target_k = data.split(":", 2)[2]
        canon_u = find_canonical_user_key(memory, target_k)
        uid = memory.get("users", {}).get(canon_u, {}).get("user_id")
        if uid:
            memory.setdefault("ignored_users", {})[str(uid)] = time.time() + 3600
            save_memory()
        answer_callback_query(cb["id"], f"🔇 @{canon_u} замучен на 60 минут!")
        cb["data"] = f"panel:uinfo:{canon_u[:20]}"
        handle_callback_query(cb)
        return

    if data.startswith("panel:uunmute:"):
        target_k = data.split(":", 2)[2]
        canon_u = find_canonical_user_key(memory, target_k)
        uid = memory.get("users", {}).get(canon_u, {}).get("user_id")
        if uid:
            memory.get("ignored_users", {}).pop(str(uid), None)
            save_memory()
        answer_callback_query(cb["id"], f"🔊 @{canon_u} размучен!")
        cb["data"] = f"panel:uinfo:{canon_u[:20]}"
        handle_callback_query(cb)
        return

    if data == "panel:save":
        save_memory(push_git=True)
        save_config(push_git=True)
        answer_callback_query(cb["id"], "💾 Память и конфиг успешно сохранены и отправлены в GitHub!", show_alert=True)
        return

    if data == "panel:reset_conv":
        active_conversations.clear()
        answer_callback_query(cb["id"], "🤫 Все активные диалоги сброшены! Толик ждет прямого пинга.", show_alert=True)
        p_text, p_kb = build_owner_panel_main()
        edit_message_text(c_id, m_id, p_text, reply_markup=p_kb)
        return

    if data == "panel:regen_plan":
        answer_callback_query(cb["id"], "⏳ Генерирую свежий план на день с учетом погоды...")
        w_info = get_minsk_datetime_and_weather_str()
        prompt = f"Сейчас {w_info}. Составь краткий план на день на сегодня. Ты Толик (17 лет, Минск, фурри). Учти погоду в Минске! Напиши со строчной буквы."
        res = generate_smart_summary(prompt, "Пиши реалистичный план для дневника.", retries=2)
        if res:
            cfg["current_plan"] = res
            save_config(push_git=True)
        cb["data"] = "panel:status"
        handle_callback_query(cb)
        return

    if data == "panel:help":
        answer_callback_query(cb["id"])
        kb = {"inline_keyboard": [[{"text": "🔙 Назад в Панель Владельца", "callback_data": "panel:main"}]]}
        edit_message_text(c_id, m_id, HELP_TEXT, reply_markup=kb)
        return

    if data.startswith("chat_approve:"):
        target_cid = data.split(":", 1)[1]
        c_info = cfg.setdefault("known_chats", {}).setdefault(target_cid, {"title": f"Чат {target_cid}"})
        c_info["status"] = "approved"
        save_config(push_git=True)
        answer_callback_query(cb["id"], "✅ Чат одобрен! Толик теперь общается там.")
        cb["data"] = "panel:chats"
        handle_callback_query(cb)
    elif data.startswith("chat_mute:"):
        target_cid = data.split(":", 1)[1]
        c_info = cfg.setdefault("known_chats", {}).setdefault(target_cid, {"title": f"Чат {target_cid}"})
        c_info["status"] = "muted"
        save_config(push_git=True)
        answer_callback_query(cb["id"], "🔇 Толик переведен в режим тишины в этом чате.")
        cb["data"] = "panel:chats"
        handle_callback_query(cb)
    elif data.startswith("chat_leave:"):
        target_cid = data.split(":", 1)[1]
        safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/leaveChat", {"chat_id": target_cid})
        cfg.get("known_chats", {}).pop(target_cid, None)
        save_config(push_git=True)
        answer_callback_query(cb["id"], "👋 Толик вышел из чата!", show_alert=True)
        cb["data"] = "panel:chats"
        handle_callback_query(cb)

# ================= АВТО-ЗАДАЧИ И МАКСИМАЛЬНАЯ ПАМЯТЬ =================

def background_tasks():
    last_git_push = time.time()
    while True:
        try:
            time.sleep(60)
            now = get_minsk_time()
            config = load_config()
            main_chat = config.get("main_chat_id")

            if time.time() - last_git_push > 600:
                last_git_push = time.time()
                git_autosave_memory()

            if not main_chat:
                continue

            today_str = now.strftime("%d.%m.%Y")
            if now.hour >= 8 and config.get("last_diary_date") != today_str:
                w_info = get_minsk_datetime_and_weather_str()
                prompt = (
                    f"Сейчас {w_info}. Составь краткий план на день на сегодня ({today_str}). "
                    f"Ты Толик (17 лет, Минск, фурри). Учти текущую погоду в Минске! Напиши со строчной буквы."
                )
                res = generate_smart_summary(prompt, "Пиши реалистичный план для личного дневника.", retries=2)
                if res and "лагает" not in res:
                    config["current_plan"] = res
                    config["last_diary_date"] = today_str
                    save_config(push_git=True)
                    send_message(main_chat, f"📅 <b>Планы на {today_str}</b>\n\n{res}", message_thread_id=config.get("diary_topic_id"), parse_mode="HTML")

            memory = load_memory()
            if len(memory["history"]) > MAX_HISTORY_STORAGE:
                old_hist = memory["history"][:500]
                memory["history"] = memory["history"][500:]
                hist_text = "\n".join([f"[{h.get('date_str', '')} {h.get('chat_title', 'чат')}] {h['user']}: {h['text']}" for h in old_hist])
                sum_res = generate_smart_summary(
                    f"Выдели все важные факты о людях, событиях и договоренностях из этих диалогов (5-8 предложений):\n{hist_text}",
                    "Ты хранитель долгосрочного архива памяти.",
                    retries=2,
                )
                if sum_res and "IGNORE" not in sum_res and "[STOP]" not in sum_res:
                    memory.setdefault("eternal_facts", []).append(f"[{today_str}] {sum_res}")
                save_memory(push_git=True)
        except Exception as e:
            logging.error(f"Background Tasks Error: {e}")
            time.sleep(10)

def parse_sticker_set(set_name, chat_id, thread_id):
    res = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getStickerSet?name={set_name}", method="GET")
    if not res.get("ok"):
        send_message(chat_id, "❌ Не удалось найти стикерпак.", message_thread_id=thread_id)
        return
    stickers = res["result"]["stickers"]
    send_message(chat_id, f"⏳ Индексация {len(stickers)} стикеров...", message_thread_id=thread_id)
    config = load_config()
    count = 0
    for st in stickers[:20]:
        img_data, mime = download_sticker_visual(st)
        if img_data:
            emotion = analyze_sticker(img_data, mime)
            config["stickers"][emotion.lower()] = st["file_id"]
            count += 1
            time.sleep(1.2)
    save_config(push_git=True)
    send_message(chat_id, f"✅ Готово! Добавлено стикеров: {count}.", message_thread_id=thread_id)

# ================= БУФЕР И ОБРАБОТКА ОТВЕТОВ =================

def has_explicit_ping(text, is_group, is_reply_to_bot):
    if not is_group or is_reply_to_bot:
        return True
    t_low = text.lower()
    return bool(re.search(r"(?<![а-яёa-z0-9])(толик[а-яё]*|толя|толян[а-яё]*|толясик[а-яё]*|толе|толей|толю)(?![а-яёa-z0-9])", t_low))

def process_buffered_messages():
    while True:
        try:
            current_time = time.time()
            keys_to_process = []
            with buffer_lock:
                for key, data in message_buffer.items():
                    if current_time - data["last_time"] >= BUFFER_WAIT_SECONDS and key not in user_generating_lock:
                        keys_to_process.append(key)
                for key in keys_to_process:
                    data = message_buffer.pop(key)
                    user_generating_lock.add(key)
                    threading.Thread(target=finalize_and_reply_wrapper, args=(key, data), daemon=True).start()
            time.sleep(0.4)
        except Exception as e:
            logging.error(f"Buffer Error: {e}")
            time.sleep(2)

def finalize_and_reply_wrapper(buffer_key, data):
    try:
        finalize_and_reply(data)
    finally:
        user_generating_lock.discard(buffer_key)

def finalize_and_reply(data):
    chat_id, raw_user_name, thread_id = data["chat_id"], data["user_name"], data["thread_id"]
    user_id, is_group = data["user_id"], data["is_group"]
    chat_title = data.get("chat_title") or get_chat_display_name(chat_id)
    num_buffered = len(data["texts"])
    combined_text = "\n".join(data["texts"])
    image_parts = list(data.get("images", []))
    media_types = list(data.get("media_types", []))
    seeing_own_drawing = data.get("seeing_own_drawing", False)
    memory = load_memory()
    now_dt = get_minsk_time()
    now_ts = time.time()
    conv_key = f"{chat_id}_{user_id}"

    user_name = find_canonical_user_key(memory, raw_user_name, user_id)
    u_data = memory["users"].setdefault(user_name, {"profile": "Пока мало данных", "msg_count": 0, "tolik_msg_count": 0})
    u_data["msg_count"] = u_data.get("msg_count", 0) + 1

    if num_buffered > 1:
        print(f"🔗 [ОБЪЕДИНЕНИЕ]: Объединено {num_buffered} сообщений подряд от @{user_name}!")

    base_hist_entry = {
        "ts": now_ts,
        "date_str": now_dt.strftime("%d.%m.%Y"),
        "time_short": now_dt.strftime("%H:%M:%S"),
        "chat_id": chat_id,
        "chat_title": chat_title,
        "user": user_name,
        "text": combined_text,
        "bot_reply": None,
    }

    if not data["should_reply"]:
        active_conversations.pop(conv_key, None)
        memory["history"].append(base_hist_entry)
        save_memory()
        log_dialogue_to_topic(user_name, user_id, combined_text, None, chat_title)
        return

    explicit_ping = has_explicit_ping(combined_text, is_group, data.get("is_reply_to_bot", False))

    only_sticker_or_gif_no_ping = (
        is_group and not explicit_ping and any(m in media_types for m in ["sticker", "gif"])
        and num_buffered == 1 and ("отправил СТИКЕР" in combined_text or "отправил ГИФКУ" in combined_text)
    )

    in_active_dialog = False
    if conv_key in active_conversations and not only_sticker_or_gif_no_ping:
        if now_ts - active_conversations[conv_key] < 35 and last_chat_sender.get(chat_id) in [user_id, BOT_ID, None]:
            in_active_dialog = True
        else:
            active_conversations.pop(conv_key, None)

    if is_group and not explicit_ping and not in_active_dialog:
        last_chat_sender[chat_id] = user_id
        memory["history"].append(base_hist_entry)
        save_memory()
        log_dialogue_to_topic(user_name, user_id, combined_text, None, chat_title)
        return

    if check_reply_rate_spam(user_id):
        memory.setdefault("ignored_users", {})[str(user_id)] = now_ts + 120
        save_memory()
        send_message(chat_id, "подожди пару минут, ты слишком быстро строчишь", message_thread_id=thread_id)
        return

    if not image_parts and chat_id in last_bot_image and now_ts - last_bot_image[chat_id]["time"] < 180:
        image_parts.append(last_bot_image[chat_id]["media"])
        seeing_own_drawing = True

    if not image_parts and chat_id in last_chat_media and now_ts - last_chat_media[chat_id]["time"] < 180:
        kws = ["видео", "ролик", "видос", "гиф", "фот", "картинк", "рисун", "скетч", "скрин", "мем", "глянь", "опиши", "посмотри", "что тут", "че там", "чё там"]
        if any(kw in combined_text.lower() for kw in kws):
            image_parts.append(last_chat_media[chat_id]["media"])
            if last_chat_media[chat_id].get("kind"):
                media_types.append(last_chat_media[chat_id]["kind"])

    fn_ln = f"{u_data.get('first_name', '')} {u_data.get('last_name', '')}".strip()
    user_profile = f"Имя в ТГ: {fn_ln or user_name} | О себе: {u_data.get('bio', 'нет')}\n{u_data.get('profile', 'Пока мало данных')}"
    chat_history = [h for h in memory["history"] if h["chat_id"] == chat_id]

    reply = fetch_ai_response(
        combined_text, chat_history, user_name, user_profile, data["reply_target_info"],
        memory=memory, chat_id=chat_id, image_parts=image_parts, media_types=media_types,
        num_buffered=num_buffered, seeing_own_drawing=seeing_own_drawing,
    )

    if reply == "[STOP]" or not reply.strip():
        active_conversations.pop(conv_key, None)
        last_chat_sender[chat_id] = user_id
        print(f"🛑 [[STOP]]: Толик решил промолчать на реплику @{user_name} в {chat_title}.")
        memory["history"].append(base_hist_entry)
        save_memory()
        log_dialogue_to_topic(user_name, user_id, combined_text, None, chat_title)
        return

    active_conversations[conv_key] = time.time()
    reply_to_id = data["message_id"] if is_group and last_chat_sender.get(chat_id) != user_id else None
    last_chat_sender[chat_id] = user_id

    chunks = reply.split("[MSG]")
    sent_replies = []

    for i, chunk in enumerate(chunks):
        chunk_text = chunk.strip()
        if not chunk_text:
            continue

        photo_gen = re.search(r"\[(GENERATE_PHOTO|EDIT_PHOTO):\s*(.+?)\]", chunk_text)
        if photo_gen:
            tag_type, prompt = photo_gen.group(1), photo_gen.group(2).strip()
            clean_before = chunk_text.replace(photo_gen.group(0), "").strip()
            if clean_before:
                send_message(chat_id, clean_before, message_thread_id=thread_id, reply_to_message_id=reply_to_id if i == 0 else None)
                sent_replies.append(clean_before)
                time.sleep(0.8)

            ref_imgs = image_parts if (tag_type == "EDIT_PHOTO" and image_parts) else None
            img_bytes = generate_image_nano_banana(prompt, reference_images=ref_imgs)
            if img_bytes:
                send_photo(chat_id, img_bytes, message_thread_id=thread_id, reply_to_message_id=reply_to_id if i == 0 else None)
                last_bot_image[chat_id] = {"media": (img_bytes, "image/jpeg"), "prompt": prompt, "time": time.time()}
                last_chat_media[chat_id] = {"media": (img_bytes, "image/jpeg"), "kind": "photo", "time": time.time()}
                sent_replies.append(f"[Толик отправил свой рисунок: {prompt}]")
            continue

        if "[SEND_SELFIE]" in chunk_text:
            config = load_config()
            clean_before = chunk_text.replace("[SEND_SELFIE]", "").strip()
            if clean_before:
                send_message(chat_id, clean_before, message_thread_id=thread_id, reply_to_message_id=reply_to_id if i == 0 else None)
                sent_replies.append(clean_before)
                time.sleep(0.8)
            if config.get("selfies"):
                chosen = random.choice(config["selfies"])
                send_photo(chat_id, chosen, message_thread_id=thread_id)
            continue

        st_match = re.search(r"\[STICKER:\s*(.+?)\]", chunk_text)
        if st_match:
            emotion = st_match.group(1).lower().strip()
            config = load_config()
            if config.get("stickers") and emotion in config["stickers"]:
                send_sticker(chat_id, config["stickers"][emotion], message_thread_id=thread_id, reply_to_message_id=reply_to_id if i == 0 else None)
            chunk_text = chunk_text.replace(st_match.group(0), "").strip()

        if chunk_text:
            send_message(chat_id, chunk_text, message_thread_id=thread_id, reply_to_message_id=reply_to_id if i == 0 else None)
            sent_replies.append(chunk_text)
            if len(chunks) > 1:
                time.sleep(random.uniform(0.8, 1.3))

    full_bot_reply = " [MSG] ".join(sent_replies)
    base_hist_entry["bot_reply"] = full_bot_reply
    memory["history"].append(base_hist_entry)

    u_data["tolik_msg_count"] = u_data.get("tolik_msg_count", 0) + 1
    tolik_cnt = u_data["tolik_msg_count"]
    save_memory()

    log_dialogue_to_topic(user_name, user_id, combined_text, full_bot_reply, chat_title)

    if tolik_cnt % 10 == 0:
        print(f"📋 [ДОСЬЕ (10 ответов Толика)]: Обновляю досье на @{user_name}...")
        threading.Thread(target=update_dossier, args=(user_name, user_id), daemon=True).start()

# ================= ГЛАВНЫЙ ЦИКЛ БОТА =================

def run_bot():
    last_update_id = 0
    print("=" * 75)
    print("🚀 Толик запущен (Gemini 3.6 Flash + Даты сообщений + Max Память + /panel)!")
    print("=" * 75)

    threading.Thread(target=sync_all_users_on_start, daemon=True).start()
    threading.Thread(target=process_buffered_messages, daemon=True).start()
    threading.Thread(target=background_tasks, daemon=True).start()

    synced_chats = set()

    while True:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                json={"offset": last_update_id + 1, "timeout": 30},
                timeout=35,
            )
            if r.status_code != 200:
                continue

            for update in r.json().get("result", []):
                last_update_id = update["update_id"]

                if "callback_query" in update:
                    handle_callback_query(update["callback_query"])
                    continue

                if "my_chat_member" in update:
                    mcm = update["my_chat_member"]
                    new_st = mcm.get("new_chat_member", {}).get("status")
                    if new_st in ["member", "administrator"]:
                        check_and_register_group_chat(mcm.get("chat", {}))
                    continue

                if "message" not in update:
                    continue

                msg = update["message"]
                if msg.get("date", 0) < START_TIME:
                    continue

                chat_id = msg["chat"]["id"]
                thread_id = msg.get("message_thread_id")
                message_id = msg["message_id"]
                user = msg.get("from", {})
                user_name = user.get("username", user.get("first_name", "User"))
                user_id = user.get("id")
                text = msg.get("text", "") or msg.get("caption", "")
                incoming_images = []
                media_types = []
                seeing_own_drawing = False

                is_group = msg["chat"]["type"] in ["group", "supergroup"]
                raw_chat_title = msg["chat"].get("title")
                chat_title = f"Группа «{raw_chat_title}»" if (is_group and raw_chat_title) else ("ЛС" if not is_group else f"Группа ({chat_id})")

                mem_snap = load_memory()
                if chat_id < 0 and chat_id not in synced_chats:
                    synced_chats.add(chat_id)
                    sync_chat_admins(chat_id, mem_snap)
                user_name = register_user_metadata(mem_snap, user, chat_id) or user_name
                if "reply_to_message" in msg and "from" in msg["reply_to_message"]:
                    register_user_metadata(mem_snap, msg["reply_to_message"]["from"], chat_id)
                save_memory()

                if user_name.lower() == ADMIN_USERNAME.lower() and text.strip().startswith("/"):
                    if handle_admin_command(msg, chat_id, thread_id, message_id, text):
                        continue

                cfg_now = load_config()
                if cfg_now.get("main_chat_id") and chat_id == cfg_now.get("main_chat_id"):
                    continue

                if is_group:
                    chat_allowed = check_and_register_group_chat(msg["chat"])
                    if not chat_allowed:
                        continue

                flood_status = check_flood_on_incoming(user_id)
                if flood_status == "MUTED":
                    continue
                elif flood_status == "TRIGGER_MUTE":
                    send_message(chat_id, "эй, полегче, не флуди так жестко, помолчу пару минут", message_thread_id=thread_id)
                    continue

                if "voice" in msg or "video_note" in msg:
                    is_vnote = "video_note" in msg
                    voice_obj = msg.get("voice") or msg.get("video_note")
                    f_bytes, real_mime = get_file_bytes(voice_obj["file_id"], "video/mp4" if is_vnote else "audio/ogg")
                    if f_bytes:
                        rec = transcribe_media_gemini(f_bytes, real_mime, is_video_note=is_vnote)
                        media_types.append("video_note" if is_vnote else "voice")
                        text += f" [{'Видеокружочек' if is_vnote else 'Голосовое сообщение'}: \"{rec or 'неразборчиво'}\"]"

                if "sticker" in msg:
                    st_bytes, st_mime = download_sticker_visual(msg["sticker"])
                    if st_bytes:
                        incoming_images.append((st_bytes, st_mime))
                        media_types.append("sticker")
                    text += " [Собеседник отправил СТИКЕР]"
                elif "reply_to_message" in msg and "sticker" in msg["reply_to_message"]:
                    st_bytes, st_mime = download_sticker_visual(msg["reply_to_message"]["sticker"])
                    if st_bytes:
                        incoming_images.append((st_bytes, st_mime))
                        media_types.append("sticker")
                    if not text:
                        text = "[Собеседник ответил на СТИКЕР]"
                elif "animation" in msg:
                    vid_obj = msg["animation"]
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                        media_types.append("gif")
                        last_chat_media[chat_id] = {"media": (v_bytes, v_mime), "kind": "gif", "time": time.time()}
                    text += " [Собеседник отправил ГИФКУ (GIF)]"
                elif "reply_to_message" in msg and "animation" in msg["reply_to_message"]:
                    vid_obj = msg["reply_to_message"]["animation"]
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                        media_types.append("gif")
                    if not text:
                        text = "[Собеседник ответил на ГИФКУ]"
                elif "video" in msg:
                    vid_obj = msg["video"]
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                        media_types.append("video")
                        last_chat_media[chat_id] = {"media": (v_bytes, v_mime), "kind": "video", "time": time.time()}
                    text += " [Собеседник отправил ВИДЕОРОЛИК]"
                elif "reply_to_message" in msg and "video" in msg["reply_to_message"]:
                    vid_obj = msg["reply_to_message"]["video"]
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                        media_types.append("video")
                    if not text:
                        text = "[Собеседник ответил на ВИДЕОРОЛИК]"
                elif "photo" in msg:
                    img_bytes, mime = get_file_bytes(msg["photo"][-1]["file_id"], "image/jpeg")
                    if img_bytes:
                        incoming_images.append((img_bytes, mime))
                        media_types.append("photo")
                        last_chat_media[chat_id] = {"media": (img_bytes, mime), "kind": "photo", "time": time.time()}
                    text += " [Собеседник прикрепил ФОТО]"
                elif "reply_to_message" in msg and "photo" in msg["reply_to_message"]:
                    rep_id = msg["reply_to_message"].get("from", {}).get("id")
                    img_bytes, mime = get_file_bytes(msg["reply_to_message"]["photo"][-1]["file_id"], "image/jpeg")
                    if img_bytes:
                        incoming_images.append((img_bytes, mime))
                        media_types.append("photo")
                        if rep_id == BOT_ID:
                            seeing_own_drawing = True
                    if not text:
                        text = "[Ответ на ТВОЙ рисунок]" if rep_id == BOT_ID else "[Ответ на фото]"

                if len(text) > 1500:
                    text = text[:1500] + "..."

                reply_msg = msg.get("reply_to_message")
                reply_target_info = "Обычное сообщение в чат."
                should_reply = True
                is_reply_to_bot = False

                if is_group and reply_msg:
                    replied_user_id = reply_msg.get("from", {}).get("id")
                    replied_user_name = reply_msg.get("from", {}).get("username", reply_msg.get("from", {}).get("first_name", "User"))
                    if replied_user_id == BOT_ID:
                        reply_target_info = "Ответ на реплику Толика."
                        is_reply_to_bot = True
                    elif has_explicit_ping(text, is_group, False):
                        reply_target_info = f"Ответ на сообщение @{replied_user_name} с обращением к Толику."
                        is_reply_to_bot = True
                    else:
                        reply_target_info = f"Ответ пользователю @{replied_user_name} (чужой диалог)."
                        should_reply = False

                if not text and not incoming_images:
                    continue

                buffer_key = f"{chat_id}_{user_id}"
                with buffer_lock:
                    if buffer_key not in message_buffer:
                        message_buffer[buffer_key] = {
                            "chat_id": chat_id, "chat_title": chat_title, "user_id": user_id, "user_name": user_name,
                            "thread_id": thread_id, "message_id": message_id, "is_group": is_group,
                            "is_reply_to_bot": is_reply_to_bot, "seeing_own_drawing": seeing_own_drawing,
                            "texts": [text] if text else [], "images": incoming_images, "media_types": media_types,
                            "last_time": time.time(), "should_reply": should_reply, "reply_target_info": reply_target_info,
                        }
                    else:
                        if text and len(message_buffer[buffer_key]["texts"]) < MAX_MESSAGES_IN_BUFFER:
                            if not message_buffer[buffer_key]["texts"] or message_buffer[buffer_key]["texts"][-1] != text:
                                message_buffer[buffer_key]["texts"].append(text)
                                print(f"➕ [ОБЪЕДИНЕНИЕ ({len(message_buffer[buffer_key]['texts'])} шт.)]: Жду ещё {BUFFER_WAIT_SECONDS} сек...")
                        if incoming_images and len(message_buffer[buffer_key]["images"]) < 4:
                            message_buffer[buffer_key]["images"].extend(incoming_images)
                        if media_types:
                            message_buffer[buffer_key]["media_types"].extend(media_types)
                        if seeing_own_drawing:
                            message_buffer[buffer_key]["seeing_own_drawing"] = True
                        message_buffer[buffer_key]["message_id"] = message_id
                        message_buffer[buffer_key]["last_time"] = time.time()
                        if is_reply_to_bot:
                            message_buffer[buffer_key]["is_reply_to_bot"] = True
                        if should_reply:
                            message_buffer[buffer_key]["should_reply"] = True
                            message_buffer[buffer_key]["reply_target_info"] = reply_target_info
            time.sleep(0.4)
        except Exception as e:
            logging.error(f"Main Loop Error: {e}")
            time.sleep(3)

# ================= ВЕБ-СЕРВЕР ДЛЯ ОБЛАКА =================

class RenderWebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        uptime_min = int((time.time() - START_TIME) // 60)
        html = f"<html><body><h2>🦊 Толик жив и работает!</h2><p>Аптайм: {uptime_min} мин.</p></body></html>"
        self.wfile.write(html.encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_fake_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderWebHandler)
    print(f"🌐 [WEB SERVICE]: Имитация веб-сервиса запущена на порту {port}!")
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=start_fake_web_server, daemon=True).start()
    run_bot()
