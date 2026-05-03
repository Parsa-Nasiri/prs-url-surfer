import json, logging, os, re, sys, time, asyncio
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("RubikaBot")

# ---------- Config ----------
TOKEN = os.getenv("RUBIKA_BOT_TOKEN", "YOUR_BOT_TOKEN")
GH_TOKEN = os.getenv("GH_PAT", "")
REPO = os.getenv("GITHUB_REPOSITORY", "owner/repo")
BASE = "https://botapi.rubika.ir/v3"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_FILE = Path("state.json")
CONFIG_FILE = Path("config.json")

# Time constants
JOB_LIMIT_HOURS = 6
RESTART_BEFORE = 20              # minutes
RUN_DURATION = (JOB_LIMIT_HOURS * 60) - RESTART_BEFORE   # 340 min
POLL_INTERVAL = 5                # seconds

# ---------- State ----------
def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(obj, path):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_json(STATE_FILE, {"offset_id": None})

# ---------- API helpers ----------
def api_call(method, data=None, files=None):
    url = f"{BASE}/{TOKEN}/{method}"
    try:
        if files:
            resp = requests.post(url, files=files)
        else:
            resp = requests.post(url, json=data or {}, timeout=30)
        return resp.json() if resp.text else {}
    except Exception as e:
        logger.error(f"API call {method} failed: {e}")
        return {}

def send_message(chat_id, text, inline_keypad=None):
    payload = {"chat_id": str(chat_id), "text": text}
    if inline_keypad:
        payload["inline_keypad"] = inline_keypad
    return api_call("sendMessage", payload)

def edit_message_text(chat_id, msg_id, text, inline_keypad=None):
    payload = {"chat_id": str(chat_id), "message_id": str(msg_id), "text": text}
    if inline_keypad:
        payload["inline_keypad"] = inline_keypad
    return api_call("editMessageText", payload)

def get_updates():
    data = {"limit": 10}
    if state.get("offset_id"):
        data["offset_id"] = state["offset_id"]
    return api_call("getUpdates", data)

# ---------- Helpers ----------
def download_to_path(url, path):
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            path.write_bytes(r.content)
            return True
    except Exception as e:
        logger.error(f"Download {url} error: {e}")
    return False

def fetch_html(url):
    try:
        return requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text
    except Exception as e:
        logger.error(f"fetch_html {url}: {e}")
        return ""

def parse_assets(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    assets = {"css": [], "js": [], "images": [], "videos": [], "files": []}
    for link in soup.find_all("link", rel="stylesheet"):
        if link.get("href"):
            assets["css"].append(urljoin(base_url, link["href"]))
    for script in soup.find_all("script", src=True):
        assets["js"].append(urljoin(base_url, script["src"]))
    for img in soup.find_all("img", src=True):
        assets["images"].append(urljoin(base_url, img["src"]))
    for video in soup.find_all("video"):
        src = video.get("src") or (video.find("source") and video.find("source").get("src"))
        if src:
            assets["videos"].append(urljoin(base_url, src))
    file_ext = r'\.(pdf|zip|rar|docx?|xlsx?|pptx?|mp3|mp4|mkv|avi|mov|apk|exe|dmg|iso|tar|gz|7z)$'
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if re.search(file_ext, href, re.IGNORECASE):
            assets["files"].append(href)
    return assets

def combine_html(html, assets):
    soup = BeautifulSoup(html, "html.parser")
    for css in assets["css"]:
        try:
            content = requests.get(css, timeout=10).text
            tag = soup.new_tag("style")
            tag.string = content
            for link in soup.find_all("link", href=css):
                link.replace_with(tag)
        except Exception:
            pass
    for js in assets["js"]:
        try:
            content = requests.get(js, timeout=10).text
            tag = soup.new_tag("script")
            tag.string = content
            for script in soup.find_all("script", src=js):
                script.replace_with(tag)
        except Exception:
            pass
    return str(soup)

def build_inline_keypad(buttons):
    """buttons: list of list of (id, text)"""
    rows = []
    for row in buttons:
        rows.append({"buttons": [{"id": bid, "type": "Simple", "button_text": bt} for bid, bt in row]})
    return {"rows": rows}

# ---------- Core logic ----------
pending_actions = load_json(CONFIG_FILE, {})   # chat_id -> {"action": "...", "url": "..."}

def handle_message(update):
    msg = update.get("new_message", {})
    chat_id = msg.get("chat_id")
    text = msg.get("text", "").strip()
    msg_id = msg.get("message_id")
    if not chat_id or not text:
        return

    # Pending action?
    if chat_id in pending_actions:
        action = pending_actions.pop(chat_id)
        save_json(pending_actions, CONFIG_FILE)
        if action["action"] == "extract":
            return handle_extract(chat_id, text)
        elif action["action"] == "combine":
            return handle_combine(chat_id, text)
        elif action["action"] == "download":
            return handle_direct_download(chat_id, text)

    # Commands
    if text.startswith("/start"):
        return start(chat_id)
    elif text.startswith(("http://", "https://")):
        return prompt_user(chat_id, text)
    else:
        send_message(chat_id, "⚠️ لطفاً یک لینک معتبر (با http یا https) بفرستید یا /start را بزنید.")

def handle_callback(update):
    cb = update.get("callback_data", {})
    data = cb.get("data", "")
    chat_id = cb.get("chat_id")
    msg_id = cb.get("message_id")
    if not data or not chat_id:
        return

    if data.startswith("combine|"):
        url = data[len("combine|"):]
        return handle_combine(chat_id, url, msg_id)
    elif data.startswith("extract|"):
        url = data[len("extract|"):]
        return handle_extract(chat_id, url, msg_id)
    elif data.startswith("download_asset|"):
        url = data[len("download_asset|"):]
        return download_asset(chat_id, url)
    elif data == "download_url":
        pending_actions[chat_id] = {"action": "download"}
        save_json(pending_actions, CONFIG_FILE)
        return send_message(chat_id, "🔗 لطفاً لینک مستقیم فایل را ارسال کنید:")
    elif data == "download_webpage":
        pending_actions[chat_id] = {"action": "combine"}
        save_json(pending_actions, CONFIG_FILE)
        return send_message(chat_id, "🌍 لطفاً آدرس صفحه وب مورد نظر را ارسال کنید:")
    elif data == "extract_sources":
        pending_actions[chat_id] = {"action": "extract"}
        save_json(pending_actions, CONFIG_FILE)
        return send_message(chat_id, "📦 لطفاً آدرس صفحه‌ای که می‌خواهید منابع آن استخراج شود را بفرستید:")
    elif data == "help":
        return send_message(chat_id, "🔰 راهنما:\n• /start : شروع\n• ارسال لینک مستقیم : دانلود فایل\n• ارسال لینک صفحه : انتخاب ترکیب یا استخراج")

# ---------- Actions ----------
def start(chat_id):
    keypad = build_inline_keypad([
        [("download_url", "🌐 دانلود فایل از URL"), ("download_webpage", "📄 ترکیب صفحه وب")],
        [("extract_sources", "📦 استخراج منابع"), ("help", "❓ راهنما")],
    ])
    send_message(chat_id, "سلام! 👋 به ربات هوشمند دانلودر خوش آمدید.\nلطفاً یک گزینه را انتخاب کنید:", keypad)

def prompt_user(chat_id, url):
    keypad = build_inline_keypad([
        [("combine|" + url, "📄 ترکیب صفحه وب"), ("extract|" + url, "📦 استخراج منابع")],
    ])
    send_message(chat_id, "چه کاری می‌خواهید انجام دهید؟", keypad)

def handle_direct_download(chat_id, url):
    send_message(chat_id, "⏳ در حال دریافت فایل...")
    name = Path(urlparse(url).path).name or "file"
    path = DOWNLOAD_DIR / name
    if download_to_path(url, path):
        send_message(chat_id, f"✅ فایل با موفقیت دانلود شد:\n`{name}`\n(مسیر: {path})")
    else:
        send_message(chat_id, "❌ خطا در دانلود فایل.")

def handle_combine(chat_id, url, msg_id=None):
    send_message(chat_id, "🌐 در حال تحلیل و ترکیب صفحه...")
    html = fetch_html(url)
    if not html:
        return send_message(chat_id, "❌ دریافت صفحه ناموفق بود.")
    assets = parse_assets(html, url)
    combined = combine_html(html, assets)
    domain = urlparse(url).netloc.replace(".", "_")
    filepath = DOWNLOAD_DIR / f"{domain}_combined.html"
    filepath.write_text(combined, encoding="utf-8")
    send_message(chat_id, f"📄 صفحه وب ترکیبی ذخیره شد:\n`{filepath}`")

def handle_extract(chat_id, url, msg_id=None):
    send_message(chat_id, "🔎 در حال استخراج منابع...")
    html = fetch_html(url)
    if not html:
        return send_message(chat_id, "❌ دریافت صفحه شکست خورد.")
    assets = parse_assets(html, url)

    # Images: send as text list (Rubika v3 API has no media group via sendMessage)
    if assets["images"]:
        img_list = "\n".join(assets["images"][:10])
        send_message(chat_id, f"🖼️ تصاویر:\n{img_list}")
    else:
        send_message(chat_id, "🖼️ هیچ تصویری یافت نشد.")

    # Videos & files
    selections = assets["videos"] + assets["files"]
    if selections:
        buttons = []
        for src in selections[:8]:
            name = Path(urlparse(src).path).name or "فایل"
            buttons.append([(f"download_asset|{src}", f"⬇️ {name[:25]}")])
        keypad = build_inline_keypad(buttons)
        send_message(chat_id, "🎬 برای دانلود هر ویدیو یا فایل روی دکمه کلیک کنید:", keypad)
    else:
        send_message(chat_id, "📭 هیچ ویدیو یا فایل قابل دانلودی یافت نشد.")

def download_asset(chat_id, url):
    send_message(chat_id, "⏳ دریافت فایل...")
    name = Path(urlparse(url).path).name or "asset"
    path = DOWNLOAD_DIR / name
    if download_to_path(url, path):
        send_message(chat_id, f"✅ فایل دانلود شد:\n`{name}`")
    else:
        send_message(chat_id, "❌ دانلود ناموفق.")

# ---------- Auto-restart ----------
def trigger_restart():
    if not GH_TOKEN:
        return
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/bot.yml/dispatches"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        requests.post(url, json={"ref": "main"}, headers=headers)
        logger.info("Next workflow dispatched")
    except Exception as e:
        logger.error(f"Dispatch error: {e}")

# ---------- Main loop ----------
def main():
    deadline = datetime.utcnow() + timedelta(minutes=RUN_DURATION)
    logger.info(f"Bot runs until {deadline} UTC")
    while datetime.utcnow() < deadline:
        try:
            result = get_updates()
            updates = result.get("updates", [])
            for upd in updates:
                if "new_message" in upd:
                    handle_message(upd)
                elif "callback_data" in upd:
                    handle_callback(upd)
                # Track offset
                if "id" in upd:
                    state["offset_id"] = str(int(upd["id"]) + 1)
            if "next_offset_id" in result:
                state["offset_id"] = result["next_offset_id"]
            save_json(state, STATE_FILE)
        except Exception as e:
            logger.error(f"Loop error: {e}")
        time.sleep(POLL_INTERVAL)
    trigger_restart()

if __name__ == "__main__":
    main()
