import base64
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ADMIN_USERNAME = "Belugahgfgh"

BUFFER_WAIT_SECONDS = 4.0
MAX_MESSAGES_IN_BUFFER = 7

GEMINI_MAIN_MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
GEMINI_FAST_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]
NANO_BANANA_MODELS = ["gemini-3.1-flash-lite-image", "gemini-nano-banana-2.1", "gemini-3.1-flash-image"]

BOT_ID = int(TELEGRAM_TOKEN.split(":")[0])
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

active_conversations = {}
last_chat_sender = {}
last_chat_media = {}
last_bot_image = {}

ram_msg_timestamps = {}
ram_reply_timestamps = {}
user_generating_lock = set()
enriched_user_ids = set()

# ================= БАЗА ДАННЫХ И ПРОФИЛИ УЧАСТНИКОВ =================

def get_minsk_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def load_config():
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
    return default_cfg

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def load_memory():
    default_mem = {"history": [], "users": {}, "ignored_users": {}}
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                default_mem.update(json.load(f))
        except Exception:
            pass
    return default_mem

def save_memory(memory):
    with memory_lock:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)

def safe_request(url, json_data=None, method="POST"):
    try:
        r = requests.post(url, json=json_data, timeout=15) if method == "POST" else requests.get(url, timeout=15)
        return r.json()
    except Exception as e:
        logging.error(f"TG API Error ({url}): {e}")
        return {"ok": False}

def fetch_tg_user_bio_and_names(user_id):
    """Достает из Telegram API точное Имя, Фамилию, @username и описание профиля (О себе / bio)."""
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
    """Сохраняет полные данные об участнике чата: @username, Имя, Фамилию, БИО и титул."""
    if not user_obj or user_obj.get("id") == BOT_ID:
        return None
    u_id = user_obj.get("id")
    username = user_obj.get("username") or ""
    first_name = user_obj.get("first_name") or ""
    last_name = user_obj.get("last_name") or ""
    key_name = username if username else (first_name if first_name else f"User_{u_id}")

    # Ищем, не был ли этот юзер записан ранее под другим ключом с тем же user_id
    for k, v in list(memory["users"].items()):
        if v.get("user_id") == u_id and k != key_name and username:
            memory["users"][key_name] = memory["users"].pop(k)
            break

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
    """Синхронизирует участников/админов группы через Telegram API, чтобы знать их Имя и Фамилию."""
    if not chat_id or chat_id > 0:
        return
    res = safe_request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatAdministrators?chat_id={chat_id}", method="GET")
    if res.get("ok"):
        for adm in res.get("result", []):
            u = adm.get("user", {})
            title = adm.get("custom_title", "")
            register_user_metadata(memory, u, chat_id, custom_title=title)

# ================= ЗАЩИТА ОТ СПАМА И ФЛУДА =================

def check_flood_on_incoming(user_id):
    now = time.time()
    memory = load_memory()
    ignore_list = memory.get("ignored_users", {})
    if str(user_id) in ignore_list:
        if now < ignore_list[str(user_id)]:
            return "MUTED"
        ignore_list.pop(str(user_id), None)
        save_memory(memory)

    times = ram_msg_timestamps.setdefault(user_id, [])
    times.append(now)
    ram_msg_timestamps[user_id] = [t for t in times if now - t < 12]

    if len(ram_msg_timestamps[user_id]) > 8:
        memory.setdefault("ignored_users", {})[str(user_id)] = now + 120
        save_memory(memory)
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

# ================= ПОИСК В ИНТЕРНЕТЕ (WIKIPEDIA + DUCKDUCKGO БЕЗ 429) =================

def search_web(query, max_results=3):
    print(f"🌐 [ПОИСК В ИНТЕРНЕТЕ]: Запрос -> \"{query}\"")
    results = []
    # 1. Быстрый поиск по Русской Википедии
    try:
        w_url = (
            f"https://ru.wikipedia.org/w/api.php?action=query&list=search"
            f"&srsearch={urllib.parse.quote(query)}&utf8=&format=json&srlimit=2"
        )
        wr = requests.get(w_url, headers={"User-Agent": "TolikBot/1.0"}, timeout=8)
        if wr.status_code == 200:
            for item in wr.json().get("query", {}).get("search", []):
                clean_s = re.sub(r"<.*?>", "", item.get("snippet", "")).strip()
                if clean_s:
                    results.append(f"{item.get('title')}: {clean_s}")
    except Exception:
        pass

    # 2. Поиск через DuckDuckGo
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

    if results:
        summary = "\n".join(results[:max_results])
        print(f"✅ [ПОИСК]: Найдена информация ({len(summary)} символов)")
        return summary
    return ""

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
                print(f"✅ [FLUX ({flux_model})]: Изображение создано!")
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
                        system_instruction=system_instruction, temperature=0.2, max_output_tokens=400, safety_settings=SAFETY_SETTINGS
                    ),
                )
                if response and response.text:
                    return response.text.strip()
            except Exception:
                continue
        time.sleep(1)
    return None

# ================= СИСТЕМА ДОСЬЕ, ЗНАНИЯ УЧАСТНИКОВ И ПОИСКА ПО ПАМЯТИ =================

def get_or_create_user_topic(main_chat_id, user_name, user_id):
    if not main_chat_id:
        return None
    memory = load_memory()
    memory["users"].setdefault(user_name, {})
    topic_id = memory["users"][user_name].get("topic_id")
    if not topic_id:
        new_topic_id = create_forum_topic(main_chat_id, f"{user_name} ({user_id})")
        if new_topic_id:
            memory["users"][user_name]["topic_id"] = new_topic_id
            save_memory(memory)
            send_message(main_chat_id, f"Создано досье для пользователя @{user_name}.", message_thread_id=new_topic_id)
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
    send_message(main_chat_id, log_msg, message_thread_id=topic_id, parse_mode="HTML")

def update_dossier(user_name, user_id=None):
    """Строит правдивое досье СТРОГО по реальным сообщениям и профилю участника (без выдумок!)."""
    memory = load_memory()
    user_history = [h for h in memory["history"] if h.get("user") == user_name]
    if not user_history:
        return

    lines = []
    for h in user_history[-30:]:
        where = "в ЛС" if h.get("chat_id", 0) > 0 else "в чате"
        line = f"[{where}] {h['user']}: {h['text']}"
        if h.get("bot_reply"):
            line += f"\nТолик: {h['bot_reply']}"
        lines.append(line)

    u_info = memory.get("users", {}).get(user_name, {})
    fn = f"{u_info.get('first_name', '')} {u_info.get('last_name', '')}".strip()
    bio = u_info.get("bio", "")
    meta_str = f"Ник: @{user_name} | Имя в ТГ: {fn or 'не указано'} | О себе (bio): {bio or 'нет'}"

    prompt = (
        f"Данные профиля: {meta_str}\n"
        f"РЕАЛЬНЫЕ СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ:\n" + "\n".join(lines) + "\n\n"
        f"Составь ТОЧНОЕ ДОСЬЕ на @{user_name} СТРОГО по этим сообщениям (НИЧЕГО не выдумывай от себя!):\n"
        f"- Как его/её зовут, какой пол, манера общения, реальные факты и интересы, которые упоминались.\n"
        f"- Отношения с Толиком и участниками чата.\n"
        f"Пиши кратко и только подтвержденные факты."
    )
    try:
        response = generate_fast_summary(prompt, "Ты точный аналитик. Запрещено выдумывать факты, пиши только то, что есть в тексте.")
        if response and response != "IGNORE":
            profile = response.strip()
            memory = load_memory()
            memory.setdefault("users", {}).setdefault(user_name, {})["profile"] = profile
            save_memory(memory)

            config = load_config()
            main_chat_id = config.get("main_chat_id")
            if main_chat_id and user_id:
                topic_id = get_or_create_user_topic(main_chat_id, user_name, user_id)
                if topic_id:
                    send_message(main_chat_id, f"🔄 <b>Обновлено досье на {user_name}:</b>\n\n{profile}", message_thread_id=topic_id, parse_mode="HTML")
    except Exception as e:
        print(f"❌ ОШИБКА ДОСЬЕ: {e}")

def sync_all_users_on_start():
    """При запуске бота синхронизирует админов чата и создает реальные досье по старой истории."""
    try:
        memory = load_memory()
        chat_ids = {h.get("chat_id") for h in memory.get("history", []) if h.get("chat_id", 0) < 0}
        for cid in chat_ids:
            sync_chat_admins(cid, memory)
        save_memory(memory)

        # Если у кого-то в истории есть сообщения, но еще нет досье — создаем!
        users_in_hist = {h.get("user") for h in memory.get("history", []) if h.get("user") and h.get("user") != "SYSTEM"}
        for u_name in users_in_hist:
            u_prof = memory.get("users", {}).get(u_name, {}).get("profile", "")
            if not u_prof or u_prof in ["Новый знакомый", "Пока мало данных", "Участник чата (пока мало общались)", "Участник чата"]:
                print(f"📋 [АВТО-ДОСЬЕ ПРИ СТАРТЕ]: Составляю реальное досье по истории для @{u_name}...")
                update_dossier(u_name, memory.get("users", {}).get(u_name, {}).get("user_id"))
                time.sleep(1.5)
    except Exception as e:
        logging.error(f"Startup Sync Error: {e}")

def build_genius_social_and_cross_memory(chat_id, current_user, text, memory):
    all_history = memory.get("history", [])
    all_users = memory.get("users", {})

    # 1. Кросс-чат память (что обсуждали с текущим собеседником в ЛС или других чатах)
    other_chat_msgs = [h for h in all_history if h.get("user") == current_user and h.get("chat_id") not in [0, chat_id]]
    cross_lines = []
    for h in other_chat_msgs[-10:]:
        place = "в ЛС (лично)" if h.get("chat_id", 0) > 0 else "в другом чате"
        entry = f"[{place} {h.get('time_short', '')}] {current_user}: {h.get('text', '')}"
        if h.get("bot_reply"):
            entry += f" -> Ты ответил: {h.get('bot_reply')}"
        cross_lines.append(entry)
    cross_memory_str = "\n".join(cross_lines) if cross_lines else "Вне этого чата пока не общались."

    # 2. Полный список участников чата + глубокий поиск по упоминаниям
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

        # Проверяем, упомянут ли этот участник (по нику, имени, фамилии или титулу)
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

    # 3. Поиск по всей истории переписок (вдруг это имя/кличку обсуждали в сообщениях раньше!)
    words = [w for w in re.findall(r"[а-яёa-z0-9_]{3,}", text_lower) if w not in [
        "толик", "толя", "толян", "придумай", "стих", "про", "кто", "это", "такой", "такая",
        "расскажи", "знаешь", "помнишь", "что", "как", "привет", "дела", "тебя", "мне", "опиши"
    ]]
    if words:
        found_in_history = []
        for h in all_history[-200:]:
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
    image_parts=None, explicit_ping=False, post_dialog_check=False, seeing_own_drawing=False,
):
    now = get_minsk_time()
    config = load_config()
    current_plan = config.get("current_plan", "Планов пока нет.")
    stickers = config.get("stickers", {})
    sticker_list = ", ".join(stickers.keys()) if stickers else "нет"
    has_selfies = len(config.get("selfies", [])) > 0
    has_input_media = bool(image_parts)
    has_video = any(m and m.startswith("video/") for (_, m) in (image_parts or []))

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
            "1) Если он продолжает диалог с тобой, отвечает тебе или комментирует твой рисунок — ответь как Толик.\n"
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
        f"ПРАВИЛО ЧЕСТНОСТИ И ЗНАНИЯ УЧАСТНИКОВ (БЕЗ ВЫДУМОК!):\n"
        f"1. Ниже дан список ВСЕХ участников чата (их @username, Имя в ТГ, графа 'О себе', досье и реальные фразы) и твоя память из ЛС.\n"
        f"2. СТРОГО ЗАПРЕЩЕНО ВЫДУМЫВАТЬ ФАКТЫ О ЛЮДЯХ ОТ БАЛДЫ (не приписывай людям программирование, хобби или черты, которых нет в их досье или сообщениях!).\n"
        f"3. Если просят придумать стих или рассказать про участника — используй ТОЛЬКО реальные факты о нем из досье/сообщений ниже. Если про конкретного человека по этому имени/нику пока совсем мало фактов в списке — обыграй его красивое имя/ник или честно и мило скажи, что он(а) в чате недавно/мало пишет, поэтому знаешь его(её) пока слабо.\n"
        f"4. ЗАПРЕЩЕНО использовать тег [SEARCH: ...] для поиска участников чата, друзей и их ников!\n"
        f"5. В общем чате веди себя тактично: ты помнишь переписки из ЛС, но НЕ сливай чужие личные секреты и странные вещи на публику.\n\n"
        f"ПРАВИЛА СТИЛЯ:\n"
        f"1. ПИШИ СТРОГО СТРОЧНЫМИ БУКВАМИ (маленькими). Точки в конце коротких реплик не ставь.\n"
        f"2. НЕ ЗДОРОВАЙСЯ ('привет', 'ку'), если диалог уже идет!\n"
        f"3. Отвечай живо, короткими сообщениями от 1-го лица, разделяя мысли тегом [MSG].\n"
        f"4. ЗАПРЕЩЕНО говорить слова: 'бот', 'ии', 'нейросеть', 'сгенерирую', 'промпт', 'досье'. Ты живой парень.\n"
        f"5. НИКАКИХ ЭМОДЗИ В ТЕКСТЕ. На стикеры реагируй по картинке стикера, не называя их смайликами.\n\n"
        f"ДОСТУПНЫЕ СПЕЦ-ТЕГИ:\n"
        f"- [SEARCH: запрос] — ТОЛЬКО для новостей мира, лекарств, игр и научных фактов (НЕ для имен участников чата!).\n"
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
        [f"[{h.get('time_short')}] {h['user']}: {h['text']}" + (f"\nТолик: {h['bot_reply']}" if h.get("bot_reply") else "") for h in chat_history[-20:]]
    )

    reply_context = f"[Информация о сообщении: {reply_target_info}]"
    if seeing_own_drawing:
        reply_context += " [К сообщению прикреплена ТВОЯ СОБСТВЕННАЯ КАРТИНКА (рисунок/фото), которую ты сам только что сделал — ты видишь её глазами!]"
    elif has_video:
        reply_context += " [К сообщению прикреплен ВИДЕОФАЙЛ — посмотри видеоряд и звук напрямую!]"
    elif has_input_media:
        reply_context += " [К сообщению прикреплена КАРТИНКА / СТИКЕР — смотри на саму картинку!]"

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
        # Убираем сам тег из исходного ответа на случай, если поиск ничего не даст
        reply_without_tag = re.sub(r"\[SEARCH:\s*.+?\]", "", reply).strip()
        search_data = search_web(search_query)
        # Перегенерируем ответ ТОЛЬКО если поиск реально нашел полезные факты!
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
    while True:
        try:
            time.sleep(60)
            now = get_minsk_time()
            config = load_config()
            main_chat = config.get("main_chat_id")
            if not main_chat:
                continue

            today_str = now.strftime("%d.%m.%Y")
            if now.hour >= 8 and config.get("last_diary_date") != today_str:
                prompt = f"Составь краткий план на день на сегодня ({today_str}). Ты Толик (17 лет, Минск, фурри). Напиши со строчной буквы."
                res = generate_fast_summary(prompt, "Пиши план для личного дневника.", retries=2)
                if res and "лагает" not in res:
                    config["current_plan"] = res
                    config["last_diary_date"] = today_str
                    save_config(config)
                    send_message(main_chat, f"📅 <b>Планы на {today_str}</b>\n\n{res}", message_thread_id=config.get("diary_topic_id"), parse_mode="HTML")

            memory = load_memory()
            if len(memory["history"]) > 160:
                old_hist = memory["history"][:70]
                memory["history"] = memory["history"][70:]
                hist_text = "\n".join([f"{h['user']}: {h['text']}" for h in old_hist])
                sum_res = generate_fast_summary(f"Выдели реальные факты о людях из этого диалога (2-4 предложения):\n{hist_text}", "Ты архиватор фактов.", retries=2)
                if sum_res and "IGNORE" not in sum_res and "STOP_DIALOG" not in sum_res:
                    memory["history"].insert(0, {"time_short": "ПАМЯТЬ", "chat_id": 0, "user": "SYSTEM", "text": sum_res, "bot_reply": None})
                save_memory(memory)
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
    save_config(config)
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
    chat_id, user_name, thread_id = data["chat_id"], data["user_name"], data["thread_id"]
    user_id, is_group = data["user_id"], data["is_group"]
    combined_text = "\n".join(data["texts"])
    image_parts = list(data.get("images", []))
    seeing_own_drawing = data.get("seeing_own_drawing", False)
    memory = load_memory()
    now = get_minsk_time()
    conv_key = f"{chat_id}_{user_id}"

    if len(data["texts"]) > 1:
        print(f"🔗 [СКЛЕЙКА]: Склеено {len(data['texts'])} сообщений от @{user_name}!")

    memory["users"].setdefault(user_name, {"profile": "Пока мало данных", "msg_count": 0})
    memory["users"][user_name]["msg_count"] = memory["users"][user_name].get("msg_count", 0) + 1
    msg_cnt = memory["users"][user_name]["msg_count"]

    if not data["should_reply"]:
        active_conversations.pop(conv_key, None)
        memory["history"].append({"time_short": now.strftime("%H:%M"), "chat_id": chat_id, "user": user_name, "text": combined_text, "bot_reply": None})
        save_memory(memory)
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
        save_memory(memory)
        log_dialogue_to_topic(user_name, user_id, combined_text, None, is_group)
        if msg_cnt == 2 or msg_cnt % 6 == 0:
            threading.Thread(target=update_dossier, args=(user_name, user_id), daemon=True).start()
        return

    if check_reply_rate_spam(user_id):
        memory.setdefault("ignored_users", {})[str(user_id)] = time.time() + 120
        save_memory(memory)
        send_message(chat_id, "подожди пару минут, ты слишком быстро строчишь", message_thread_id=thread_id)
        return

    if not image_parts and chat_id in last_bot_image and time.time() - last_bot_image[chat_id]["time"] < 180:
        image_parts.append(last_bot_image[chat_id]["media"])
        seeing_own_drawing = True
        print("🎨👁 [ЗРЕНИЕ ТОЛИКА]: Толик смотрит на свою недавно созданную картинку!")

    if not image_parts and chat_id in last_chat_media and time.time() - last_chat_media[chat_id]["time"] < 180:
        kws = ["видео", "ролик", "видос", "фот", "картинк", "рисун", "скетч", "скрин", "мем", "глянь", "опиши", "посмотри", "что тут", "че там", "чё там"]
        if any(kw in combined_text.lower() for kw in kws):
            image_parts.append(last_chat_media[chat_id]["media"])

    u_info = memory.get("users", {}).get(user_name, {})
    fn_ln = f"{u_info.get('first_name', '')} {u_info.get('last_name', '')}".strip()
    user_profile = f"Имя в ТГ: {fn_ln or user_name} | О себе: {u_info.get('bio', 'нет')}\n{u_info.get('profile', 'Пока мало данных')}"
    chat_history = [h for h in memory["history"] if h["chat_id"] == chat_id]

    post_dialog_check = is_group and not explicit_ping and in_active_dialog
    reply = fetch_ai_response(
        combined_text, chat_history, user_name, user_profile, data["reply_target_info"],
        memory=memory, chat_id=chat_id, image_parts=image_parts,
        explicit_ping=explicit_ping, post_dialog_check=post_dialog_check, seeing_own_drawing=seeing_own_drawing,
    )

    if reply == "STOP_DIALOG":
        active_conversations.pop(conv_key, None)
        print(f"🛑 [STOP_DIALOG]: Диалог с @{user_name} завершен -> включен стандартный режим!")
        memory["history"].append({"time_short": now.strftime("%H:%M"), "chat_id": chat_id, "user": user_name, "text": combined_text, "bot_reply": None})
        save_memory(memory)
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
                last_chat_media[chat_id] = {"media": (img_bytes, "image/jpeg"), "time": time.time()}
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
    save_memory(memory)
    log_dialogue_to_topic(user_name, user_id, combined_text, full_bot_reply, is_group)

    if msg_cnt == 2 or msg_cnt % 5 == 0:
        threading.Thread(target=update_dossier, args=(user_name, user_id), daemon=True).start()

# ================= ГЛАВНЫЙ ЦИКЛ БОТА =================

def run_bot():
    last_update_id = 0
    print("=" * 70)
    print("🚀 Толик запущен (Реальные имена/БИО + Честные досье + Склейка + Анти-Флуд)!")
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
                seeing_own_drawing = False

                mem_snap = load_memory()
                if chat_id < 0 and chat_id not in synced_chats:
                    synced_chats.add(chat_id)
                    sync_chat_admins(chat_id, mem_snap)
                register_user_metadata(mem_snap, user, chat_id)
                if "reply_to_message" in msg and "from" in msg["reply_to_message"]:
                    register_user_metadata(mem_snap, msg["reply_to_message"]["from"], chat_id)
                save_memory(mem_snap)

                flood_status = check_flood_on_incoming(user_id)
                if flood_status == "MUTED":
                    continue
                elif flood_status == "TRIGGER_MUTE":
                    send_message(chat_id, "эй, полегче, не флуди так жестко [MSG] помолчу пару минут", message_thread_id=thread_id)
                    continue

                if user_name.lower() == ADMIN_USERNAME.lower():
                    if text.strip().lower() == "/mainchat":
                        cfg = load_config()
                        cfg["main_chat_id"] = chat_id
                        save_config(cfg)
                        send_message(chat_id, "✅ Основной чат для досье установлен!", message_thread_id=thread_id)
                        continue
                    if text.strip().lower() == "/setdiary":
                        cfg = load_config()
                        cfg["main_chat_id"] = chat_id
                        cfg["diary_topic_id"] = thread_id
                        save_config(cfg)
                        send_message(chat_id, "✅ Тема установлена для дневника!", message_thread_id=thread_id)
                        continue
                    if text.startswith("/set_looks") and "reply_to_message" in msg and "photo" in msg["reply_to_message"]:
                        photo_id = msg["reply_to_message"]["photo"][-1]["file_id"]
                        cfg = load_config()
                        cfg["selfies"].append(photo_id)
                        save_config(cfg)
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

                if "voice" in msg or "video_note" in msg:
                    is_vnote = "video_note" in msg
                    voice_obj = msg.get("voice") or msg.get("video_note")
                    f_bytes, real_mime = get_file_bytes(voice_obj["file_id"], "video/mp4" if is_vnote else "audio/ogg")
                    if f_bytes:
                        rec = transcribe_media_gemini(f_bytes, real_mime, is_video_note=is_vnote)
                        text += f" [{'Видеокружочек' if is_vnote else 'Голосовое'}: \"{rec or 'неразборчиво'}\"]"

                if "photo" in msg:
                    img_bytes, mime = get_file_bytes(msg["photo"][-1]["file_id"], "image/jpeg")
                    if img_bytes:
                        incoming_images.append((img_bytes, mime))
                        last_chat_media[chat_id] = {"media": (img_bytes, mime), "time": time.time()}
                    text += " [Пользователь прикрепил фото]"
                elif "reply_to_message" in msg and "photo" in msg["reply_to_message"]:
                    rep_id = msg["reply_to_message"].get("from", {}).get("id")
                    img_bytes, mime = get_file_bytes(msg["reply_to_message"]["photo"][-1]["file_id"], "image/jpeg")
                    if img_bytes:
                        incoming_images.append((img_bytes, mime))
                        if rep_id == BOT_ID:
                            seeing_own_drawing = True
                    if not text:
                        text = "[Ответ на ТВОЙ рисунок]" if rep_id == BOT_ID else "[Ответ на фото]"

                if "video" in msg or "animation" in msg:
                    vid_obj = msg.get("video") or msg.get("animation")
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                        last_chat_media[chat_id] = {"media": (v_bytes, v_mime), "time": time.time()}
                    text += " [Пользователь прикрепил видео]"
                elif "reply_to_message" in msg and ("video" in msg["reply_to_message"] or "animation" in msg["reply_to_message"]):
                    vid_obj = msg["reply_to_message"].get("video") or msg["reply_to_message"].get("animation")
                    v_bytes, v_mime, _ = download_video_or_thumb(vid_obj)
                    if v_bytes:
                        incoming_images.append((v_bytes, v_mime))
                    if not text:
                        text = "[Пользователь ответил на видео]"
                elif "sticker" in msg:
                    st_bytes, st_mime = download_sticker_visual(msg["sticker"])
                    if st_bytes:
                        incoming_images.append((st_bytes, st_mime))
                    text += " [Пользователь прислал стикер — посмотри глазами на картинку стикера]"

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
                            "texts": [text] if text else [], "images": incoming_images,
                            "last_time": time.time(), "should_reply": should_reply, "reply_target_info": reply_target_info,
                        }
                    else:
                        if text and len(message_buffer[buffer_key]["texts"]) < MAX_MESSAGES_IN_BUFFER:
                            if not message_buffer[buffer_key]["texts"] or message_buffer[buffer_key]["texts"][-1] != text:
                                message_buffer[buffer_key]["texts"].append(text)
                                print(f"➕ [СКЛЕЙКА ({len(message_buffer[buffer_key]['texts'])} шт.)]: Жду ещё {BUFFER_WAIT_SECONDS} сек...")
                        if incoming_images and len(message_buffer[buffer_key]["images"]) < 4:
                            message_buffer[buffer_key]["images"].extend(incoming_images)
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

from http.server import BaseHTTPRequestHandler, HTTPServer


class RenderWebHandler(BaseHTTPRequestHandler):
    """Имитирует живой сайт для проверок Render и пингеров (GET и HEAD запросы)."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        uptime_min = int((time.time() - START_TIME) // 60)
        html = (
            f"<html><body style='font-family:sans-serif;'>"
            f"<h2>🦊 Толик жив и работает!</h2>"
            f"<p>Аптайм: {uptime_min} мин. | Статус: OK (200)</p>"
            f"</body></html>"
        )
        self.wfile.write(html.encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        # Отключаем спам в консоль от каждого пинга сайта
        pass


def start_fake_web_server():
    # Render сам передает нужный порт в переменную окружения PORT (по умолчанию 10000)
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderWebHandler)
    print(f"🌐 [RENDER WEB SERVICE]: Имитация веб-сервиса запущена на порту {port}!")
    server.serve_forever()


def keep_render_awake():
    """Авто-самопинг каждые 10 минут, если Render выдал внешнюю ссылку RENDER_EXTERNAL_URL."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        return
    print(f"🔄 [KEEP-ALIVE]: Включен авто-пинг адреса {render_url}")
    while True:
        time.sleep(600)
        try:
            requests.get(render_url, timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    # 1. Запускаем фейковый веб-сервер в фоновом потоке для Render
    threading.Thread(target=start_fake_web_server, daemon=True).start()
    # 2. Запускаем самопинг от засыпания
    threading.Thread(target=keep_render_awake, daemon=True).start()
    # 3. Запускаем самого Толика
    run_bot()
