import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
import requests
from bs4 import BeautifulSoup
from rubka.asynco import Robot
from rubka.context import Message
from rubka.button import InlineBuilder

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("RubikaBot")

# ---------- Config ----------
TOKEN = os.getenv("RUBIKA_BOT_TOKEN", "YOUR_BOT_TOKEN")
GH_TOKEN = os.getenv("GH_PAT", "")
REPO = os.getenv("GITHUB_REPOSITORY", "owner/repo")
WORKFLOW_ID = "bot.yml"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_FILE = Path("state.json")

# Time settings
JOB_LIMIT_HOURS = 6
RESTART_BEFORE = 20  # minutes
RUN_DURATION = (JOB_LIMIT_HOURS * 60) - RESTART_BEFORE  # 340 minutes

# ---------- State ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

pending_actions = load_state()  # e.g., {"chat_id": {"action": "extract_sources"}}

# ---------- Helper Functions ----------
async def download_file(url: str, save_path: Path) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    save_path.write_bytes(await resp.read())
                    return True
                logger.warning(f"Download failed {resp.status} for {url}")
                return False
    except Exception as e:
        logger.error(f"Download error {url}: {e}")
        return False

def fetch_html(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"fetch_html failed: {e}")
        return ""

def parse_assets(html: str, base_url: str) -> dict:
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
    file_exts = r'\.(pdf|zip|rar|docx?|xlsx?|pptx?|mp3|mp4|mkv|avi|mov|apk|exe|dmg|iso|tar|gz|7z)$'
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if re.search(file_exts, href, re.IGNORECASE):
            assets["files"].append(href)
    return assets

def combine_to_single_html(html: str, assets: dict, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for css_url in assets["css"]:
        try:
            css_content = requests.get(css_url, timeout=10).text
            style_tag = soup.new_tag("style")
            style_tag.string = css_content
            for link in soup.find_all("link", href=css_url):
                link.replace_with(style_tag)
        except Exception:
            pass
    for js_url in assets["js"]:
        try:
            js_content = requests.get(js_url, timeout=10).text
            script_tag = soup.new_tag("script")
            script_tag.string = js_content
            for script in soup.find_all("script", src=js_url):
                script.replace_with(script_tag)
        except Exception:
            pass
    return str(soup)

# ---------- Auto‑restart logic ----------
async def trigger_next_workflow():
    if not GH_TOKEN:
        logger.warning("GH_PAT not set – cannot auto-restart")
        return
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_ID}/dispatches"
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {"ref": "main"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 204:
                    logger.info("Next workflow dispatched successfully")
                else:
                    logger.error(f"Dispatch failed: {resp.status} {await resp.text()}")
    except Exception as e:
        logger.error(f"Dispatch error: {e}")

# ---------- Bot Handlers ----------
bot = Robot(token=TOKEN)

@bot.on_message(commands=["start"])
async def start(bot: Robot, message: Message):
    builder = InlineBuilder()
    builder.row(
        InlineBuilder().button_simple(id="download_url", text="🌐 دانلود فایل از URL"),
        InlineBuilder().button_simple(id="download_webpage", text="📄 ترکیب صفحه وب"),
    )
    builder.row(
        InlineBuilder().button_simple(id="extract_sources", text="📦 استخراج منابع"),
        InlineBuilder().button_simple(id="help", text="❓ راهنما"),
    )
    await message.reply(
        "سلام! 👋 به ربات هوشمند دانلودر خوش آمدید.\nلطفاً یک گزینه را انتخاب کنید:",
        inline_keypad=builder.build(),
    )

@bot.on_callback("download_url")
async def cb_download_url(bot: Robot, message: Message):
    await message.reply("🔗 لطفاً لینک مستقیم فایل را ارسال کنید:")

@bot.on_callback("download_webpage")
async def cb_download_webpage(bot: Robot, message: Message):
    await message.reply("🌍 لطفاً آدرس صفحه وب مورد نظر را ارسال کنید:")

@bot.on_callback("extract_sources")
async def cb_extract_sources(bot: Robot, message: Message):
    pending_actions[str(message.chat_id)] = {"action": "extract_sources"}
    save_state(pending_actions)
    await message.reply("🕵️ لطفاً آدرس صفحه‌ای که می‌خواهید منابع آن استخراج شود را بفرستید:")

@bot.on_callback("help")
async def cb_help(bot: Robot, message: Message):
    await message.reply(
        "🔰 **راهنما**\n\n"
        "• **دانلود فایل**: لینک مستقیم (pdf, zip, apk, ...)\n"
        "• **ترکیب صفحه وب**: CSS و JS را در HTML ادغام می‌کند.\n"
        "• **استخراج منابع**: تصاویر، ویدیوها و فایل‌های قابل دانلود را نشان می‌دهد.\n"
        "• برای شروع دوباره /start"
    )

@bot.on_message()
async def handle_text(bot: Robot, message: Message):
    chat_id = str(message.chat_id)
    text = message.text.strip()
    if not text.startswith(("http://", "https://")):
        await message.reply("⚠️ لطفاً یک لینک معتبر بفرستید.")
        return

    if chat_id in pending_actions:
        action = pending_actions[chat_id]["action"]
        if action == "extract_sources":
            await extract_sources(bot, message, text)
            del pending_actions[chat_id]
            save_state(pending_actions)
            return

    parsed = urlparse(text)
    path = parsed.path.lower()
    if path.endswith(('.pdf', '.zip', '.rar', '.jpg', '.png', '.mp4', '.exe', '.apk', '.mp3')):
        await handle_direct_download(bot, message, text)
    else:
        builder = InlineBuilder()
        builder.row(
            InlineBuilder().button_simple(id=f"combine|{text}", text="📄 ترکیب صفحه وب"),
            InlineBuilder().button_simple(id=f"extract|{text}", text="📦 استخراج منابع"),
        )
        await message.reply("چه کاری می‌خواهید انجام دهید؟", inline_keypad=builder.build())

@bot.on_callback(pattern=r"^combine\|(.+)$")
async def cb_combine_url(bot: Robot, message: Message):
    url = message.matches[0]
    await handle_webpage_combination(bot, message, url)

@bot.on_callback(pattern=r"^extract\|(.+)$")
async def cb_extract_url(bot: Robot, message: Message):
    url = message.matches[0]
    await extract_sources(bot, message, url)

async def handle_direct_download(bot: Robot, message: Message, url: str):
    await message.reply("⏳ در حال دریافت فایل...")
    filename = Path(urlparse(url).path).name or "file"
    save_path = DOWNLOAD_DIR / filename
    if await download_file(url, save_path):
        await bot.send_document(message.chat_id, str(save_path), caption=f"✅ {filename}")
    else:
        await message.reply("❌ خطا در دانلود فایل.")

async def handle_webpage_combination(bot: Robot, message: Message, url: str):
    await message.reply("🌐 در حال تحلیل و ترکیب صفحه...")
    html = fetch_html(url)
    if not html:
        await message.reply("❌ دریافت صفحه ناموفق بود.")
        return
    assets = parse_assets(html, url)
    combined = combine_to_single_html(html, assets, url)
    domain = urlparse(url).netloc.replace(".", "_")
    filepath = DOWNLOAD_DIR / f"{domain}_combined.html"
    filepath.write_text(combined, encoding="utf-8")
    await bot.send_document(message.chat_id, str(filepath), caption="📄 صفحه وب ترکیبی")

async def extract_sources(bot: Robot, message: Message, url: str):
    await message.reply("🔎 در حال استخراج منابع...")
    html = fetch_html(url)
    if not html:
        await message.reply("❌ دریافت صفحه شکست خورد.")
        return
    assets = parse_assets(html, url)

    if assets["images"]:
        photos = assets["images"][:10]
        media = [{"type": "photo", "media": img_url} for img_url in photos]
        await bot.send_media_group(message.chat_id, media=media)
        if len(assets["images"]) > 10:
            await message.reply(f"📸 تنها 10 تصویر از {len(assets['images'])} تصویر نمایش داده شد.")
    else:
        await message.reply("🖼️ هیچ تصویری یافت نشد.")

    selections = assets["videos"] + assets["files"]
    if selections:
        builder = InlineBuilder()
        for i, src in enumerate(selections[:8]):
            name = Path(urlparse(src).path).name or f"منبع {i+1}"
            builder.row(
                InlineBuilder().button_simple(
                    id=f"download_asset|{src}",
                    text=f"⬇️ {name[:30]}"
                )
            )
        if len(selections) > 8:
            await message.reply(f"📦 {len(selections)} منبع یافت شد. نمایش ۸ مورد اول.")
        await message.reply(
            "🎬 برای دانلود هر ویدیو یا فایل روی دکمه مربوطه کلیک کنید:",
            inline_keypad=builder.build()
        )
    else:
        await message.reply("📭 هیچ ویدیو یا فایل قابل دانلودی یافت نشد.")

@bot.on_callback(pattern=r"^download_asset\|(.+)$")
async def cb_download_asset(bot: Robot, message: Message):
    asset_url = message.matches[0]
    await message.reply("⏳ دریافت فایل...")
    try:
        name = Path(urlparse(asset_url).path).name or "asset"
        save_path = DOWNLOAD_DIR / name
        if await download_file(asset_url, save_path):
            await bot.send_document(message.chat_id, str(save_path), caption=f"✅ {name}")
        else:
            await message.reply("❌ دانلود ناموفق.")
    except Exception as e:
        logger.error(f"Asset download error: {e}")
        await message.reply("❌ خطایی رخ داد.")

# ---------- Main Runner ----------
async def main_loop():
    deadline = datetime.utcnow() + timedelta(minutes=RUN_DURATION)
    logger.info(f"Bot will run until {deadline} UTC, then restart itself.")
    bot_task = asyncio.create_task(bot.run())
    await asyncio.sleep(RUN_DURATION * 60)
    await trigger_next_workflow()
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        logger.info("Bot cancelled, exiting.")

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
    finally:
        save_state(pending_actions)
