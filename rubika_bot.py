"""
Rubika Smart Downloader Bot
- Synchronous HTTP with 'requests' library
- Inline keypad with correct structure (rows → buttons)
- Button clicks via aux_data.button_id
- 24/7 cron job compatible with GitHub Actions
"""

import os
import sys
import re
import time
import json
import base64
import hashlib
import logging
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup

# ==================== CONFIGURATION ====================
TOKEN = os.getenv("RUBIKA_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ RUBIKA_BOT_TOKEN environment variable is not set.")

BASE_URL = "https://botapi.rubika.ir/v3"
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# GitHub Actions timing: run 5h40m (20 min before 6h limit), then exit
# Cron schedule: every 5 minutes → nearly 24/7 coverage
POLL_INTERVAL = 5          # seconds (as recommended by Rubika docs)
MAX_RUNTIME = 6 * 3600 - 20 * 60  # 5h 40m

# Optional admin chat ID for startup/shutdown notifications
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# ==================== EMOJIS ====================
ICONS = {
    "success": "✅", "error": "❌", "warning": "⚠️", "info": "ℹ️",
    "file": "📄", "image": "🖼️", "video": "🎥", "audio": "🎵",
    "download": "⬇️", "loading": "⏳", "done": "🎉", "link": "🔗",
    "folder": "📁", "globe": "🌐", "robot": "🤖", "star": "⭐",
    "clock": "🕐", "package": "📦", "wrench": "🔧",
}

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("RubikaBot")

# ==================== INLINE KEYPAD TEMPLATES ====================
def build_keypad(rows: List[List[dict]]) -> dict:
    """
    Build a valid Rubika inline_keypad structure.
    Each row must be: {"buttons": [ ... ]}
    """
    return {
        "rows": [
            {"buttons": row} for row in rows
        ]
    }

MAIN_MENU = build_keypad([
    [{"id": "get_sources", "type": "Simple", "button_text": "📥 Get Page Sources"}],
    [{"id": "download_page", "type": "Simple", "button_text": "🌐 Download Full Webpage"}],
    [{"id": "help", "type": "Simple", "button_text": "❓ Help"}],
])

# ==================== UTILITY FUNCTIONS ====================
def clean_filename(url_path: str) -> str:
    """Create a valid filename from a URL path."""
    name = url_path.split("/")[-1] or "index.html"
    name = unquote(name)
    if "?" in name:
        name = name.split("?")[0]
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "download"

def fix_relative_url(base_url: str, relative_url: str) -> str:
    """Resolve a relative URL against a base URL."""
    return urljoin(base_url, relative_url)

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

# ==================== RUBIKA BOT CLASS ====================
class RubikaBot:
    def __init__(self, token: str):
        self.token = token
        self.base_url = BASE_URL
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.offset_id: Optional[str] = None
        self.start_time = time.time()

    def is_time_exceeded(self) -> bool:
        """Check if the bot has run beyond the allowed time (GitHub Actions limit)."""
        return (time.time() - self.start_time) >= MAX_RUNTIME

    # ---------- Rubika API Methods ----------
    def call_api(self, method: str, data: dict) -> dict:
        """Generic method to call Rubika Bot API."""
        url = f"{self.base_url}/{self.token}/{method}"
        try:
            resp = self.session.post(url, json=data, timeout=30)
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"API call failed ({method}): {e}")
            return {"status": "ERROR", "message": str(e)}

    def get_updates(self, limit: int = 10) -> List[dict]:
        """Fetch new updates using Long Polling (offset_id)."""
        data = {"limit": limit}
        if self.offset_id:
            data["offset_id"] = self.offset_id
        response = self.call_api("getUpdates", data)
        updates = response.get("updates", [])
        if response.get("next_offset_id"):
            self.offset_id = response["next_offset_id"]
        return updates

    def send_message(
        self,
        chat_id: str,
        text: str,
        inline_keypad: Optional[dict] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[str]:
        """Send a text message, optionally with an inline keypad."""
        data = {"chat_id": chat_id, "text": text}
        if inline_keypad:
            data["inline_keypad"] = inline_keypad
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id

        response = self.call_api("sendMessage", data)
        if response.get("status") == "OK":
            return response.get("message_id")
        else:
            logger.error(f"sendMessage failed: {response}")
            return None

    def edit_message_keypad(
        self, chat_id: str, message_id: str, inline_keypad: dict
    ) -> bool:
        """Update the inline keypad of an existing message."""
        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "inline_keypad": inline_keypad,
        }
        response = self.call_api("editMessageKeypad", data)
        return response.get("status") == "OK"

    # ---------- Web Scraping & Download ----------
    def fetch_html(self, url: str) -> Optional[str]:
        """Fetch HTML content from a URL."""
        try:
            resp = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (compatible; RubikaBot/1.0)"
            })
            if resp.status_code == 200:
                return resp.text
            else:
                logger.warning(f"Fetch {url} returned HTTP {resp.status_code}")
                return None
        except requests.RequestException as e:
            logger.error(f"Error fetching {url}: {e}")
            return None

    def extract_resources(self, url: str, html: str) -> Dict[str, List[str]]:
        """
        Parse HTML and extract links to images, videos, and other files.
        Returns dict with keys: 'images', 'videos', 'files'.
        """
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

    def download_resource(self, url: str, save_dir: Path) -> Optional[Path]:
        """Download a single resource and save it locally."""
        try:
            file_name = clean_filename(url)
            file_path = save_dir / file_name
            resp = requests.get(url, stream=True, timeout=120, headers={
                "User-Agent": "Mozilla/5.0 (compatible; RubikaBot/1.0)"
            })
            if resp.status_code == 200:
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                return file_path
            else:
                logger.warning(f"Download {url} failed (HTTP {resp.status_code})")
                return None
        except requests.RequestException as e:
            logger.warning(f"Error downloading {url}: {e}")
            return None

    def download_webpage_embedded(self, url: str) -> Optional[str]:
        """
        Download a complete webpage and embed all CSS, JS, and images
        into a single HTML file. Returns the path to the generated file.
        """
        html = self.fetch_html(url)
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
                css_content = self.fetch_html(css_url)
                if css_content:
                    style_tag = soup.new_tag("style")
                    style_tag.string = css_content
                    link.replace_with(style_tag)

        # Embed scripts
        for script in soup.find_all("script", src=True):
            src = script["src"]
            js_url = fix_relative_url(url, src)
            js_content = self.fetch_html(js_url)
            if js_content:
                new_script = soup.new_tag("script")
                new_script.string = js_content
                script.replace_with(new_script)

        # Convert images to base64
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if src.startswith("data:"):
                continue
            img_url = fix_relative_url(url, src)
            try:
                resp = requests.get(img_url, timeout=30, headers={
                    "User-Agent": "Mozilla/5.0"
                })
                if resp.status_code == 200:
                    content_type = resp.headers.get("Content-Type", "")
                    if "image" in content_type:
                        img_data = resp.content
                        b64 = base64.b64encode(img_data).decode()
                        img["src"] = f"data:{content_type};base64,{b64}"
            except Exception:
                pass

        domain = urlparse(url).netloc.replace(".", "_")
        filename = f"{domain}_fullpage_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        filepath = DOWNLOAD_DIR / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(str(soup))
        return str(filepath)

    # ---------- User Interaction Handlers ----------
    def handle_start(self, chat_id: str):
        """Send welcome message on /start command."""
        text = (
            f"{ICONS['globe']} *Welcome to the Smart Downloader Bot!*\n\n"
            f"I can help you:\n"
            f"{ICONS['link']} Extract and download resources from any webpage (images, videos, files)\n"
            f"{ICONS['download']} Download a complete webpage with all assets embedded in a single HTML file\n\n"
            f"Send me a URL to begin, or use the menu below."
        )
        self.send_message(chat_id, text, inline_keypad=MAIN_MENU)

    def handle_url_input(self, chat_id: str, url: str):
        """
        Process a URL sent by the user.
        Extract resources and present options via inline keypads.
        """
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        self.send_message(chat_id, f"{ICONS['loading']} Analyzing `{url}`…")

        html = self.fetch_html(url)
        if not html:
            self.send_message(chat_id, f"{ICONS['error']} Could not fetch the page. Please check the URL.")
            return

        resources = self.extract_resources(url, html)
        images = resources["images"]
        videos = resources["videos"]
        files = resources["files"]

        if not any([images, videos, files]):
            self.send_message(
                chat_id,
                f"{ICONS['warning']} No downloadable resources found on this page.\n"
                f"You can still download the full webpage using the menu button.",
                inline_keypad=MAIN_MENU,
            )
            return

        # Images: show first few URLs, offer download button
        if images:
            self.send_message(chat_id, f"{ICONS['image']} Found *{len(images)} images* on the page:")
            for img in images[:5]:
                self.send_message(chat_id, f"{ICONS['image']} {img}")
            if len(images) > 5:
                self.send_message(chat_id, f"... and {len(images) - 5} more.")

            download_images_kp = build_keypad([
                [{"id": f"dl_images|{url}", "type": "Simple",
                  "button_text": f"⬇️ Download All Images ({len(images)})"}],
            ])
            self.send_message(chat_id, "Download all images?", inline_keypad=download_images_kp)

        # Videos and files: offer selection (large files, user confirmation needed)
        if videos or files:
            buttons = []
            if videos:
                buttons.append([{"id": f"dl_videos|{url}", "type": "Simple",
                                 "button_text": f"🎥 Download All Videos ({len(videos)})"}])
            if files:
                buttons.append([{"id": f"dl_files|{url}", "type": "Simple",
                                 "button_text": f"📁 Download All Files ({len(files)})"}])
            if buttons:
                media_kp = build_keypad(buttons)
                self.send_message(chat_id, "Download these media files?", inline_keypad=media_kp)

    def handle_button_click(self, chat_id: str, button_id: str, message_id: Optional[str] = None):
        """Process inline button clicks (from aux_data.button_id)."""
        if button_id == "get_sources":
            self.send_message(chat_id, f"{ICONS['link']} Please send me the URL you want to analyze.")
        elif button_id == "download_page":
            self.send_message(chat_id, f"{ICONS['globe']} Please send me the URL of the webpage you want to download.")
        elif button_id == "help":
            help_text = (
                f"{ICONS['info']} *How to use this bot:*\n\n"
                f"1. Send a URL to extract resources.\n"
                f"2. Use the buttons to download images, videos, or files.\n"
                f"3. Use 'Download Full Webpage' to get a self-contained HTML file.\n\n"
                f"Large files (videos, archives) require confirmation before downloading."
            )
            self.send_message(chat_id, help_text)
        elif button_id.startswith("dl_images|"):
            url = button_id.split("|", 1)[1]
            self.download_category(chat_id, url, "images")
        elif button_id.startswith("dl_videos|"):
            url = button_id.split("|", 1)[1]
            self.download_category(chat_id, url, "videos")
        elif button_id.startswith("dl_files|"):
            url = button_id.split("|", 1)[1]
            self.download_category(chat_id, url, "files")
        else:
            self.send_message(chat_id, f"{ICONS['warning']} Unknown action: {button_id}")

    def download_category(self, chat_id: str, url: str, category: str):
        """Download all resources of a given category (images/videos/files)."""
        html = self.fetch_html(url)
        if not html:
            self.send_message(chat_id, f"{ICONS['error']} Failed to refetch the page.")
            return

        resources = self.extract_resources(url, html)
        items = resources.get(category, [])
        if not items:
            self.send_message(chat_id, f"{ICONS['warning']} No {category} found.")
            return

        self.send_message(chat_id, f"{ICONS['loading']} Starting download of {len(items)} {category}...")

        download_dir = DOWNLOAD_DIR / f"{chat_id}_{category}_{int(time.time())}"
        download_dir.mkdir(parents=True, exist_ok=True)

        success = 0
        for item_url in items:
            file_path = self.download_resource(item_url, download_dir)
            if file_path:
                success += 1
                size = format_file_size(file_path.stat().st_size)
                self.send_message(chat_id, f"{ICONS['file']} Downloaded: `{file_path.name}` ({size})")

        self.send_message(
            chat_id,
            f"{ICONS['done']} Download complete! {success}/{len(items)} files saved to:\n"
            f"`{download_dir}`",
            inline_keypad=MAIN_MENU,
        )

    def handle_fullpage_download(self, chat_id: str, url: str):
        """Handle the 'download full webpage' action."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        self.send_message(chat_id, f"{ICONS['loading']} Downloading and embedding assets for `{url}`...\nThis may take a while.")

        filepath = self.download_webpage_embedded(url)
        if filepath:
            size = format_file_size(Path(filepath).stat().st_size)
            self.send_message(
                chat_id,
                f"{ICONS['done']} Full webpage saved!\n"
                f"File: `{Path(filepath).name}`\n"
                f"Size: {size}\n"
                f"All CSS, JS, and images have been embedded into a single HTML file.",
                inline_keypad=MAIN_MENU,
            )
        else:
            self.send_message(
                chat_id,
                f"{ICONS['error']} Failed to download the webpage. Please check the URL.",
                inline_keypad=MAIN_MENU,
            )

    # ---------- Main Loop ----------
    def process_update(self, update: dict):
        """
        Process a single update from Rubika.
        The update can be a new message (with optional aux_data for button clicks).
        """
        update_type = update.get("type")
        chat_id = update.get("chat_id")

        if not chat_id:
            return 0

        if update_type == "NewMessage":
            new_msg = update.get("new_message")
            if not new_msg:
                return 0

            text = new_msg.get("text", "").strip()
            sender_type = new_msg.get("sender_type", "User")
            message_id = new_msg.get("message_id")

            if sender_type != "User":
                return 0  # Ignore bot's own messages

            # Check for button click via aux_data
            aux_data = new_msg.get("aux_data")
            if aux_data and isinstance(aux_data, dict) and aux_data.get("button_id"):
                button_id = aux_data["button_id"]
                logger.info(f"Button clicked: {button_id} by {chat_id}")
                # If it's a download_fullpage button
                if button_id.startswith("dl_fullpage|"):
                    url = button_id.split("|", 1)[1]
                    self.handle_fullpage_download(chat_id, url)
                else:
                    self.handle_button_click(chat_id, button_id, message_id)
                return 1

            # Process text commands
            if text == "/start":
                self.handle_start(chat_id)
                return 1
            elif text == "/help":
                self.send_message(
                    chat_id,
                    f"{ICONS['robot']} Send me a URL to start!",
                    inline_keypad=MAIN_MENU,
                )
                return 1

            # Check for URL pattern
            url_match = re.match(r"https?://\S+", text)
            if url_match:
                url = url_match.group(0)

                # Offer both options
                choice_kp = build_keypad([
                    [{"id": f"dl_sources|{url}", "type": "Simple",
                      "button_text": "📥 Extract Resources"}],
                    [{"id": f"dl_fullpage|{url}", "type": "Simple",
                      "button_text": "🌐 Download Full Page"}],
                ])
                self.send_message(
                    chat_id,
                    f"{ICONS['link']} I detected a URL: `{url}`\n\n"
                    f"What would you like to do?",
                    inline_keypad=choice_kp,
                )
                return 1

            # Handle dl_sources button
            if aux_data and isinstance(aux_data, dict) and aux_data.get("button_id"):
                button_id = aux_data["button_id"]
                if button_id.startswith("dl_sources|"):
                    url = button_id.split("|", 1)[1]
                    self.handle_url_input(chat_id, url)
                    return 1

            # Unknown input
            self.send_message(
                chat_id,
                f"{ICONS['info']} Please send a valid URL or use the menu.",
                inline_keypad=MAIN_MENU,
            )
            return 1

        return 0

    def run(self):
        """Main synchronous loop for the bot."""
        logger.info(f"{ICONS['robot']} Bot started. Will run for ~{MAX_RUNTIME // 60} minutes.")
        logger.info(f"Polling every {POLL_INTERVAL}s | Token: {self.token[:8]}...")

        if ADMIN_CHAT_ID:
            self.send_message(ADMIN_CHAT_ID, f"{ICONS['robot']} Bot online at {datetime.now().isoformat()}")

        processed_count = 0
        while not self.is_time_exceeded():
            try:
                updates = self.get_updates(limit=10)
                for update in updates:
                    processed_count += self.process_update(update)
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error(f"Error in main loop: {traceback.format_exc()}")
                time.sleep(5)

        logger.info(f"{ICONS['clock']} Time limit reached. Processed {processed_count} updates. Shutting down.")
        if ADMIN_CHAT_ID:
            self.send_message(ADMIN_CHAT_ID, f"{ICONS['warning']} Bot shutting down (time limit).")

# ==================== GITHUB ACTIONS ENTRY POINT ====================
def main():
    bot = RubikaBot(TOKEN)
    bot.run()

if __name__ == "__main__":
    main()
