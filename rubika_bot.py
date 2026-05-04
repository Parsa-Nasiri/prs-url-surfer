import asyncio
import aiohttp
import json
import os
import re
import sys
import time
import traceback
import logging
import hashlib
import base64
from datetime import datetime
from urllib.parse import urljoin, urlparse, unquote
from pathlib import Path
from typing import List, Dict, Optional, Union
from bs4 import BeautifulSoup

# Required third-party libraries (install via requirements.txt)
try:
    import aiofiles
    from aiohttp import ClientTimeout
except ImportError:
    raise ImportError("Missing dependencies. Install: aiohttp, aiofiles, beautifulsoup4, requests")

# ================= CONFIGURATION =================
TOKEN = os.getenv("RUBIKA_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("RUBIKA_BOT_TOKEN environment variable is not set.")

BASE_URL = "https://botapi.rubika.ir/v3"
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Cron timing (GitHub Actions friendly)
POLL_INTERVAL = 150               # seconds between API calls
MAX_RUNTIME = 6 * 3600 - 20 * 60  # 5h 40m (stop 20 min before GitHub's 6h limit)

# Optional: admin chat ID for startup / shutdown notifications
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # set this if you want notifications

# Inline keypad for the main menu
MAIN_MENU_KEYPAD = {
    "rows": [
        [{"id": "get_sources", "type": "Simple", "button_text": "📥 Get Page Sources"}],
        [{"id": "download_page", "type": "Simple", "button_text": "🌐 Download Full Webpage"}],
        [{"id": "help", "type": "Simple", "button_text": "❓ Help"}],
    ]
}

# Emojis for a better look
ICONS = {
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
    "file": "📄",
    "image": "🖼️",
    "video": "🎥",
    "audio": "🎵",
    "download": "⬇️",
    "loading": "⏳",
    "done": "🎉",
    "link": "🔗",
    "folder": "📁",
    "globe": "🌐",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("RubikaBot")

# ================= UTILITY FUNCTIONS =================
def generate_message_id() -> str:
    return hashlib.md5(str(time.time()).encode()).hexdigest()[:12]

def format_file_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {units[i]}"

def clean_filename(url_path: str) -> str:
    name = url_path.split("/")[-1] or "index.html"
    name = unquote(name)
    if "?" in name:
        name = name.split("?")[0]
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "download"

def fix_relative_url(base_url: str, relative_url: str) -> str:
    return urljoin(base_url, relative_url)

# ================= BOT CLASS =================
class RubikaBot:
    def __init__(self, token: str):
        self.token = token
        self.base_url = BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None
        self.offset_id: Optional[str] = None
        self.start_time = time.time()

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=ClientTimeout(total=60),
            headers={"Content-Type": "application/json"},
        )
        try:
            await self.main_loop()
        except Exception as e:
            logger.error(f"Fatal error in main loop: {e}")
        finally:
            await self.session.close()

    def is_time_exceeded(self) -> bool:
        return (time.time() - self.start_time) >= MAX_RUNTIME

    # ---------- Rubika API Calls ----------
    async def call_api(self, method: str, data: dict) -> dict:
        url = f"{self.base_url}/{self.token}/{method}"
        try:
            async with self.session.post(url, json=data) as resp:
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"API call failed ({method}): {e}")
            return {"status": "ERROR", "message": str(e)}

    async def get_updates(self, limit: int = 10) -> List[dict]:
        data = {"limit": limit}
        if self.offset_id:
            data["offset_id"] = self.offset_id
        response = await self.call_api("getUpdates", data)
        updates = response.get("updates", [])
        if response.get("next_offset_id"):
            self.offset_id = response["next_offset_id"]
        return updates

    async def send_message(
        self,
        chat_id: str,
        text: str,
        inline_keypad: Optional[dict] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[str]:
        data = {"chat_id": chat_id, "text": text}
        if inline_keypad:
            data["inline_keypad"] = inline_keypad
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        response = await self.call_api("sendMessage", data)
        if response.get("status") == "OK":
            return response.get("message_id")
        else:
            logger.error(f"sendMessage failed: {response}")
            return None

    # ---------- Web Scraping & Download ----------
    async def fetch_html(self, url: str) -> Optional[str]:
        try:
            async with self.session.get(url, timeout=ClientTimeout(30)) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    logger.warning(f"Fetch {url} returned HTTP {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None

    async def extract_resources(self, url: str, html: str) -> Dict[str, List[str]]:
        soup = BeautifulSoup(html, "html.parser")
        resources = {"images": [], "videos": [], "files": []}

        # Images
        for tag in soup.find_all(["img", "source"]):
            src = tag.get("src") or tag.get("data-src") or tag.get("srcset")
            if src and not src.startswith("data:"):
                full_url = fix_relative_url(url, src)
                resources["images"].append(full_url)

        # Videos
        for tag in soup.find_all(["video", "iframe"]):
            src = tag.get("src") or tag.get("data-src")
            if src:
                full_url = fix_relative_url(url, src)
                resources["videos"].append(full_url)

        # Other files (a href with typical extensions)
        file_exts = (
            ".pdf", ".zip", ".rar", ".doc", ".docx", ".xls", ".xlsx",
            ".ppt", ".pptx", ".txt", ".csv", ".mp3", ".mp4", ".mov", ".avi",
            ".mkv", ".wav", ".flac", ".apk", ".exe", ".dmg"
        )
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if any(href.lower().endswith(ext) for ext in file_exts):
                full_url = fix_relative_url(url, href)
                resources["files"].append(full_url)

        # Deduplicate
        for key in resources:
            resources[key] = list(set(resources[key]))

        return resources

    async def download_resource(self, url: str, save_dir: Path) -> Optional[Path]:
        try:
            file_name = clean_filename(url)
            file_path = save_dir / file_name
            async with self.session.get(url, timeout=ClientTimeout(120)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(content)
                    return file_path
                else:
                    logger.warning(f"Download {url} failed (HTTP {resp.status})")
                    return None
        except Exception as e:
            logger.warning(f"Error downloading {url}: {e}")
            return None

    async def download_webpage_embedded(self, url: str) -> Optional[str]:
        html = await self.fetch_html(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        base_tag = soup.new_tag("base", href=url)
        if soup.head:
            soup.head.insert(0, base_tag)
        else:
            head = soup.new_tag("head")
            head.insert(0, base_tag)
            soup.html.insert(0, head) if soup.html else soup.insert(0, head)

        # Embed stylesheets
        for link in soup.find_all("link", rel="stylesheet"):
            href = link.get("href")
            if href:
                css_url = fix_relative_url(url, href)
                css_content = await self.fetch_html(css_url)
                if css_content:
                    style_tag = soup.new_tag("style")
                    style_tag.string = css_content
                    link.replace_with(style_tag)

        # Embed scripts
        for script in soup.find_all("script", src=True):
            src = script["src"]
            js_url = fix_relative_url(url, src)
            js_content = await self.fetch_html(js_url)
            if js_content:
                new_script = soup.new_tag("script")
                new_script.string = js_content
                script.replace_with(new_script)

        # Convert images to base64 (optional, can be disabled if too large)
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if src.startswith("data:"):
                continue
            img_url = fix_relative_url(url, src)
            try:
                async with self.session.get(img_url, timeout=ClientTimeout(30)) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        if "image" in content_type:
                            img_data = await resp.read()
                            b64 = base64.b64encode(img_data).decode()
                            img["src"] = f"data:{content_type};base64,{b64}"
            except Exception:
                pass

        domain = urlparse(url).netloc.replace(".", "_")
        filename = f"{domain}_fullpage_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        filepath = DOWNLOAD_DIR / filename
        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(str(soup))
        return str(filepath)

    # ---------- User Interaction ----------
    async def handle_start(self, chat_id: str):
        text = (
            f"{ICONS['globe']} *Welcome to the Smart Downloader Bot!*\n\n"
            "I can help you:\n"
            f"{ICONS['link']} Extract and download resources from any webpage (images, videos, files)\n"
            f"{ICONS['download']} Download a complete webpage with all assets embedded in a single HTML file\n\n"
            "Send me a URL to begin, or use the menu below."
        )
        await self.send_message(chat_id, text, inline_keypad=MAIN_MENU_KEYPAD)

    async def handle_url_input(self, chat_id: str, url: str):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        await self.send_message(chat_id, f"{ICONS['loading']} Analyzing `{url}`…")

        html = await self.fetch_html(url)
        if not html:
            await self.send_message(chat_id, f"{ICONS['error']} Could not fetch the page.")
            return

        resources = await self.extract_resources(url, html)
        images = resources["images"]
        videos = resources["videos"]
        files = resources["files"]

        if not any([images, videos, files]):
            await self.send_message(
                chat_id,
                f"{ICONS['warning']} No downloadable resources found.\n"
                "You can still download the full page using the menu.",
            )
            return

        # Show images (thumbnails are not supported natively, so just links)
        if images:
            await self.send_message(chat_id, f"{ICONS['image']} Found *{len(images)} images*:")
            for img in images[:5]:  # show first 5
                await self.send_message(chat_id, f"{ICONS['image']} {img}")
            if len(images) > 5:
                await self.send_message(chat_id, f"... and {len(images)-5} more.")
            keypad = {
                "rows": [
                    [{"id": f"dl_images_{url}", "type": "Simple",
                      "button_text": f"⬇️ Download All Images ({len(images)})"}]
                ]
            }
            await self.send_message(chat_id, "Download these images?", inline_keypad=keypad)

        # Videos and files – require user confirmation
        if videos or files:
            buttons = []
            if videos:
                buttons.append([{"id": f"dl_videos_{url}", "type": "Simple",
                                 "button_text": f"🎥 Download All Videos ({len(videos)})"}])
            if files:
                buttons.append([{"id": f"dl_files_{url}", "type": "Simple",
                                 "button_text": f"📁 Download All Files ({len(files)})"}])
            if buttons:
                await self.send_message(
                    chat_id,
                    "Download these media files?",
                    inline_keypad={"rows": buttons}
                )

    async def handle_button_click(self, chat_id: str, button_id: str):
        if button_id == "get_sources":
            await self.send_message(chat_id, f"{ICONS['link']} Send me the URL.")
        elif button_id == "download_page":
            await self.send_message(chat_id, f"{ICONS['globe']} Send me the webpage URL.")
        elif button_id == "help":
            help_text = (
                f"{ICONS['info']} *How to use this bot:*\n\n"
                "1. Send a URL to extract resources.\n"
                "2. Use the buttons to download images, videos, or files.\n"
                "3. Use 'Download Full Webpage' to get a self-contained HTML.\n\n"
                "Large files (videos, archives) require confirmation before downloading."
            )
            await self.send_message(chat_id, help_text)
        elif button_id.startswith("dl_images_"):
            await self.download_category(chat_id, button_id.replace("dl_images_", ""), "images")
        elif button_id.startswith("dl_videos_"):
            await self.download_category(chat_id, button_id.replace("dl_videos_", ""), "videos")
        elif button_id.startswith("dl_files_"):
            await self.download_category(chat_id, button_id.replace("dl_files_", ""), "files")
        else:
            await self.send_message(chat_id, f"{ICONS['warning']} Unknown action.")

    async def download_category(self, chat_id: str, url: str, category: str):
        html = await self.fetch_html(url)
        if not html:
            await self.send_message(chat_id, f"{ICONS['error']} Could not refetch the page.")
            return

        resources = await self.extract_resources(url, html)
        items = resources.get(category, [])
        if not items:
            await self.send_message(chat_id, f"{ICONS['warning']} No {category} found.")
            return

        await self.send_message(chat_id, f"{ICONS['loading']} Starting download of {len(items)} {category}...")
        download_dir = DOWNLOAD_DIR / f"{chat_id}_{category}_{int(time.time())}"
        download_dir.mkdir(parents=True, exist_ok=True)

        success = 0
        for item_url in items:
            file_path = await self.download_resource(item_url, download_dir)
            if file_path:
                success += 1
                # For simplicity, we just notify the user; sending actual files may require rubpy.
                await self.send_message(chat_id, f"{ICONS['file']} Downloaded: {file_path.name}")
        await self.send_message(chat_id, f"{ICONS['done']} Download complete! {success}/{len(items)} files saved.")

    # ---------- Main Loop ----------
    async def main_loop(self):
        logger.info(f"Bot started. Will run for approximately {MAX_RUNTIME // 60} minutes.")

        # Optional: notify admin (only if ADMIN_CHAT_ID is set and valid)
        if ADMIN_CHAT_ID:
            try:
                await self.send_message(ADMIN_CHAT_ID, f"Bot online at {datetime.now().isoformat()}")
            except Exception as e:
                logger.warning(f"Could not send admin notification: {e}")

        while not self.is_time_exceeded():
            try:
                updates = await self.get_updates(limit=10)
                for update in updates:
                    await self.process_update(update)
                await asyncio.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error(f"Error in main loop: {traceback.format_exc()}")
                await asyncio.sleep(5)

        logger.info("Time limit reached. Shutting down gracefully.")
        if ADMIN_CHAT_ID:
            await self.send_message(ADMIN_CHAT_ID, "Bot shutting down (time limit).")

    async def process_update(self, update: dict):
        chat_id = update.get("chat_id")
        if not chat_id:
            return

        update_type = update.get("type")

        if update_type == "NewMessage":
            new_msg = update.get("new_message")
            if not new_msg:
                return
            text = new_msg.get("text", "").strip()
            sender_type = new_msg.get("sender_type", "User")
            if sender_type != "User":
                return

            if text == "/start":
                await self.handle_start(chat_id)
            elif re.match(r"https?://\S+", text):
                await self.handle_url_input(chat_id, text)
            else:
                await self.send_message(
                    chat_id,
                    f"{ICONS['info']} Please send a valid URL or use the menu.",
                    inline_keypad=MAIN_MENU_KEYPAD,
                )

        elif update_type == "ButtonClicked":
            aux_data = update.get("aux_data", {})
            button_id = aux_data.get("button_id")
            if button_id:
                await self.handle_button_click(chat_id, button_id)

# ================= ENTRY POINT =================
async def main():
    bot = RubikaBot(TOKEN)
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
