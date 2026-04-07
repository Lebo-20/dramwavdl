import os
import asyncio
import logging
import shutil
import tempfile
import random
import json
from telethon import TelegramClient, events, Button
from dotenv import load_dotenv

load_dotenv()

# Local imports
from api import (
    get_drama_detail, get_episode_data, get_popular_feed, search_drama
)
from downloader import aria2c_download, download_episode_with_subs
from merge import merge_and_hardsub
from uploader import upload_drama

# Configuration
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
AUTO_CHANNEL = int(os.environ.get("AUTO_CHANNEL", ADMIN_ID))
PROCESSED_FILE = "processed.json"

# Initialize state
def load_processed():
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r") as f:
                return set(json.load(f))
        except:
            return set()
    return set()

def save_processed(data):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(data), f)

processed_ids = load_processed()

# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class BotState:
    is_auto_running = True
    is_processing = False

# Initialize client
client = TelegramClient('dramawave_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

def get_panel_buttons():
    status_text = "🟢 RUNNING" if BotState.is_auto_running else "🔴 STOPPED"
    return [
        [Button.inline("▶️ Start Auto", b"start_auto"), Button.inline("⏹ Stop Auto", b"stop_auto")],
        [Button.inline(f"📊 Status: {status_text}", b"status")]
    ]

@client.on(events.NewMessage(pattern='/update'))
async def update_bot(event):
    if event.sender_id != ADMIN_ID: return
    import subprocess, sys
    status_msg = await event.reply("🔄 **Menarik pembaruan dari Git...**")
    try:
        # Pastikan file __pycache__ tidak mengganggu
        import shutil
        if os.path.exists("__pycache__"):
            shutil.rmtree("__pycache__", ignore_errors=True)
            
        # Simpan perubahan lokal sementara (seperti processed.json jika dilacak)
        subprocess.run(["git", "stash"], capture_output=True)
        
        # Menarik update
        process = subprocess.run(["git", "pull"], capture_output=True, text=True)
        output = process.stdout + process.stderr
        
        # Kembalikan perubahan lokal
        subprocess.run(["git", "stash", "pop"], capture_output=True)
        
        if "Already up to date" in output:
            await status_msg.edit("✅ **Bot sudah versi terbaru.**")
            return

        await status_msg.edit(f"✅ **Update Berhasil!**\n\n```\n{output[:500]}\n```\n🔄 Memulai ulang bot...")
        
        # Restart file
        os.execl(sys.executable, sys.executable, *sys.argv)
    except Exception as e:
        await status_msg.edit(f"❌ **Gagal update:**\n`{e}`")

@client.on(events.NewMessage(pattern='/panel'))
async def panel(event):
    if event.chat_id != ADMIN_ID: return
    await event.reply("🎛 **DramaWave Control Panel**", buttons=get_panel_buttons())

@client.on(events.NewMessage(pattern=r'/cari (.+)'))
async def on_search(event):
    if event.chat_id != ADMIN_ID: return
    query = event.pattern_match.group(1)
    status_msg = await event.reply(f"🔍 Mencari drama: **{query}**...")
    
    results = await search_drama(query)
    if not results:
        await status_msg.edit(f"❌ Tidak ditemukan hasil untuk: **{query}**")
        return
        
    buttons = []
    # Limit results to 10 for clean display
    for d in results[:10]:
        title = d.get("name") or d.get("title") or "Unknown"
        drama_id = d.get("playlet_id") or d.get("id") or d.get("key")
        if drama_id:
            buttons.append([Button.inline(f"🎬 {title[:30]}...", data=f"dl_{drama_id}")])
            
    await status_msg.edit(f"✅ Ditemukan {len(results)} hasil untuk: **{query}**\nKlik tombol di bawah untuk download.", buttons=buttons)

@client.on(events.CallbackQuery())
async def panel_callback(event):
    if event.sender_id != ADMIN_ID: return
    data = event.data.decode()
    
    if data == "start_auto":
        BotState.is_auto_running = True
        await event.answer("Auto-mode started!")
        await event.edit("🎛 **DramaWave Control Panel**", buttons=get_panel_buttons())
    elif data == "stop_auto":
        BotState.is_auto_running = False
        await event.answer("Auto-mode stopped!")
        await event.edit("🎛 **DramaWave Control Panel**", buttons=get_panel_buttons())
    elif data == "status":
        await event.answer(f"Status: {'Running' if BotState.is_auto_running else 'Stopped'}")
    elif data.startswith("dl_"):
        drama_id = data.replace("dl_", "")
        if BotState.is_processing:
            await event.answer("⚠️ Sedang memproses drama lain.", alert=True)
            return
            
        await event.answer("🚀 Sedang memproses download...")
        # Auto edit the search message into a status message
        BotState.is_processing = True
        await process_drama_full(drama_id, event.chat_id, event)
        BotState.is_processing = False

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.reply("Welcome to DramaWave Downloader Bot! 🎉\n\nGunakan perintah `/cari {judul}` atau `/download {ID}` untuk mulai.")

@client.on(events.NewMessage(pattern=r'/download ([\w-]+)'))
async def on_download(event):
    if event.chat_id != ADMIN_ID: return
    if BotState.is_processing:
        await event.reply("⚠️ Sedang memproses drama lain.")
        return
        
    drama_id = event.pattern_match.group(1)
    status_msg = await event.reply(f"🔍 Mencari drama `{drama_id}`...")
    
    BotState.is_processing = True
    success = await process_drama_full(drama_id, event.chat_id, status_msg)
    BotState.is_processing = False
    
    if success:
        processed_ids.add(drama_id)
        save_processed(processed_ids)

async def process_drama_full(drama_id, chat_id, status_obj=None):
    """DramaWave Pipeline: Fetch -> Download with Subs -> Burn Subtitles -> Merge -> Upload."""
    # status_obj can be a Message object or a Callback event (which has .edit)
    # Helper to edit regardless of type
    async def fast_edit(text, buttons=None):
        try:
            if hasattr(status_obj, 'edit'):
                await status_obj.edit(text, buttons=buttons)
            else:
                # If it's a message ID or similar logic needed? Usually status_obj is the msg
                pass
        except Exception as e:
            logger.warning(f"Edit failed: {e}")

    try:
        await fast_edit(f"🔍 Sedang mengambil detail drama `{drama_id}`...")
        detail = await get_drama_detail(drama_id)
        if not detail:
            await fast_edit(f"❌ Drama `{drama_id}` tidak ditemukan.")
            return False

        title = detail.get("name") or detail.get("title") or f"Drama_{drama_id}"
        description = detail.get("description") or detail.get("intro") or "No description."
        poster = detail.get("cover") or detail.get("poster") or ""
        
        detail_items = detail.get("items", [])
        total_eps = len(detail_items) if detail_items else 0
        
        await fast_edit(f"🎬 Processing **{title}** ({total_eps} episodes)...")

        temp_dir = tempfile.mkdtemp(prefix=f"dw_{drama_id}_")
        video_dir = os.path.join(temp_dir, "episodes")
        os.makedirs(video_dir, exist_ok=True)

        # 3. Download episodes (1 -> total_eps)
        semaphore = asyncio.Semaphore(5)
        
        async def download_task(ep_num):
            async with semaphore:
                if ep_num - 1 >= len(detail_items):
                    return False
                play_data = detail_items[ep_num - 1]
                video_url = play_data.get("1080p_mp4") or play_data.get("720p_mp4") or play_data.get("video_url")
                sub_list = play_data.get("subtitle_list") or []
                sub_url = None
                if isinstance(sub_list, list):
                    for s in sub_list:
                        lang = s.get("language") or s.get("lang") or ""
                        if lang in ["id-ID", "id", "in"]:
                            sub_url = s.get("subtitle") or s.get("vtt")
                            break
                    if not sub_url and sub_list:
                        sub_url = sub_list[0].get("subtitle") or sub_list[0].get("vtt")
                
                if not video_url: return False
                return await download_episode_with_subs(ep_num, video_url, sub_url, video_dir)

        # Update status before download
        await fast_edit(f"📥 **Downloading {total_eps} episode...**\nMohon tunggu sejenak.")
        download_results = await asyncio.gather(*(download_task(i) for i in range(1, total_eps + 1)))
        
        if not all(download_results):
            await fast_edit(f"❌ Gagal mendownload beberapa episode dari **{title}**.")
            # return False # Continue anyway?

        # 4. Hardsub and Merge
        async def progress_callback(text):
            await fast_edit(text)

        output_path = os.path.join(temp_dir, f"{title}.mp4")
        merge_success = await merge_and_hardsub(video_dir, output_path, progress_callback, title)
        if not merge_success:
            await fast_edit("❌ Proses Hardsub/Merge Gagal.")
            return False

        # 5. Upload
        await fast_edit(f"📤 Mengunggah **{title}** ke Telegram...")
        upload_success = await upload_drama(client, chat_id, title, description, poster, output_path)
        
        if upload_success:
            if hasattr(status_obj, 'delete'):
                try: await status_obj.delete()
                except: pass
            return True
        else:
            await fast_edit("❌ Upload Gagal.")
            return False

    except Exception as e:
        logger.error(f"Error processing drama {drama_id}: {e}")
        await fast_edit(f"❌ Error: {e}")
        return False
    finally:
        if 'temp_dir' in locals() and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

async def auto_mode_loop():
    logger.info("🚀 DramaWave Auto-Mode Active.")
    
    while True:
        if not BotState.is_auto_running:
            await asyncio.sleep(10)
            continue
            
        try:
            logger.info("🔍 Checking popular feed...")
            dramas = await get_popular_feed(page=1)
            
            for drama in dramas:
                if not BotState.is_auto_running: break
                
                drama_id = drama.get("playlet_id") or drama.get("id") or drama.get("key")
                if not drama_id: continue
                
                drama_id = str(drama_id)
                if drama_id not in processed_ids:
                    title = drama.get("name") or drama.get("title") or "Unknown"
                    logger.info(f"✨ New drama found: {title} ({drama_id})")
                    
                    status_msg = None
                    try:
                        status_msg = await client.send_message(ADMIN_ID, f"🆕 **Auto-Detect Drama Baru!**\n🎬 {title}\n🆔 `{drama_id}`\n⏳ Sedang memproses hardsub...")
                    except: pass
                    
                    BotState.is_processing = True
                    success = await process_drama_full(drama_id, AUTO_CHANNEL, status_msg)
                    BotState.is_processing = False
                    
                    if success:
                        processed_ids.add(drama_id)
                        save_processed(processed_ids)
                        await client.send_message(ADMIN_ID, f"✅ Sukses Post: **{title}**")
                    else:
                        logger.error(f"Failed to process {title}")
                    
                    await asyncio.sleep(15) 

            await asyncio.sleep(3600) 
        except Exception as e:
            logger.error(f"Error in auto_mode: {e}")
            await asyncio.sleep(300)

if __name__ == '__main__':
    logger.info("Bot started.")
    client.loop.create_task(auto_mode_loop())
    client.run_until_disconnected()
