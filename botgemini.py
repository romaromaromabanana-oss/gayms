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

BUFFER_WAIT_SECONDS = 4.0
MAX_MESSAGES_IN_BUFFER = 7

GEMINI_MAIN_MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
GEMINI_FAST_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]
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

# ================= ЕДИНАЯ ПОТОКОБЕЗОПАСНАЯ БАЗА И АВТОСОХРАНЕНИЕ В GITHUB =================

def get_minsk_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def git_autosave_memory():
    """Мгновенно сохраняет bot_memory.json и bot_config.json в GitHub в фоновом режиме."""
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
            "main_chat_id": None, "diary_topic_id": None, "last_diary_date": "",
            "current_plan": "Планов пока нет.", "known_chats": {}, "selfies": [], "stickers": {},
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
        default_mem = {"history": [], "users": {}, "ignored_users": {}}
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

def find_canonical_user_key(memory, user_name, user_id=None):
    """Находит существующую запись юзера по user_id или нику (без учета регистра), чтобы не плодить дубликаты!"""
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

    existing_key = find_canonical_user_key(memory, raw_name, u_id)
    if existing_key in memory["users"] and username and existing_key != username:
        # Переносим старые данные (включая topic_id!) под актуальный username
        old_data = memory["users"].pop(existing_key)
        memory["users"].setdefault(username, {}).update(old_data)
        key_name = username
    else:
        key_name = existing_key

    u_data = memory["users"].setdefault(key_name, {"profile": "Пока мало данных", "msg_count": 0})
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

    if len(ram_msg_timestamps[user_id]) > 8:
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
    print(f"🌐 [ПОИСК В ИНТЕРНЕТЕ]: Запрос -> \"{query}\"")
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
    except Exception as e:
        logging.error(f"Search Web Error: {e}")

    return "\n".join(results[:max_results]) if results else ""

# ================= TELEGRAM API И МЕДИА =================

def send_message(chat_id, text, message_thread_id=None, reply_to_message_id=None, parse_mode=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if message_thread_id:
        data["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        data["parse_mode"] = parse_mode
    return safe_request(url, data)

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
    except Exception as e:
        logging.error(f"Send Photo Error: {e}")
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
    except Exception as e:
        logging.error(f"Download File Error: {e}")
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
        print(f"🎨 [FLUX FREE ENGINE]: Рисую картинку -> {final_flux_prompt}")
        enc = urllib.parse.quote(final_flux_prompt)
        seed = random.randint(1000, 99999999)
        for flux_model in ["flux", "turbo"]:
            url = f"https://image.pollinations.ai/prompt/{enc}?model={flux_model}&width=1024&height=1024&nologo=true&seed={seed}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
    except Exception as e:
        logging.error(f"Flux Error: {e}")
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

# ================= GEMINI: ГОЛОС, ВИДЕО, ДИАЛОГ, САММАРИ =================

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
                print(f"🧠 [GEMINI ({model_name})]: Формирую ответ Толика...")
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.75,
                        max_output_tokens=600,
                        safety_settings=SAFETY_SETTINGS,
                    ),
                )
                if response and response.text:
                    raw_reply = response.text.strip()
                    print(f"✨ [ОТВЕТ GEMINI ({model_name}) ПОЛУЧЕН]: {raw_reply}")
                    return raw_reply
            except Exception as e:
                logging.error(f"Gemini Main Error ({model_name}): {e}")
                if "blocked" in str(e).lower() or "safety" in str(e).lower():
                    return "ты че? я такое делать не буду."
                continue
        time.sleep(2)
    return "блин, инет лагает... [MSG] сорян"

def generate_fast_summary(prompt_text, system_instruction, retries=2):
    for _ in range(retries):
        for model_name in GEMINI_FAST_MODELS:
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt_text,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction, temperature=0.2, max_output_tokens=450, safety_settings=SAFETY_SETTINGS
                    ),
                )
                if response and response.text:
                    return response.text.strip()
            except Exception:
                continue
        time.sleep(1)
    return None

# ================= СИСТЕМА ДОСЬЕ (БЕЗ ДУБЛИКАТОВ ТОПИКОВ!) =================

def get_or_create_user_topic(main_chat_id, user_name, user_id):
    if not main_chat_id:
        return None
    with topic_lock:
        memory = load_memory()
        canon_key = find_canonical_user_key(memory, user_name, user_id)
        u_data = memory["users"].setdefault(canon_key, {})
        if user_id:
            u_data["user_id"] = user_id

        # Проверяем, нет ли уже сохраненного topic_id у этого user_id под любым ключом
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
                send_message(main_chat_id, f"📁 Создано досье для пользователя @{canon_key}.", message_thread_id=new_topic_id)
                return new_topic_id
        return topic_id

def log_dialogue_to_topic(user_name, user_id, user_text, bot_reply, is_group):
    config = load_config()
    main_chat_id = config.get("main_chat_id")
    if not main_chat_id:
        return
    topic_id = get_or_create_user_topic(main_chat_id, user_name, user_id)
    if not topic_id:
        return
    chat_type = "в Группе" if is_group else "в ЛС"
    log_msg = f"💬 <b>{user_name} ({chat_type}):</b> {user_text}"
    if bot_reply:
        log_msg += f"\n🦊 <b>Толик:</b> {bot_reply}"
    res = send_message(main_chat_id, log_msg, message_thread_id=topic_id, parse_mode="HTML")
    # Если старый топик был удален вручную в ТГ (ошибка thread not found), сбрасываем topic_id
    if not res.get("ok") and "thread" in str(res.get("description", "")).lower():
        memory = load_memory()
        canon_key = find_canonical_user_key(memory, user_name, user_id)
        memory["users"].get(canon_key, {}).pop("topic_id", None)
        save_memory()

def update_dossier(user_name, user_id=None):
    """Накопительное досье: сохраняет старые факты и дополняет их новыми."""
    memory = load_memory()
    canon_key = find_canonical_user_key(memory, user_name, user_id)
    user_history = [h for h in memory["history"] if h.get("user", "").lower() == canon_key.lower()]
    if not user_history:
        return

    lines = []
    for h in user_history[-60:]:
        where = "в ЛС" if h.get("chat_id", 0) > 0 else "в чате"
        line = f"[{where}] {h['user']}: {h['text']}"
        if h.get("bot_reply"):
            line += f"\nТолик: {h['bot_reply']}"
        lines.append(line)

    u_info = memory.get("users", {}).get(canon_key, {})
    old_profile = u_info.get("profile", "Пока мало данных")
    fn = f"{u_info.get('first_name', '')} {u_info.get('last_name', '')}".strip()
    bio = u_info.get("bio", "")
    meta_str = f"Ник: @{canon_key} | Имя в ТГ: {fn or 'не указано'} | О себе (bio): {bio or 'нет'}"

    prompt = (
        f"Данные профиля: {meta_str}\n\n"
        f"ПРЕДЫДУЩЕЕ ДОСЬЕ (сохрани важные факты отсюда!):\n{old_profile}\n\n"
        f"СВЕЖИЕ СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ:\n" + "\n".join(lines) + "\n\n"
        f"Обнови и дополни ДОСЬЕ на @{canon_key} СТРОГО по этим реальным сообщениям (ничего не выдумывай!):\n"
        f"- Как зовут, какой пол, манера общения, реальные факты и увлечения.\n"
        f"- Отношения с Толиком и участниками чата.\n"
        f"Объедини старые факты с новыми кратко от 3-го лица."
    )
    try:
        response = generate_fast_summary(prompt, "Ты хранитель долгосрочной памяти. Пиши только подтвержденные факты.")
        if response and response != "IGNORE":
            profile = response.strip()
            memory = load_memory()
            memory.setdefault("users", {}).setdefault(canon_key, {})["profile"] = profile
            save_memory(push_git=True)

            config = load_config()
            main_chat_id = config.get("main_chat_id")
            if main_chat_id and (user_id or u_info.get("user_id")):
                topic_id = get_or_create_user_topic(main_chat_id, canon_key, user_id or u_info.get("user_id"))
                if topic_id:
                    send_message(main_chat_id, f"🔄 <b>Обновлено досье на {canon_key}:</b>\n\n{profile}", message_thread_id=topic_id, parse_mode="HTML")
    except Exception as e:
        print(f"❌ ОШИБКА ДОСЬЕ: {e}")

def sync_all_users_on_start():
    try:
        memory = load_memory()
        chat_ids = {h.get("chat_id") for h in memory.get("history", []) if h.get("chat_id", 0) < 0}
        for cid in chat_ids:
            sync_chat_admins(cid, memory)
        save_memory()
    except Exception as e:
        logging.error(f"Startup Sync Error: {e}")

def build_genius_social_and_cross_memory(chat_id, current_user, text, memory):
    all_history = memory.get("history", [])
    all_users = memory.get("users", {})

    other_chat_msgs = [h for h in all_history if h.get("user") == current_user and h.get("chat_id") not in [0, chat_id]]
    cross_lines = []
    for h in other_chat_msgs[-25:]:
        place = "в ЛС (лично)" if h.get("chat_id", 0) > 0 else "в другом чате"
        entry = f"[{place} {h.get('time_short', '')}] {current_user}: {h.get('text', '')}"
        if h.get("bot_reply"):
            entry += f" -> Ты ответил: {h.get('bot_reply')}"
        cross_lines.append(entry)
    cross_memory_str = "\n".join(cross_lines) if cross_lines else "Вне этого чата пока не общались."

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

        display = f"@{uname}"
        if fn or ln:
            display += f" | Имя в ТГ: {fn} {ln}".rstrip()
        if title:
            display += f" | Титул: {title}"
        if bio:
            display += f" | О себе: {bio}"

        u_msgs = [h["text"] for h in all_history if h.get("user") == u_key and h.get("text")][-5:]
        quotes_str = f" | Реальные реплики: {' // '.join(u_msgs)}" if u_msgs else " | Пока молчит в чате"

        short_prof = profile.replace("\n", " ")[:180]
        participants_summary.append(f"• {display} — Досье: {short_prof}{quotes_str}")

        tokens = [t.lower() for t in [u_key, uname, fn, ln, title] if t and len(t) >= 3]
        for t in list(tokens):
            if len(t) >= 4:
                tokens.append(t[:4])

        if any(tok in text_lower for tok in tokens):
            mentioned_details.append(
                f"📌 РЕАЛЬНЫЕ ДАННЫЕ ПРО УПОМЯНУТОГО ЧЕЛОВЕКА ({display}):\n"
                f"Досье: {profile}\n"
                f"Его/её настоящие сообщения в чате: {' // '.join(u_msgs) if u_msgs else 'сообщений от него пока нет'}"
            )

    words = [w for w in re.findall(r"[а-яёa-z0-9_]{3,}", text_lower) if w not in [
        "толик", "толя", "толян", "придумай", "стих", "про", "кто", "это", "такой", "такая",
        "расскажи", "знаешь", "помнишь", "что", "как", "привет", "дела", "тебя", "мне", "опиши"
    ]]
    if words:
        found_in_history = []
        for h in all_history[-300:]:
            h_txt = (h.get("text", "") + " " + (h.get("bot_reply") or "")).lower()
            if any(w in h_txt for w in words):
                pl = "в ЛС" if h.get("chat_id", 0) > 0 else "в чате"
                found_in_history.append(
                    f"[{pl}] {h.get('user')}: {h.get('text')}"
                    + (f" -> Толик: {h.get('bot_reply')}" if h.get("bot_reply") else "")
                )
        if found_in_history:
            mentioned_details.append(
                "📌 УПОМИНАНИЯ ЭТИХ СЛОВ/ИМЕН В ПРОШЛЫХ ПЕРЕПИСКАХ (ЛС И ЧАТ):\n"
                + "\n".join(found_in_history[-8:])
            )

    roster_str = "\n".join(participants_summary[:45]) if participants_summary else "Пока никого нет."
    mentioned_str = "\n\n".join(mentioned_details)
    return cross_memory_str, roster_str, mentioned_str

# ================= ИНТЕЛЛЕКТ И КАНОН ТОЛИКА =================

def fetch_ai_response(
    text, chat_history, user_name, user_profile, reply_target_info, memory, chat_id,
    image_parts=None, media_types=None, explicit_ping=False, post_dialog_check=False, seeing_own_drawing=False,
):
    now = get_minsk_time()
    config = load_config()
    current_plan = config.get("current_plan", "Планов пока нет.")
    stickers = config.get("stickers", {})
    sticker_list = ", ".join(stickers.keys()) if stickers else "нет"
    has_selfies = len(config.get("selfies", [])) > 0
    has_input_media = bool(image_parts)
    media_types = media_types or []

    cross_memory_str, roster_str, mentioned_str = build_genius_social_and_cross_memory(chat_id, user_name, text, memory)

    if explicit_ping:
        address_rule = (
            "СОБЕСЕДНИК ОБРАТИЛСЯ ЛИЧНО К ТЕБЕ. Обязательно ответь ему в характере Толика! "
            "Слова STOP_DIALOG и IGNORE использовать ЗАПРЕЩЕНО."
        )
    elif post_dialog_check:
        address_rule = (
            "РЕЖИМ АНАЛИЗА ПОСЛЕ ДИАЛОГА:\n"
            "Ты только что общался с этим пользователем, но сейчас он тебя не тегнул.\n"
            "1) Если он продолжает диалог с тобой, отвечает тебе, кинул стикер/гифку тебе или комментирует твой рисунок — ответь как Толик.\n"
            "2) Если он уже НЕ ХОЧЕТ с тобой общаться, закрыл тему или пишет другим — ВЫВЕДИ РОВНО ОДНО СЛОВО: STOP_DIALOG"
        )
    else:
        address_rule = "Если с тобой не хотят общаться — выведи ровно одно слово: STOP_DIALOG"

    sys_prompt = (
        f"ТВОЯ ЛИЧНОСТЬ И ИНТЕЛЛЕКТ:\n"
        f"Тебя зовут Толик. Тебе 17 лет. Ты живешь в Минске.\n"
        f"Ты милый, сообразительный, эрудированный парень, душа компании. Ты открытый гей, фембой и фурри (фурсона — бело-голубой кот).\n"
        f"Ты рисуешь скетчи в блокноте карандашом, любишь уют, музыку, аниме, технологии.\n"
        f"ВАЖНО ПРО ГОЛОСОВЫЕ ЧАТЫ: Общаешься ТОЛЬКО текстом. От звонков отмазывайся.\n"
        f"Сейчас: {now.strftime('%H:%M %d.%m.%Y')}. Твой план на день:\n{current_plan}\n\n"
        f"{address_rule}\n\n"
        f"ВАЖНО ПРО РАЗЛИЧЕНИЕ ТИПОВ МЕДИА (СТИКЕР vs ГИФКА vs ВИДЕО vs ФОТО):\n"
        f"- Если собеседник прислал СТИКЕР — ты знаешь, что это именно СТИКЕР в Телеграме! НИКОГДА не называй стикер 'картинкой', 'фоткой', 'артом' или 'рисунком'! Реагируй на эмоцию/вайб/действие персонажа со стикера (и можешь отправить стикер в ответ через [STICKER: ...]).\n"
        f"- Если собеседник прислал ГИФКУ (GIF-анимацию) — ты знаешь, что это именно ГИФКА (анимированный мем без звука), а не обычная картинка и не видео со звуком! Можешь называть её гифкой и комментировать действие на ней.\n"
        f"- Если собеседник прислал ВИДЕО — это видеоролик со звуком.\n"
        f"- Если собеседник прислал ФОТО / КАРТИНКУ — тогда это реально фотка, скриншот или рисунок.\n\n"
        f"ПРАВИЛО ЧЕСТНОСТИ И ЗНАНИЯ УЧАСТНИКОВ (БЕЗ ВЫДУМОК!):\n"
        f"1. Ниже дан список ВСЕХ участников чата и твоя память из ЛС.\n"
        f"2. СТРОГО ЗАПРЕЩЕНО ВЫДУМЫВАТЬ ФАКТЫ О ЛЮДЯХ ОТ БАЛДЫ.\n"
        f"3. ЗАПРЕЩЕНО использовать тег [SEARCH: ...] для поиска участников чата, друзей и их ников!\n"
        f"4. В общем чате веди себя тактично: ты помнишь переписки из ЛС, но НЕ сливай чужие личные секреты на публику.\n\n"
        f"ПРАВИЛА СТИЛЯ:\n"
        f"1. ПИШИ СТРОГО СТРОЧНЫМИ БУКВАМИ (маленькими). Точки в конце коротких реплик не ставь.\n"
        f"2. НЕ ЗДОРОВАЙСЯ ('привет', 'ку'), если диалог уже идет!\n"
        f"3. Отвечай живо, короткими сообщениями от 1-го лица, разделяя мысли тегом [MSG].\n"
        f"4. ЗАПРЕЩЕНО говорить слова: 'бот', 'ии', 'нейросеть', 'сгенерирую', 'промпт', 'досье'. Ты живой парень.\n"
        f"5. НИКАКИХ ЭМОДЗИ В ТЕКСТЕ.\n\n"
        f"ДОСТУПНЫЕ СПЕЦ-ТЕГИ:\n"
        f"- [SEARCH: запрос] — ТОЛЬКО для новостей мира, лекарств, игр и научных фактов.\n"
        f"- [STICKER: описание] — отправить стикер. Доступны: {sticker_list}.\n"
        f"- [SEND_SELFIE] — прислать готовое селфи. {'(фото пока нет)' if not has_selfies else ''}\n"
        f"- [GENERATE_PHOTO: detailed prompt in english] — нарисовать скетч кота или сфоткать что-то из жизни.\n"
        f"- [EDIT_PHOTO: detailed edit instructions in english] — дорисовать/изменить присланную или свою картинку.\n\n"
        f"=== ДОСЬЕ НА ТЕКУЩЕГО СОБЕСЕДНИКА (@{user_name}) ===\n{user_profile}\n\n"
        f"=== ЧТО ВЫ ОБСУЖДАЛИ С @{user_name} ВНЕ ЭТОГО ЧАТА (В ЛС / ДРУГИХ ЧАТАХ) ===\n{cross_memory_str}\n\n"
        f"=== СПИСОК УЧАСТНИКОВ ЧАТА И ЗНАКОМЫХ ===\n{roster_str}\n"
    )

    if mentioned_str:
        sys_prompt += f"\n=== НАЙДЕНО В ПАМЯТИ ПО ИМЕНАМ/СЛОВАМ ИЗ СООБЩЕНИЯ ===\n{mentioned_str}\n"

    history_text = "\n".join(
        [f"[{h.get('time_short')}] {h['user']}: {h['text']}" + (f"\nТолик: {h['bot_reply']}" if h.get("bot_reply") else "") for h in chat_history[-50:]]
    )

    reply_context = f"[Информация о сообщении: {reply_target_info}]"
    if seeing_own_drawing:
        reply_context += " [К сообщению прикреплена ТВОЯ СОБСТВЕННАЯ КАРТИНКА (рисунок/фото), которую ты сам только что сделал — ты видишь её глазами!]"
    elif "sticker" in media_types:
        reply_context += " [ВАЖНО: Собеседник отправил СТИКЕР в Телеграме! Это именно СТИКЕР (реакция/эмоция), а НЕ фотография и НЕ картинка! Оцени эмоцию/посыл на стикере, не называй его 'картинкой' или 'рисунком'!]"
    elif "gif" in media_types:
        reply_context += " [ВАЖНО: Собеседник отправил ГИФКУ (GIF-анимацию без звука)! Это именно ГИФКА, а не фото и не обычное видео!]"
    elif "video" in media_types:
        reply_context += " [ВАЖНО: Собеседник отправил ВИДЕОРОЛИК со звуком! Посмотри видеоряд и послушай звук напрямую!]"
    elif "photo" in media_types:
        reply_context += " [Собеседник отправил ФОТОГРАФИЮ / КАРТИНКУ — посмотри на неё своими глазами!]"

    footer = (
        "[Ответь собеседнику как Толик! Слова STOP_DIALOG и IGNORE использовать ЗАПРЕЩЕНО!]"
        if explicit_ping
        else "[Если собеседник продолжает разговор с тобой — ответь как Толик. Если с тобой больше не хотят общаться — выведи: STOP_DIALOG]"
    )

    final_prompt = f"Контекст переписки в чате:\n{history_text}\n\n{reply_context}\nНовое сообщение от {user_name}:\n{text}\n\n{footer}"
    reply = generate_main_dialogue(final_prompt, sys_prompt, image_parts=image_parts, retries=2)
    clean_up = reply.strip().upper()

    if ("STOP_DIALOG" in clean_up or clean_up == "IGNORE") and explicit_ping:
        reply = generate_main_dialogue(final_prompt + "\n\nОтветь собеседнику прямо сейчас как Толик!", sys_prompt, image_parts=image_parts, retries=2)
        clean_up = reply.strip().upper()

    if "STOP_DIALOG" in clean_up or clean_up == "IGNORE":
        return "STOP_DIALOG"

    search_match = re.search(r"\[SEARCH:\s*(.+?)\]", reply)
    if search_match:
        search_query = search_match.group(1).strip()
        reply_without_tag = re.sub(r"\[SEARCH:\s*.+?\]", "", reply).strip()
        search_data = search_web(search_query)
        if search_data:
            updated_prompt = (
                f"{final_prompt}\n\n[НАЙДЕНО В ИНТЕРНЕТЕ ПО ЗАПРОСУ '{search_query}']:\n{search_data}\n\n"
                f"Перескажи суть найденного своими словами кратко и по-человечески (через [MSG]), без 'привет'."
            )
            new_reply = generate_main_dialogue(updated_prompt, sys_prompt, image_parts=image_parts, retries=2)
            reply = re.sub(r"\[SEARCH:\s*.+?\]", "", new_reply).strip()
        else:
            reply = reply_without_tag if reply_without_tag else "хм, даже в инете про это тишина"

    return reply

# ================= АВТО-ЗАДАЧИ И СТИКЕРПАКИ =================

def background_tasks():
    last_git_push = time.time()
    while True:
        try:
            time.sleep(60)
            now = get_minsk_time()
            config = load_config()
            main_chat = config.get("main_chat_id")

            # Каждые 10 минут пушим актуальную память в GitHub
            if time.time() - last_git_push > 600:
                last_git_push = time.time()
                git_autosave_memory()

            if not main_chat:
                continue

            today_str = now.strftime("%d.%m.%Y")
            if now.hour >= 8 and config.get("last_diary_date") != today_str:
                prompt = f"Составь краткий план на день на сегодня ({today_str}). Ты Толик (17 лет, Минск, фурри). Напиши со строчной буквы."
                res = generate_fast_summary(prompt, "Пиши план для личного дневника.", retries=2)
                if res and "лагает" not in res:
                    config["current_plan"] = res
                    config["last_diary_date"] = today_str
                    save_config(push_git=True)
                    send_message(main_chat, f"📅 <b>Планы на {today_str}</b>\n\n{res}", message_thread_id=config.get("diary_topic_id"), parse_mode="HTML")

            memory = load_memory()
            if len(memory["history"]) > 1200:
                old_hist = memory["history"][:250]
                memory["history"] = memory["history"][250:]
                hist_text = "\n".join([f"{h['user']}: {h['text']}" for h in old_hist])
                sum_res = generate_fast_summary(f"Выдели реальные факты о людях из этого диалога (3-5 предложений):\n{hist_text}", "Ты архиватор фактов.", retries=2)
                if sum_res and "IGNORE" not in sum_res and "STOP_DIALOG" not in sum_res:
                    memory["history"].insert(0, {"time_short": "ПАМЯТЬ", "chat_id": 0, "user": "SYSTEM", "text": sum_res, "bot_reply": None})
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
    return any(name in t_low for name in ["толик", "толя", "толян", "толику", "толиком", "толясик"])

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
    combined_text = "\n".join(data["texts"])
    image_parts = list(data.get("images", []))
    media_types = list(data.get("media_types", []))
    seeing_own_drawing = data.get("seeing_own_drawing", False)
    memory = load_memory()
    now = get_minsk_time()
    conv_key = f"{chat_id}_{user_id}"

    user_name = find_canonical_user_key(memory, raw_user_name, user_id)

    if len(data["texts"]) > 1:
        print(f"🔗 [СКЛЕЙКА]: Склеено {len(data['texts'])} сообщений от @{user_name}!")

    memory["users"].setdefault(user_name, {"profile": "Пока мало данных", "msg_count": 0})
    memory["users"][user_name]["msg_count"] = memory["users"][user_name].get("msg_count", 0) + 1
    msg_cnt = memory["users"][user_name]["msg_count"]

    if not data["should_reply"]:
        active_conversations.pop(conv_key, None)
        memory["history"].append({"time_short": now.strftime("%H:%M"), "chat_id": chat_id, "user": user_name, "text": combined_text, "bot_reply": None})
        save_memory()
        log_dialogue_to_topic(user_name, user_id, combined_text, None, is_group)
        if msg_cnt == 2 or msg_cnt % 6 == 0:
            threading.Thread(target=update_dossier, args=(user_name, user_id), daemon=True).start()
        return

    explicit_ping = has_explicit_ping(combined_text, is_group, data.get("is_reply_to_bot", False))
    in_active_dialog = False
    if conv_key in active_conversations:
        if time.time() - active_conversations[conv_key] < 180:
            in_active_dialog = True
        else:
            active_conversations.pop(conv_key, None)

    if is_group and not explicit_ping and not in_active_dialog:
        print(f"💤 [СТАНДАРТНЫЙ РЕЖИМ]: @{user_name} написал без пинга -> сохранено в историю.")
        memory["history"].append({"time_short": now.strftime("%H:%M"), "chat_id": chat_id, "user": user_name, "text": combined_text, "bot_reply": None})
        save_memory()
        log_dialogue_to_topic(user_name, user_id, combined_text, None, is_group)
        if msg_cnt == 2 or msg_cnt % 6 == 0:
            threading.Thread(target=update_dossier, args=(user_name, user_id), daemon=True).start()
        return

    if check_reply_rate_spam(user_id):
        memory.setdefault("ignored_users", {})[str(user_id)] = time.time() + 120
        save_memory()
        send_message(chat_id, "подожди пару минут, ты слишком быстро строчишь", message_thread_id=thread_id)
        return

    if not image_parts and chat_id in last_bot_image and time.time() - last_bot_image[chat_id]["time"] < 180:
        image_parts.append(last_bot_image[chat_id]["media"])
        seeing_own_drawing = True
        print("🎨👁 [ЗРЕНИЕ ТОЛИКА]: Толик смотрит на свою недавно созданную картинку!")

    if not image_parts and chat_id in last_chat_media and time.time() - last_chat_media[chat_id]["time"] < 180:
        kws = ["видео", "ролик", "видос", "гиф", "фот", "картинк", "рисун", "скетч", "скрин", "мем", "глянь", "опиши", "посмотри", "что тут", "че там", "чё там"]
        if any(kw in combined_text.lower() for kw in kws):
            image_parts.append(last_chat_media[chat_id]["media"])
            if last_chat_media[chat_id].get("kind"):
                media_types.append(last_chat_media[chat_id]["kind"])

    u_info = memory.get("users", {}).get(user_name, {})
    fn_ln = f"{u_info.get('first_name', '')} {u_info.get('last_name', '')}".strip()
    user_profile = f"Имя в ТГ: {fn_ln or user_name} | О себе: {u_info.get('bio', 'нет')}\n{u_info.get('profile', 'Пока мало данных')}"
    chat_history = [h for h in memory["history"] if h["chat_id"] == chat_id]

    post_dialog_check = is_group and not explicit_ping and in_active_dialog
    reply = fetch_ai_response(
        combined_text, chat_history, user_name, user_profile, data["reply_target_info"],
        memory=memory, chat_id=chat_id, image_parts=image_parts, media_types=media_types,
        explicit_ping=explicit_ping, post_dialog_check=post_dialog_check, seeing_own_drawing=seeing_own_drawing,
    )

    if reply == "STOP_DIALOG":
        active_conversations.pop(conv_key, None)
        print(f"🛑 [STOP_DIALOG]: Диалог с @{user_name} завершен -> включен стандартный режим!")
        memory["history"].append({"time_short": now.strftime("%H:%M"), "chat_id": chat_id, "user": user_name, "text": combined_text, "bot_reply": None})
        save_memory()
        log_dialogue_to_topic(user_name, user_id, combined_text, None, is_group)
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
            print(f"💬 [ОТПРАВКА TG]: {chunk_text}")
            send_message(chat_id, chunk_text, message_thread_id=thread_id, reply_to_message_id=reply_to_id if i == 0 else None)
            sent_replies.append(chunk_text)
            time.sleep(random.uniform(0.8, 1.3))

    full_bot_reply = " [MSG] ".join(sent_replies)
    memory["history"].append({"time_short": now.strftime("%H:%M"), "chat_id": chat_id, "user": user_name, "text": combined_text, "bot_reply": full_bot_reply})
    save_memory()
    log_dialogue_to_topic(user_name, user_id, combined_text, full_bot_reply, is_group)

    if msg_cnt == 2 or msg_cnt % 5 == 0:
        threading.Thread(target=update_dossier, args=(user_name, user_id), daemon=True).start()

# ================= ГЛАВНЫЙ ЦИКЛ БОТА =================

def run_bot():
    last_update_id = 0
    print("=" * 70)
    print("🚀 Толик запущен (Точное различение Стикеров/Гифок + Автосохранение топиков)!")
    print("=" * 70)

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

                mem_snap = load_memory()
                if chat_id < 0 and chat_id not in synced_chats:
                    synced_chats.add(chat_id)
                    sync_chat_admins(chat_id, mem_snap)
                user_name = register_user_metadata(mem_snap, user, chat_id) or user_name
                if "reply_to_message" in msg and "from" in msg["reply_to_message"]:
                    register_user_metadata(mem_snap, msg["reply_to_message"]["from"], chat_id)
                save_memory()

                flood_status = check_flood_on_incoming(user_id)
                if flood_status == "MUTED":
                    continue
                elif flood_status == "TRIGGER_MUTE":
                    send_message(chat_id, "эй, полегче, не флуди так жестко [MSG] помолчу пару минут", message_thread_id=thread_id)
                    continue

                # --- Команды Админа ---
                if user_name.lower() == ADMIN_USERNAME.lower():
                    if text.strip().lower() == "/mainchat":
                        cfg = load_config()
                        cfg["main_chat_id"] = chat_id
                        save_config(push_git=True)
                        send_message(chat_id, "✅ Основной чат для досье установлен и сохранен в GitHub!", message_thread_id=thread_id)
                        continue
                    if text.strip().lower() == "/setdiary":
                        cfg = load_config()
                        cfg["main_chat_id"] = chat_id
                        cfg["diary_topic_id"] = thread_id
                        save_config(push_git=True)
                        send_message(chat_id, "✅ Тема установлена для дневника и сохранена в GitHub!", message_thread_id=thread_id)
                        continue
                    if text.strip().lower().startswith("/bind"):
                        parts = text.strip().split(maxsplit=1)
                        target_u = parts[1].lstrip("@") if len(parts) > 1 else user_name
                        canon_t = find_canonical_user_key(mem_snap, target_u)
                        mem_snap["users"].setdefault(canon_t, {})["topic_id"] = thread_id
                        save_memory(push_git=True)
                        send_message(chat_id, f"✅ Этот топик успешно привязан к досье @{canon_t} и сохранен в GitHub!", message_thread_id=thread_id)
                        continue
                    if text.startswith("/set_looks") and "reply_to_message" in msg and "photo" in msg["reply_to_message"]:
                        photo_id = msg["reply_to_message"]["photo"][-1]["file_id"]
                        cfg = load_config()
                        cfg["selfies"].append(photo_id)
                        save_config(push_git=True)
                        send_message(chat_id, "✅ Фотка добавлена в канон Толика!", message_thread_id=thread_id)
                        continue
                    if text.startswith("/add_stickers") and "reply_to_message" in msg and "sticker" in msg["reply_to_message"]:
                        set_name = msg["reply_to_message"]["sticker"].get("set_name")
                        if set_name:
                            threading.Thread(target=parse_sticker_set, args=(set_name, chat_id, thread_id), daemon=True).start()
                        continue
                    if text.startswith("/stats"):
                        mem = load_memory()
                        cfg = load_config()
                        stats = (
                            f"📊 <b>Статистика Толика:</b>\n"
                            f"- Знакомых участников: {len(mem['users'])}\n"
                            f"- Сообщений в памяти: {len(mem['history'])}\n"
                            f"- Стикеров: {len(cfg.get('stickers', {}))}\n"
                            f"- Каноничных фото: {len(cfg.get('selfies', []))}"
                        )
                        send_message(chat_id, stats, message_thread_id=thread_id, reply_to_message_id=message_id, parse_mode="HTML")
                        continue

                # --- Не отправляем ответы в сам технический чат логов досье ---
                cfg_now = load_config()
                if cfg_now.get("main_chat_id") and chat_id == cfg_now.get("main_chat_id"):
                    continue

                # --- 1. Голосовые и видеокружочки ---
                if "voice" in msg or "video_note" in msg:
                    is_vnote = "video_note" in msg
                    voice_obj = msg.get("voice") or msg.get("video_note")
                    f_bytes, real_mime = get_file_bytes(voice_obj["file_id"], "video/mp4" if is_vnote else "audio/ogg")
                    if f_bytes:
                        rec = transcribe_media_gemini(f_bytes, real_mime, is_video_note=is_vnote)
                        media_types.append("video_note" if is_vnote else "voice")
                        text += f" [{'Видеокружочек' if is_vnote else 'Голосовое сообщение'}: \"{rec or 'неразборчиво'}\"]"

                # --- 2. СТИКЕРЫ (в сообщении или в реплае) ---
                if "sticker" in msg:
                    st_bytes, st_mime = download_sticker_visual(msg["sticker"])
                    if st_bytes:
                        incoming_images.append((st_bytes, st_mime))
                        media_types.append("sticker")
                    text += " [Собеседник отправил СТИКЕР (это именно стикер в Телеграме, а не картинка и не фото!)]"
                elif "reply_to_message" in msg and "sticker" in msg["reply_to_message"]:
                    st_bytes, st_mime = download_sticker_visual(msg["reply_to_message"]["sticker"])
                    if st_bytes:
                        incoming_images.append((st_bytes, st_mime))
                        media_types.append("sticker")
                    if not text:
                        text = "[Собеседник ответил на СТИКЕР]"

                # --- 3. ГИФКИ (animation — в ТГ гифки приходят как animation, иногда с полем video) ---
                elif "animation" in msg:
                    vid_obj = msg["animation"]
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                        media_types.append("gif")
                        last_chat_media[chat_id] = {"media": (v_bytes, v_mime), "kind": "gif", "time": time.time()}
                    text += " [Собеседник отправил ГИФКУ (GIF-анимацию без звука, это именно гифка, а не фото!)]"
                elif "reply_to_message" in msg and "animation" in msg["reply_to_message"]:
                    vid_obj = msg["reply_to_message"]["animation"]
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                        media_types.append("gif")
                    if not text:
                        text = "[Собеседник ответил на ГИФКУ]"

                # --- 4. ОБЫЧНЫЕ ВИДЕО ---
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

                # --- 5. ФОТО / КАРТИНКИ ---
                elif "photo" in msg:
                    img_bytes, mime = get_file_bytes(msg["photo"][-1]["file_id"], "image/jpeg")
                    if img_bytes:
                        incoming_images.append((img_bytes, mime))
                        media_types.append("photo")
                        last_chat_media[chat_id] = {"media": (img_bytes, mime), "kind": "photo", "time": time.time()}
                    text += " [Собеседник прикрепил ФОТО / КАРТИНКУ]"
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

                is_group = msg["chat"]["type"] in ["group", "supergroup"]
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
                    elif any(name in text.lower() for name in ["толик", "толя", "толян"]):
                        reply_target_info = f"Ответ на сообщение @{replied_user_name} с обращением к Толику."
                        is_reply_to_bot = True
                    else:
                        reply_target_info = f"Ответ пользователю @{replied_user_name} (чужой диалог)."
                        should_reply = False

                print(f"\n📩 [СООБЩЕНИЕ] От: @{user_name} | {reply_target_info}\n   Текст: \"{text}\"")
                if not text and not incoming_images:
                    continue

                buffer_key = f"{chat_id}_{user_id}"
                with buffer_lock:
                    if buffer_key not in message_buffer:
                        message_buffer[buffer_key] = {
                            "chat_id": chat_id, "user_id": user_id, "user_name": user_name,
                            "thread_id": thread_id, "message_id": message_id, "is_group": is_group,
                            "is_reply_to_bot": is_reply_to_bot, "seeing_own_drawing": seeing_own_drawing,
                            "texts": [text] if text else [], "images": incoming_images, "media_types": media_types,
                            "last_time": time.time(), "should_reply": should_reply, "reply_target_info": reply_target_info,
                        }
                    else:
                        if text and len(message_buffer[buffer_key]["texts"]) < MAX_MESSAGES_IN_BUFFER:
                            if not message_buffer[buffer_key]["texts"] or message_buffer[buffer_key]["texts"][-1] != text:
                                message_buffer[buffer_key]["texts"].append(text)
                                print(f"➕ [СКЛЕЙКА ({len(message_buffer[buffer_key]['texts'])} шт.)]: Жду ещё {BUFFER_WAIT_SECONDS} сек...")
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
