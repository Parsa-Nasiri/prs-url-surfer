import json, logging, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("RubikaBot")

TOKEN = os.getenv("RUBIKA_BOT_TOKEN", "YOUR_BOT_TOKEN")
GH_TOKEN = os.getenv("GH_PAT", "")
REPO = os.getenv("GITHUB_REPOSITORY", "owner/repo")
BASE = "https://botapi.rubika.ir/v3"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_FILE = Path("state.json")
CONFIG_FILE = Path("config.json")

JOB_LIMIT_HOURS = 6
RESTART_BEFORE = 20
RUN_DURATION = (JOB_LIMIT_HOURS * 60) - RESTART_BEFORE
POLL_INTERVAL = 5

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def save_json(obj, path):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_json(STATE_FILE, {"offset_id": None})

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

def get_updates():
    data = {"limit": 10}
    if state.get("offset_id"):
        data["offset_id"] = state["offset_id"]
    return api_call("getUpdates", data)

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
    rows = []
    for row in buttons:
        rows.append({"buttons": [{"id": bid, "type": "Simple", "button_text": bt} for bid, bt in row]})
    return {"rows": rows}

pending_actions = load_json(CONFIG_FILE, {})

def handle_message(update):
    msg = update["new_message"]
    chat_id = msg["chat_id"]
    text = msg.get("text", "").strip()
    if chat_id in pending_actions:
        action = pending_actions.pop(chat_id)
        save_json(pending_actions, CONFIG_FILE)
        if action["action"] == "extract":
            return handle_extract(chat_id, text)
        elif action["action"] == "combine":
            return handle_combine(chat_id, text)
        elif action["action"] == "download":
            return handle_direct_download(chat_id, text)
    if text.startswith("/start"):
        start(chat_id)
    elif text.startswith(("http://", "https://")):
        prompt_user(chat_id, text)
    else:
        send_message(chat_id, "⚠️ لطفاً یک لینک معتبر بفرستید یا /start")

def handle_callback(update):
    cb = update["callback_data"]
    data = cb["data"]
    chat_id = cb["chat_id"]
    if data.startswith("combine|"):
        return handle_combine(chat_id, data[len("combine|"):])
    elif data.startswith("extract|"):
        return handle_extract(chat_id, data[len("extract|"):])
    elif data.startswith("download_asset|"):
        return download_asset(chat_id, data[len("download_asset|"):])
    elif data == "download_url":
        pending_actions[chat_id] = {"action": "download"}
        save_json(pending_actions, CONFIG_FILE)
        return send_message(chat_id, "🔗 لطفاً لینک مستقیم فایل را ارسال کنید:")
    elif data == "download_webpage":
        pending_actions[chat_id] = {"action": "combine"}
        save_json(pending_actions, CONFIG_FILE)
        return send_message(chat_id, "🌍 لطفاً آدرس صفحه وب را ارسال کنید:")
    elif data == "extract_sources":
        pending_actions[chat_id] = {"action": "extract"}
        save_json(pending_actions, CONFIG_FILE)
        return send_message(chat_id, "📦 لطفاً آدرس صفحه را بفرستید:")
    elif data == "help":
        return send_message(chat_id, "🔰 راهنما:\n• /start\n• ارسال لینک مستقیم\n• ارسال لینک صفحه")

def start(chat_id):
    keypad = build_inline_keypad([
        [("download_url", "🌐 دانلود فایل"), ("download_webpage", "📄 ترکیب صفحه")],
        [("extract_sources", "📦 استخراج منابع"), ("help", "❓ راهنما")],
    ])
    send_message(chat_id, "سلام! 👋 به ربات هوشمند دانلودر خوش آمدید.", keypad)

def prompt_user(chat_id, url):
    keypad = build_inline_keypad([
        [("combine|" + url, "📄 ترکیب صفحه"), ("extract|" + url, "📦 استخراج")],
    ])
    send_message(chat_id, "چه کاری انجام دهم؟", keypad)

def handle_direct_download(chat_id, url):
    send_message(chat_id, "⏳ در حال دریافت...")
    name = Path(urlparse(url).path).name or "file"
    path = DOWNLOAD_DIR / name
    if download_to_path(url, path):
        send_message(chat_id, f"✅ فایل ذخیره شد:\n`{name}`")
    else:
        send_message(chat_id, "❌ خطا در دانلود.")

def handle_combine(chat_id, url, msg_id=None):
    send_message(chat_id, "🌐 در حال ترکیب صفحه...")
    html = fetch_html(url)
    if not html:
        return send_message(chat_id, "❌ صفحه دریافت نشد.")
    assets = parse_assets(html, url)
    combined = combine_html(html, assets)
    domain = urlparse(url).netloc.replace(".", "_")
    filepath = DOWNLOAD_DIR / f"{domain}_combined.html"
    filepath.write_text(combined, encoding="utf-8")
    send_message(chat_id, f"📄 صفحه ترکیبی:\n`{filepath}`")

def handle_extract(chat_id, url, msg_id=None):
    send_message(chat_id, "🔎 استخراج منابع...")
    html = fetch_html(url)
    if not html:
        return send_message(chat_id, "❌ صفحه دریافت نشد.")
    assets = parse_assets(html, url)
    if assets["images"]:
        send_message(chat_id, "🖼️ " + "\n".join(assets["images"][:10]))
    else:
        send_message(chat_id, "🖼️ هیچ تصویری پیدا نشد.")
    selections = assets["videos"] + assets["files"]
    if selections:
        buttons = []
        for src in selections[:8]:
            name = Path(urlparse(src).path).name or "فایل"
            buttons.append([(f"download_asset|{src}", f"⬇️ {name[:25]}")])
        keypad = build_inline_keypad(buttons)
        send_message(chat_id, "🎬 فایل‌های قابل دانلود:", keypad)
    else:
        send_message(chat_id, "📭 فایل قابل دانلودی یافت نشد.")

def download_asset(chat_id, url):
    send_message(chat_id, "⏳ دریافت...")
    name = Path(urlparse(url).path).name or "asset"
    path = DOWNLOAD_DIR / name
    if download_to_path(url, path):
        send_message(chat_id, f"✅ {name}")
    else:
        send_message(chat_id, "❌ دانلود ناموفق.")

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

def main():
    deadline = datetime.utcnow() + timedelta(minutes=RUN_DURATION)
    logger.info(f"Bot runs until {deadline} UTC")
    test = api_call("getMe")
    if test.get("status") != "OK":
        logger.error(f"❌ Token invalid: {test}")
        return
    logger.info(f"✅ Bot @{test.get('username','?')} started")
    last_heartbeat = 0
    while datetime.utcnow() < deadline:
        try:
            result = get_updates()
            updates = result.get("updates", [])
            if updates:
                logger.info(f"📩 {len(updates)} update(s)")
            for upd in updates:
                if "new_message" in upd:
                    handle_message(upd)
                elif "callback_data" in upd:
                    handle_callback(upd)
                if "id" in upd:
                    state["offset_id"] = str(int(upd["id"]) + 1)
            if "next_offset_id" in result:
                state["offset_id"] = result["next_offset_id"]
            save_json(state, STATE_FILE)
            if time.time() - last_heartbeat > 60:
                logger.info("💓 alive")
                last_heartbeat = time.time()
        except Exception as e:
            logger.error(f"Loop error: {e}")
        time.sleep(POLL_INTERVAL)
    trigger_restart()

if __name__ == "__main__":
    main()
