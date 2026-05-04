import json, logging, os, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote_plus
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("RubikaBot")

TOKEN = os.getenv("RUBIKA_BOT_TOKEN", "")
GH_TOKEN = os.getenv("GH_PAT", "")
REPO = os.getenv("GITHUB_REPOSITORY", "owner/repo")

# Base URL from https://rubika.ir/botapi
BASE = "https://rubika.ir/botapi/v3"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_FILE = Path("state.json")
CONFIG_FILE = Path("config.json")

JOB_LIMIT_HOURS = 6
RESTART_BEFORE = 20
RUN_DURATION = (JOB_LIMIT_HOURS * 60) - RESTART_BEFORE
POLL_INTERVAL = 3

# ---------- Persistent state ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"offset_id": None}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

def load_pending():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}

def save_pending(data):
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_state()
pending = load_pending()

# ---------- Rubika API helper ----------
def api(method, payload=None, files=None):
    safe_token = quote_plus(TOKEN)
    url = f"{BASE}/{safe_token}/{method}"
    try:
        if files:
            resp = requests.post(url, files=files, timeout=30)
        else:
            resp = requests.post(url, json=payload or {}, timeout=30)

        if resp.status_code != 200:
            logger.error(f"HTTP {resp.status_code}: {resp.text[:300]}")
            return None

        data = resp.json()
        if data.get("status") != "OK":
            logger.error(f"API error: {data}")
            return None
        return data.get("result", {})
    except Exception as e:
        logger.error(f"Request failed {method}: {e}")
        return None

def send_message(chat_id, text, inline_keypad=None):
    payload = {"chat_id": str(chat_id), "text": text}
    if inline_keypad:
        payload["inline_keypad"] = inline_keypad
    return api("sendMessage", payload)

def send_document(chat_id, file_path):
    with open(file_path, "rb") as f:
        return api("sendDocument", files={"document": f})

def send_media_group(chat_id, media_list):
    return api("sendMediaGroup", {"chat_id": str(chat_id), "media": media_list})

def get_updates():
    payload = {"limit": 10}
    if state.get("offset_id"):
        payload["offset_id"] = state["offset_id"]
    return api("getUpdates", payload)

# ---------- Web helpers ----------
def download_file(url, dest):
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            dest.write_bytes(r.content)
            return True
    except Exception as e:
        logger.error(f"Download {url}: {e}")
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
        except:
            pass
    for js in assets["js"]:
        try:
            content = requests.get(js, timeout=10).text
            tag = soup.new_tag("script")
            tag.string = content
            for script in soup.find_all("script", src=js):
                script.replace_with(tag)
        except:
            pass
    return str(soup)

def build_keypad(button_rows):
    rows = []
    for row in button_rows:
        rows.append({"buttons": [{"id": bid, "type": "Simple", "button_text": txt} for bid, txt in row]})
    return {"rows": rows}

# ---------- Handlers ----------
def handle_message(update):
    msg = update.get("new_message", {})
    chat_id = msg.get("chat_id")
    text = msg.get("text", "").strip()
    if not chat_id or not text:
        return

    if chat_id in pending:
        act = pending.pop(chat_id)
        save_pending(pending)
        if act["action"] == "extract":
            return extract_sources(chat_id, text)
        elif act["action"] == "combine":
            return combine_page(chat_id, text)
        elif act["action"] == "download":
            return direct_download(chat_id, text)

    if text == "/start":
        show_main_menu(chat_id)
    elif text.startswith(("http://", "https://")):
        ask_action(chat_id, text)
    else:
        send_message(chat_id, "⚠️ لطفاً یک لینک معتبر ارسال کنید یا /start")

def handle_callback(update):
    cb = update.get("callback_data", {})
    chat_id = cb.get("chat_id")
    data = cb.get("data", "")
    if not data or not chat_id:
        return

    if data.startswith("combine|"):
        combine_page(chat_id, data[8:])
    elif data.startswith("extract|"):
        extract_sources(chat_id, data[8:])
    elif data.startswith("download|"):
        download_asset(chat_id, data[9:])
    elif data == "menu_download_url":
        pending[chat_id] = {"action": "download"}
        save_pending(pending)
        send_message(chat_id, "🔗 لطفاً لینک مستقیم فایل را ارسال کنید:")
    elif data == "menu_combine_url":
        pending[chat_id] = {"action": "combine"}
        save_pending(pending)
        send_message(chat_id, "🌍 لطفاً آدرس صفحه وب را ارسال کنید:")
    elif data == "menu_extract_url":
        pending[chat_id] = {"action": "extract"}
        save_pending(pending)
        send_message(chat_id, "📦 لطفاً آدرس صفحه را برای استخراج منابع ارسال کنید:")
    elif data == "help":
        send_message(chat_id, "🔰 راهنما:\n/start - منوی اصلی\nارسال لینک مستقیم - دانلود فایل\nارسال لینک صفحه - انتخاب عملیات")

# ---------- Features ----------
def show_main_menu(chat_id):
    kb = build_keypad([
        [("menu_download_url", "🌐 دانلود فایل"), ("menu_combine_url", "📄 ترکیب صفحه")],
        [("menu_extract_url", "📦 استخراج منابع"), ("help", "❓ راهنما")],
    ])
    send_message(chat_id, "سلام! 👋 به ربات هوشمند دانلودر خوش آمدید.\nلطفاً گزینه مورد نظر را انتخاب کنید:", kb)

def ask_action(chat_id, url):
    kb = build_keypad([
        [("combine|" + url, "📄 ترکیب صفحه"), ("extract|" + url, "📦 استخراج منابع")],
    ])
    send_message(chat_id, "چه کاری می‌خواهید روی این لینک انجام دهید؟", kb)

def direct_download(chat_id, url):
    send_message(chat_id, "⏳ در حال دریافت فایل...")
    name = Path(urlparse(url).path).name or "file"
    path = DOWNLOAD_DIR / name
    if download_file(url, path):
        send_message(chat_id, "✅ فایل دریافت شد. در حال ارسال...")
        send_document(chat_id, path)
    else:
        send_message(chat_id, "❌ خطا در دانلود فایل.")

def combine_page(chat_id, url, msg_id=None):
    send_message(chat_id, "🌐 در حال تحلیل و ترکیب صفحه...")
    html = fetch_html(url)
    if not html:
        return send_message(chat_id, "❌ دریافت صفحه ناموفق بود.")
    assets = parse_assets(html, url)
    combined = combine_html(html, assets)
    domain = urlparse(url).netloc.replace(".", "_")
    filepath = DOWNLOAD_DIR / f"{domain}_combined.html"
    filepath.write_text(combined, encoding="utf-8")
    send_document(chat_id, filepath)

def extract_sources(chat_id, url, msg_id=None):
    send_message(chat_id, "🔎 در حال استخراج منابع...")
    html = fetch_html(url)
    if not html:
        return send_message(chat_id, "❌ دریافت صفحه شکست خورد.")
    assets = parse_assets(html, url)

    if assets["images"]:
        images = assets["images"][:10]
        media = [{"type": "photo", "media": img} for img in images]
        send_media_group(chat_id, media)
        if len(assets["images"]) > 10:
            send_message(chat_id, f"📸 تنها 10 تصویر از {len(assets['images'])} نمایش داده شد.")
    else:
        send_message(chat_id, "🖼️ هیچ تصویری یافت نشد.")

    items = assets["videos"] + assets["files"]
    if items:
        buttons = []
        for src in items[:8]:
            name = Path(urlparse(src).path).name or "فایل"
            buttons.append([(f"download|{src}", f"⬇️ {name[:25]}")])
        kb = build_keypad(buttons)
        send_message(chat_id, "🎬 ویدیوها / فایل‌های قابل دانلود:", kb)
    else:
        send_message(chat_id, "📭 فایل یا ویدیوئی یافت نشد.")

def download_asset(chat_id, url):
    send_message(chat_id, "⏳ دریافت فایل...")
    name = Path(urlparse(url).path).name or "asset"
    path = DOWNLOAD_DIR / name
    if download_file(url, path):
        send_message(chat_id, "✅ در حال ارسال فایل...")
        send_document(chat_id, path)
    else:
        send_message(chat_id, "❌ دانلود ناموفق.")

# ---------- Self‑restart ----------
def trigger_restart():
    if not GH_TOKEN:
        return
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/bot.yml/dispatches"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        resp = requests.post(url, json={"ref": "main"}, headers=headers)
        if resp.status_code == 204:
            logger.info("🔄 Next workflow dispatched")
        else:
            logger.error(f"Dispatch failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Dispatch error: {e}")

# ---------- Main ----------
def main():
    deadline = datetime.utcnow() + timedelta(minutes=RUN_DURATION)
    logger.info(f"⏳ Bot runs until {deadline} UTC, then restarts.")

    bot_info = api("getMe")
    if not bot_info:
        logger.critical("❌ Cannot connect to Rubika API – check token. Exiting.")
        return
    logger.info(f"✅ Bot @{bot_info.get('username','?')} is alive and polling...")

    last_heartbeat = 0
    while datetime.utcnow() < deadline:
        try:
            result = get_updates()
            if result is None:
                time.sleep(POLL_INTERVAL)
                continue

            updates = result.get("updates", [])
            for upd in updates:
                if "new_message" in upd:
                    handle_message(upd)
                elif "callback_data" in upd:
                    handle_callback(upd)
                if "id" in upd:
                    state["offset_id"] = str(int(upd["id"]) + 1)
            if "next_offset_id" in result:
                state["offset_id"] = result["next_offset_id"]

            if updates:
                logger.info(f"📩 {len(updates)} update(s)")

            if time.time() - last_heartbeat > 60:
                logger.info("💓 alive")
                last_heartbeat = time.time()
        except Exception as e:
            logger.error(f"Loop error: {e}")

        save_state(state)
        time.sleep(POLL_INTERVAL)

    trigger_restart()
    logger.info("🛑 Finished.")

if __name__ == "__main__":
    main()
