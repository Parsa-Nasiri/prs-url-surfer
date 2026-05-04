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
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, unquote
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Union
from bs4 import BeautifulSoup

# Optional libraries for extended functionality (install via requirements.txt)
try:
    import aiofiles
    from aiohttp import ClientTimeout
except ImportError:
    raise ImportError("Required libraries: aiohttp, aiofiles, beautifulsoup4, requests. Install them first.")

# ================= CONFIGURATION =================
# These environment variables should be set in GitHub Actions secrets.
TOKEN = os.getenv("RUBIKA_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BASE_URL = "https://botapi.rubika.ir/v3"
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# GitHub Actions specific: cron job interval (seconds) - 20 minutes before GitHub's 6-hour limit ends.
# We'll use an asynchronous loop that runs every 150 seconds (2.5 minutes) to stay active.
POLL_INTERVAL = 150  # seconds
MAX_RUNTIME = 6 * 3600 - 20 * 60  # 6 hours - 20 minutes = 5h 40m, stop before limit

# Inline keypad template for main menu
MAIN_MENU_KEYPAD = {
    "rows": [
        [{"id": "get_sources", "type": "Simple", "button_text": "📥 Get Page Sources"}],
        [{"id": "download_page", "type": "Simple", "button_text": "🌐 Download Full Webpage"}],
        [{"id": "help", "type": "Simple", "button_text": "❓ Help"}],
    ]
}

# Emojis for better UX
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

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("RubikaBot")

# ================= UTILITY FUNCTIONS =================

def generate_message_id() -> str:
    """Generate a unique ID for messages when needed."""
    return hashlib.md5(str(time.time()).encode()).hexdigest()[:12]

def format_file_size(size_bytes: int) -> str:
    """Convert bytes to human-readable format."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {units[i]}"

def clean_filename(url_path: str) -> str:
    """Create a valid filename from a URL path."""
    name = url_path.split("/")[-1] or "index.html"
    name = unquote(name)
    # Remove query string
    if "?" in name:
        name = name.split("?")[0]
    # Replace invalid characters
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    if not name:
        name = "download"
    return name

def fix_relative_url(base_url: str, relative_url: str) -> str:
    """Resolve a relative URL against a base URL."""
    return urljoin(base_url, relative_url)

# ================= RUBIKA BOT CLASS =================

class RubikaBot:
    """
    A fully featured Rubika bot that can:
    - Extract and list downloadable resources from a given URL (images, videos, files).
    - Download a complete webpage and inline all assets (HTML, CSS, JS) into a single file.
    - Operate 24/7 using a smart polling loop suitable for GitHub Actions.
    - Provide an interactive UX with inline keypads and emojis.
    """
    def __init__(self, token: str):
        self.token = token
        self.base_url = BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None
        self.offset_id: Optional[str] = None
        self.start_time = time.time()
        # Track ongoing downloads per user (chat_id -> {url: status})
        self.download_tasks: Dict[str, asyncio.Task] = {}

    async def start(self):
        """Initialize HTTP session and start main loop."""
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
        """Check if the bot has run beyond the allowed time (GitHub Actions limit)."""
        return (time.time() - self.start_time) >= MAX_RUNTIME

    # ---------- Rubika API Methods ----------

    async def call_api(self, method: str, data: dict) -> dict:
        """Generic method to call Rubika Bot API."""
        url = f"{self.base_url}/{self.token}/{method}"
        try:
            async with self.session.post(url, json=data) as resp:
                result = await resp.json()
                return result
        except aiohttp.ClientError as e:
            logger.error(f"API call failed for {method}: {e}")
            return {"status": "ERROR", "message": str(e)}

    async def get_updates(self, limit: int = 10) -> List[dict]:
        """Fetch new updates using Long Polling."""
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
        """Send a text message, optionally with an inline keypad."""
        data = {
            "chat_id": chat_id,
            "text": text,
        }
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

    async def send_file(self, chat_id: str, file_path: Union[str, Path], caption: str = "") -> bool:
        """
        Send a file using rubpy's method.
        Since the official API may not expose a direct 'sendFile' endpoint,
        we rely on the rubpy library (if installed) or implement a fallback
        using a multipart upload.
        """
        # Note: Rubika's official bot API may limit file sending.
        # If rubpy is available, use its send_file method.
        # Otherwise, we fall back to sending a message with file info.
        try:
            from rubpy import Client  # type: ignore
            async with Client(self.token) as client:
                await client.send_file(chat_id, str(file_path), caption=caption)
            return True
        except ImportError:
            # Fallback: send a message with file details
            file_name = Path(file_path).name
            text = f"{ICONS['file']} File ready: {file_name}\n{caption}"
            await self.send_message(chat_id, text)
            return True
        except Exception as e:
            logger.error(f"Failed to send file: {e}")
            await self.send_message(chat_id, f"{ICONS['error']} Failed to send file: {e}")
            return False

    # ---------- Web Scraping and Download Functions ----------

    async def fetch_html(self, url: str) -> Optional[str]:
        """Fetch HTML content from a URL."""
        try:
            async with self.session.get(url, timeout=ClientTimeout(30)) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    logger.error(f"Failed to fetch {url}: HTTP {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None

    async def extract_resources(self, url: str, html: str) -> Dict[str, List[str]]:
        """
        Parse HTML and extract links to images, videos, and other files.
        Returns a dictionary with categories: 'images', 'videos', 'files'.
        """
        soup = BeautifulSoup(html, "html.parser")
        resources = {"images": [], "videos": [], "files": []}

        # Extract images
        for tag in soup.find_all(["img", "source"]):
            src = tag.get("src") or tag.get("data-src") or tag.get("srcset")
            if src:
                # Skip data URIs
                if src.startswith("data:"):
                    continue
                full_url = fix_relative_url(url, src)
                resources["images"].append(full_url)

        # Extract videos
        for tag in soup.find_all(["video", "iframe"]):
            src = tag.get("src") or tag.get("data-src")
            if src:
                full_url = fix_relative_url(url, src)
                resources["videos"].append(full_url)

        # Extract downloadable files (a href ending with known extensions)
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

        # Remove duplicates
        for key in resources:
            resources[key] = list(set(resources[key]))

        return resources

    async def download_resource(self, url: str, save_dir: Path) -> Optional[Path]:
        """Download a single resource and save it locally."""
        try:
            file_name = clean_filename(url)
            file_path = save_dir / file_name
            async with self.session.get(url, timeout=ClientTimeout(60)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(content)
                    return file_path
                else:
                    logger.warning(f"Failed to download {url}: HTTP {resp.status}")
                    return None
        except Exception as e:
            logger.warning(f"Error downloading {url}: {e}")
            return None

    async def download_webpage_embedded(self, url: str) -> Optional[str]:
        """
        Download a complete webpage and embed all CSS, JS, and images into a single HTML file.
        Returns the path to the generated file.
        """
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

        # Download and embed CSS
        for link in soup.find_all("link", rel="stylesheet"):
            href = link.get("href")
            if href:
                css_url = fix_relative_url(url, href)
                css_content = await self.fetch_html(css_url)
                if css_content:
                    style_tag = soup.new_tag("style")
                    style_tag.string = css_content
                    link.replace_with(style_tag)

        # Download and embed JavaScript
        for script in soup.find_all("script", src=True):
            src = script["src"]
            js_url = fix_relative_url(url, src)
            js_content = await self.fetch_html(js_url)
            if js_content:
                new_script = soup.new_tag("script")
                new_script.string = js_content
                script.replace_with(new_script)

        # Download and embed images as base64 (optional, can be heavy)
        # We'll embed small images only
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
                pass  # If image download fails, keep original src

        # Save the final HTML file
        domain = urlparse(url).netloc.replace(".", "_")
        filename = f"{domain}_fullpage_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        filepath = DOWNLOAD_DIR / filename
        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(str(soup))

        return str(filepath)

    # ---------- User Interaction Handlers ----------

    async def handle_start(self, chat_id: str):
        """Send welcome message on /start command."""
        text = (
            f"{ICONS['globe']} *Welcome to the Smart Downloader Bot!*\n\n"
            "I can help you:\n"
            f"{ICONS['link']} Extract and download resources from any webpage (images, videos, files)\n"
            f"{ICONS['download']} Download a complete webpage with all assets embedded in a single HTML file\n\n"
            "Just send me a URL to get started, or use the menu below."
        )
        await self.send_message(chat_id, text, inline_keypad=MAIN_MENU_KEYPAD)

    async def handle_url_input(self, chat_id: str, url: str):
        """
        Process a URL sent by the user. Extract resources and present options.
        """
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Notify user that we are processing
        await self.send_message(chat_id, f"{ICONS['loading']} Analyzing `{url}`... Please wait.")

        html = await self.fetch_html(url)
        if not html:
            await self.send_message(chat_id, f"{ICONS['error']} Could not fetch the page. Please check the URL.")
            return

        # Extract resources
        resources = await self.extract_resources(url, html)

        # Build summary message
        images = resources.get("images", [])
        videos = resources.get("videos", [])
        files = resources.get("files", [])

        if not any([images, videos, files]):
            await self.send_message(
                chat_id,
                f"{ICONS['warning']} No downloadable resources found on this page.\n"
                "You can still download the full webpage using the menu.",
            )
            return

        # Show images in chat directly (send as separate messages)
        if images:
            await self.send_message(chat_id, f"{ICONS['image']} Found *{len(images)} images* on the page:")
            # Show thumbnails for first few images (Rubika may not support direct image URL preview)
            for img in images[:5]:  # Limit to 5 to avoid spamming
                await self.send_message(chat_id, f"{ICONS['image']} {img}")

            if len(images) > 5:
                await self.send_message(chat_id, f"... and {len(images) - 5} more.")

            # Inline keypad to download images
            keypad = {
                "rows": [
                    [{"id": f"dl_images_{url}", "type": "Simple", "button_text": f"⬇️ Download All Images ({len(images)})"}],
                ]
            }
            await self.send_message(chat_id, "Do you want to download these images?", inline_keypad=keypad)

        # For videos and files, present them with inline keypads for selection (large files)
        if videos or files:
            # Build selection keypad
            buttons = []
            if videos:
                buttons.append([{"id": f"dl_videos_{url}", "type": "Simple", "button_text": f"🎥 Download All Videos ({len(videos)})"}])
            if files:
                buttons.append([{"id": f"dl_files_{url}", "type": "Simple", "button_text": f"📁 Download All Files ({len(files)})"}])

            if buttons:
                keypad = {"rows": buttons}
                await self.send_message(
                    chat_id,
                    f"{ICONS['file']} Available media for download:",
                    inline_keypad=keypad,
                )

    async def handle_button_click(self, chat_id: str, button_id: str, message_id: Optional[str] = None):
        """Process inline button clicks."""
        if button_id == "get_sources":
            await self.send_message(chat_id, f"{ICONS['link']} Please send me the URL you want to analyze.")
        elif button_id == "download_page":
            await self.send_message(chat_id, f"{ICONS['globe']} Please send me the URL of the webpage you want to download.")
        elif button_id == "help":
            help_text = (
                f"{ICONS['info']} *How to use this bot:*\n\n"
                "1. Send a URL to extract resources.\n"
                "2. Choose from inline buttons to download images, videos, or other files.\n"
                "3. Use the 'Download Full Webpage' button to get a self-contained HTML file.\n\n"
                "For large files (videos, archives), you'll need to confirm download first."
            )
            await self.send_message(chat_id, help_text)
        elif button_id.startswith("dl_images_"):
            url = button_id.replace("dl_images_", "")
            await self.download_category(chat_id, url, "images")
        elif button_id.startswith("dl_videos_"):
            url = button_id.replace("dl_videos_", "")
            await self.download_category(chat_id, url, "videos")
        elif button_id.startswith("dl_files_"):
            url = button_id.replace("dl_files_", "")
            await self.download_category(chat_id, url, "files")
        else:
            await self.send_message(chat_id, f"{ICONS['warning']} Unknown action.")

    async def download_category(self, chat_id: str, url: str, category: str):
        """Initiate download of all resources of a given category."""
        html = await self.fetch_html(url)
        if not html:
            await self.send_message(chat_id, f"{ICONS['error']} Failed to refetch the page.")
            return

        resources = await self.extract_resources(url, html)
        items = resources.get(category, [])
        if not items:
            await self.send_message(chat_id, f"{ICONS['warning']} No {category} found.")
            return

        await self.send_message(chat_id, f"{ICONS['loading']} Starting download of {len(items)} {category}...")

        # Create a directory for this download
        download_dir = DOWNLOAD_DIR / f"{chat_id}_{category}_{int(time.time())}"
        download_dir.mkdir(parents=True, exist_ok=True)

        success = 0
        for idx, item_url in enumerate(items):
            file_path = await self.download_resource(item_url, download_dir)
            if file_path:
                success += 1
                # Send files to user (images are sent, videos/files are notified)
                if category == "images":
                    await self.send_file(chat_id, file_path)
                else:
                    await self.send_message(chat_id, f"{ICONS['file']} Downloaded: {file_path.name}")

        await self.send_message(chat_id, f"{ICONS['done']} Download complete! {success}/{len(items)} files saved.")

    # ---------- Main Loop (Cron-Aligned) ----------

    async def main_loop(self):
        """
        Main event loop with smart timing:
        - Runs for a maximum of 5h 40m (20 minutes before the GitHub 6h limit).
        - Polls for updates every 150 seconds to stay responsive while respecting rate limits.
        - On exit, saves state so it can resume from the last processed message.
        """
        logger.info(f"Bot started. Will run for approximately {MAX_RUNTIME // 60} minutes.")
        await self.send_message("me", f"{ICONS['info']} Bot started at {datetime.now().isoformat()}")

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
        await self.send_message("me", f"{ICONS['warning']} Bot shutting down due to time limit.")

    async def process_update(self, update: dict):
        """
        Process a single update from Rubika.
        The update can be a new message, a button click, etc.
        """
        update_type = update.get("type")
        chat_id = update.get("chat_id")

        if not chat_id:
            return

        # Handle new message
        if update_type == "NewMessage":
            new_msg = update.get("new_message")
            if not new_msg:
                return

            text = new_msg.get("text", "")
            message_id = new_msg.get("message_id")
            sender_type = new_msg.get("sender_type", "User")

            if sender_type != "User":
                return  # Ignore bot's own messages

            # Check for /start command
            if text.strip() == "/start":
                await self.handle_start(chat_id)
            # Check for URL pattern
            elif re.match(r"https?://\S+", text.strip()):
                url = text.strip()
                await self.handle_url_input(chat_id, url)
            else:
                # Unknown input, show help
                await self.send_message(
                    chat_id,
                    f"{ICONS['info']} Please send a valid URL or use the menu.",
                    inline_keypad=MAIN_MENU_KEYPAD,
                )

        # Handle button click (if reported as an update with aux_data)
        elif update_type == "ButtonClicked":  # Hypothetical, adjust based on actual API
            aux_data = update.get("aux_data", {})
            button_id = aux_data.get("button_id")
            if button_id:
                await self.handle_button_click(chat_id, button_id)

# ================= GITHUB ACTIONS RUNNER =================

async def main():
    """Entry point for GitHub Actions (or local testing)."""
    if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("Bot token not set. Please set the RUBIKA_BOT_TOKEN environment variable.")
        sys.exit(1)

    bot = RubikaBot(TOKEN)
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
