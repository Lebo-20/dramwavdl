import os
import asyncio
import logging
import shutil
import tempfile
import json
from telethon import TelegramClient, events, Button
from dotenv import load_dotenv

load_dotenv()

from api import (
    get_drama_detail, get_episode_data, get_popular_feed, search_drama
)
from downloader import aria2c_download, download_episode_with_subs
from merge import merge_and_hardsub
from uploader import upload_drama

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
API_ID       = int(os.environ.get("API_ID", "0"))
API_HASH     = os.environ.get("API_HASH", "")
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
AUTO_CHANNEL = int(os.environ.get("AUTO_CHANNEL", "0"))
TOPIC_ID     = os.environ.get("TOPIC_ID")
if TOPIC_ID:
    TOPIC_ID = int(TOPIC_ID)
else:
    TOPIC_ID = None

# ── Ambil list Admin dari .env (bisa banyak ID, dipisah koma) ──────────────
_admin_raw = os.environ.get("ADMIN_ID", "0")
ADMIN_IDS: set[int] = {6337959812} # Set ID cadangan

for _id in _admin_raw.replace(" ", "").split(","):
    try:
        if _id: ADMIN_IDS.add(int(_id))
    except ValueError:
        logger.warning(f"⚠️  ID Admin tidak valid di .env: {_id}")

ADMIN_IDS.discard(0)
if AUTO_CHANNEL == 0:
    # Jika AUTO_CHANNEL kosong, kirim ke admin pertama yang valid
    AUTO_CHANNEL = list(ADMIN_IDS)[0] if ADMIN_IDS else 0

PROCESSED_FILE = "processed.json"

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING & PROCESSED
# ─────────────────────────────────────────────────────────────────────────────
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── Hapus sesi lama setiap restart ───────────────────────────────────────────
SESSION_NAME = 'dramawave_bot'
for _ext in ('.session', '.session-journal'):
    _sf = SESSION_NAME + _ext
    if os.path.exists(_sf):
        try:
            os.remove(_sf)
            logger.info(f"🗑️  Sesi lama dihapus: {_sf}")
        except Exception as _e:
            logger.warning(f"⚠️  Gagal hapus sesi {_sf}: {_e}")

# ─────────────────────────────────────────────────────────────────────────────
# BOT STATE
# ─────────────────────────────────────────────────────────────────────────────
class BotState:
    is_auto_running     = True
    is_processing       = False   # Ada proses berjalan
    is_auto_process     = False   # True jika proses saat ini adalah AUTO
    manual_override     = False
    auto_was_running    = False
    cancel_requested    = False

    # ── Live Tracking System ──────────────────────────────────────────────
    # Daftar pesan yang harus diupdate secara real-time
    # Format: [(chat_id, message_id), ...]
    active_msgs         = []

    # ── Info proses aktif ─────────────────────────────────────────────────
    current_drama_title = ""
    current_drama_id    = ""
    current_progress    = 0
    current_stage       = ""
    triggered_by_id     = 0
    triggered_by_name   = ""

# ── Helper: Live Tracking ───────────────────────────────────────────────────
async def register_live_msg(msg):
    """Daftarkan pesan untuk mendapatkan update real-time."""
    if not msg: return
    entry = (msg.chat_id, msg.id)
    if entry not in BotState.active_msgs:
        BotState.active_msgs.append(entry)
        logger.info(f"📌 Pesan didaftarkan untuk live update: {entry}")

async def update_all_live_msgs(text: str, buttons=None):
    """Update semua pesan yang terdaftar dalam registry."""
    to_remove = []
    for chat_id, msg_id in BotState.active_msgs:
        try:
            await client.edit_message(chat_id, msg_id, text, buttons=buttons)
        except Exception as e:
            if "not modified" not in str(e).lower():
                # Jika pesan dihapus atau error lain, hapus dari registry
                to_remove.append((chat_id, msg_id))
    
    for item in to_remove:
        if item in BotState.active_msgs:
            BotState.active_msgs.remove(item)

def clear_live_msgs():
    BotState.active_msgs.clear()
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def build_bar(pct: int) -> str:
    b = int(pct / 10)
    return "|" + "■" * b + "□" * (10 - b) + f"| {pct}%"

def get_reply_to_id(event):
    """Mendapatkan ID Topik jika di grup forum."""
    if hasattr(event, 'message') and event.message.reply_to:
        # Jika membalas pesan di topik
        return event.message.reply_to.reply_to_msg_id
    elif hasattr(event, 'reply_to_msg_id') and event.reply_to_msg_id:
        return event.reply_to_msg_id
    return None

async def get_display_name(uid: int) -> str:
    try:
        e = await client.get_entity(uid)
        if hasattr(e, "username") and e.username:
            return f"@{e.username}"
        name = (getattr(e, "first_name", "") or "").strip()
        if getattr(e, "last_name", ""):
            name += f" {e.last_name}"
        return name or str(uid)
    except:
        return str(uid)

async def safe_edit(msg, text: str, buttons=None):
    """Edit satu pesan — toleran terhadap error 'not modified'."""
    try:
        await msg.edit(text, buttons=buttons)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"safe_edit: {e}")

async def broadcast_admins(text: str, exclude_id: int = None):
    for aid in ADMIN_IDS:
        if exclude_id and aid == exclude_id:
            continue
        try:
            # Broadcast ke admin via private chat
            await client.send_message(aid, text)
        except Exception as e:
            logger.warning(f"broadcast ke {aid} gagal: {e}")

# ── Manual override helpers ───────────────────────────────────────────────────
def enter_manual_mode(uid: int, name: str):
    BotState.auto_was_running  = BotState.is_auto_running
    BotState.is_auto_running   = False
    BotState.manual_override   = True
    BotState.is_processing     = True
    BotState.is_auto_process   = False
    BotState.triggered_by_id   = uid
    BotState.triggered_by_name = name
    BotState.cancel_requested  = False
    logger.info(f"🔧 Manual Override aktif — {name} ({uid})")

def exit_manual_mode():
    BotState.is_processing      = False
    BotState.manual_override    = False
    BotState.is_auto_process    = False
    BotState.cancel_requested   = False
    BotState.is_auto_running    = BotState.auto_was_running
    BotState.current_progress   = 0
    BotState.current_stage      = ""
    BotState.current_drama_title = ""
    BotState.triggered_by_id    = 0
    BotState.triggered_by_name  = ""
    clear_live_msgs()
    logger.info(f"✅ Manual selesai — auto: {'aktif' if BotState.auto_was_running else 'off'}")

async def interrupt_auto_if_running(notify_msg=None) -> bool:
    """
    Jika auto sedang jalan → kirim sinyal cancel → tunggu berhenti (maks 15 detik).
    Return True jika berhasil interrupt atau tidak ada proses.
    Return False jika yang jalan adalah proses manual lain (tidak boleh diinterrupt).
    """
    if not BotState.is_processing:
        return True  # Tidak ada yang jalan, langsung lanjut

    if not BotState.is_auto_process:
        # Ada proses MANUAL lain → tidak boleh diinterrupt
        return False

    # Ada proses AUTO → kirim sinyal cancel
    logger.info("⚡ Manual interrupt: mengirim sinyal cancel ke auto-process...")
    BotState.cancel_requested = True

    if notify_msg:
        await safe_edit(notify_msg,
            f"⚡ **Menghentikan auto-process...**\n"
            f"🎬 Menghentikan: **{BotState.current_drama_title}**\n"
            f"⏳ Tunggu sebentar..."
        )

    # Tunggu proses auto berhenti (max 15 detik)
    for _ in range(30):
        if not BotState.is_processing:
            break
        await asyncio.sleep(0.5)

    BotState.cancel_requested = False
    logger.info("✅ Auto-process telah dihentikan. Manual bisa mulai.")
    return True

def get_panel_buttons():
    if BotState.manual_override:
        s = "🔧 MANUAL MODE"
    elif BotState.is_auto_running:
        s = "🟢 AUTO RUNNING"
    else:
        s = "🔴 AUTO STOPPED"
    return [
        [Button.inline("▶️ Start Auto", b"start_auto"),
         Button.inline("⏹ Stop Auto", b"stop_auto")],
        [Button.inline(f"📊 Status: {s}", b"status")]
    ]

# ─────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern='/dramawave start'))
async def cmd_start(event):
    if not is_admin(event.sender_id): return
    # Kirim 1 pesan → langsung isi konten (tidak perlu edit lagi karena statis)
    await event.reply(
        "🎬 **DramaWave Bot — Aktif!**\n\n"
        "**📋 Perintah Baru:**\n"
        "• `/dramawave cari {judul}` — Cari drama by judul\n"
        "• `/dramawave download {ID}` — Download by ID\n"
        "• `/dramawave status` — Cek proses aktif\n"
        "• `/dramawave panel` — Control panel\n"
        "• `/dramawave update` — Update dari Git\n\n"
        "⚡ **Prioritas Manual:**\n"
        "_Perintah `/dramawave cari` & `/dramawave download` selalu diutamakan._\n"
        f"👥 Admin: `{ADMIN_IDS}`"
    )


@client.on(events.NewMessage(pattern='/dramawave panel'))
async def cmd_panel(event):
    if not is_admin(event.sender_id): return
    await event.reply("🎛 **DramaWave Control Panel**", buttons=get_panel_buttons())


@client.on(events.NewMessage(pattern='/dramawave status'))
async def cmd_status(event):
    if not is_admin(event.sender_id): return
    
    if not BotState.is_processing:
        mode = "🟢 Auto Running" if BotState.is_auto_running else "🔴 Auto Stopped"
        await event.reply(f"💤 **Tidak ada proses aktif**\n📡 Mode: {mode}")
    else:
        src = "🤖 Auto-mode" if BotState.is_auto_process \
              else f"👤 **{BotState.triggered_by_name}** (`{BotState.triggered_by_id}`)"
        
        status_text = (
            f"⚙️ **Proses Aktif**\n"
            f"🎬 Drama: **{BotState.current_drama_title}**\n"
            f"🆔 ID: `{BotState.current_drama_id}`\n"
            f"📋 Tahap: **{BotState.current_stage}**\n"
            f"{build_bar(BotState.current_progress)}\n"
            f"🙋 Dipicu: {src}\n\n"
            f"⏳ _Pesan ini akan terupdate otomatis..._"
        )
        msg = await event.reply(status_text)
        await register_live_msg(msg)


@client.on(events.NewMessage(pattern='/dramawave update'))
async def cmd_update(event):
    if not is_admin(event.sender_id): return
    import subprocess, sys
    msg = await event.reply("🔄 **Memulai update...**")
    try:
        await safe_edit(msg, "🔄 Membersihkan cache...")
        if os.path.exists("__pycache__"):
            shutil.rmtree("__pycache__", ignore_errors=True)
        await safe_edit(msg, "🔄 `git stash`...")
        subprocess.run(["git", "stash"], capture_output=True)
        await safe_edit(msg, "🔄 `git pull`...")
        result = subprocess.run(["git", "pull"], capture_output=True, text=True)
        output = result.stdout + result.stderr
        subprocess.run(["git", "stash", "pop"], capture_output=True)
        if "Already up to date" in output:
            await safe_edit(msg, "✅ **Bot sudah versi terbaru.**")
            return
        await safe_edit(msg,
            f"✅ **Update Berhasil!**\n\n```\n{output[:400]}\n```\n"
            f"🔄 _Restart dalam 2 detik..._"
        )
        await asyncio.sleep(2)
        os.execl(sys.executable, sys.executable, *sys.argv)
    except Exception as e:
        await safe_edit(msg, f"❌ **Gagal update:** `{e}`")


# ── /dramawave cari {judul} ─────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r'/dramawave cari (.+)'))
async def cmd_search(event):
    if not is_admin(event.sender_id): return
    query = event.pattern_match.group(1).strip()
    # Pastikan membalas di topik yang sama
    msg   = await event.reply(f"🔍 **Mencari:** `{query}`...")

    results = []
    # ── 1. Coba Direct ID Lookup ──────────────────────────────────────────
    # Jika query terlihat seperti ID (tidak ada spasi, alphanumeric)
    if not " " in query:
        direct = await get_drama_detail(query)
        if direct:
            results.append(direct)

    # ── 2. Fuzzy Search ──────────────────────────────────────────────────
    search_res = await search_drama(query)
    
    # Gabungkan dan hilangkan duplikat berdasarkan ID
    seen_ids = set()
    if results:
        # Tambahkan ID dari direct lookup ke set 'seen'
        d_id = results[0].get("playlet_id") or results[0].get("id") or results[0].get("key")
        if d_id: seen_ids.add(str(d_id))

    for item in search_res:
        item_id = str(item.get("playlet_id") or item.get("id") or item.get("key"))
        if item_id not in seen_ids:
            results.append(item)
            seen_ids.add(item_id)

    if not results:
        await safe_edit(msg, f"❌ Tidak ditemukan hasil untuk: **{query}**")
        return

    buttons = []
    for d in results[:12]:
        title    = d.get("name") or d.get("title") or "Unknown"
        drama_id = d.get("playlet_id") or d.get("id") or d.get("key")
        if drama_id:
            # Tampilkan ID di tombol agar admin tahu mana yang benar
            btn_text = f"🎬 {title[:30]} [{drama_id}]"
            buttons.append([Button.inline(btn_text, data=f"manual_{drama_id}")])

    await safe_edit(msg,
        f"✅ **{len(results)} hasil** untuk: `{query}`\n"
        f"📌 Pilih drama yang sesuai (ID terlampir):",
        buttons=buttons
    )


# ── /dramawave download {ID} ────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r'/dramawave download ([\w-]+)'))
async def cmd_download(event):
    if not is_admin(event.sender_id): return

    drama_id     = event.pattern_match.group(1).strip()
    display_name = await get_display_name(event.sender_id)
    reply_to     = get_reply_to_id(event)
    
    msg          = await event.reply(
        f"🔧 **Mode Manual**\n"
        f"👤 Admin: **{display_name}**\n"
        f"🆔 Drama ID: `{drama_id}`\n"
        f"⏳ Memeriksa status proses..."
    )
    
    # ... rest of the logic ...
    # (Saya akan mengupdate pemanggilan process_drama_full di chunk berikutnya)

    # ── Jika ada proses manual lain → tolak, tapi beri live update ───────
    if BotState.is_processing and not BotState.is_auto_process:
        src = f"👤 **{BotState.triggered_by_name}** (`{BotState.triggered_by_id}`)"
        await safe_edit(msg,
            f"⚠️ **Admin lain sedang memproses!**\n"
            f"🎬 Drama: **{BotState.current_drama_title}**\n"
            f"📋 Tahap: {BotState.current_stage}\n"
            f"{build_bar(BotState.current_progress)}\n"
            f"🙋 Dipicu oleh: {src}\n\n"
            f"⏳ _Pesan ini akan terupdate otomatis..._"
        )
        await register_live_msg(msg)
        return

    # ── Jika auto sedang jalan → interrupt ───────────────────────────────
    if BotState.is_processing and BotState.is_auto_process:
        interrupted = await interrupt_auto_if_running(msg)
        if not interrupted:
            return  # Seharusnya tidak terjadi, tapi aman

    await safe_edit(msg,
        f"🔧 **Mode Manual Aktif** _(Prioritas Tinggi)_\n"
        f"👤 Admin: **{display_name}** (`{event.sender_id}`)\n"
        f"🆔 Drama ID: `{drama_id}`\n"
        f"⏸ Auto-mode di-pause...\n"
        f"🔍 Mengambil detail drama..."
    )

    await broadcast_admins(
        f"⚡ **Manual Override!**\n"
        f"👤 Admin **{display_name}** memulai download manual (auto dihentikan).\n"
        f"🆔 ID: `{drama_id}`",
        exclude_id=event.sender_id
    )

    enter_manual_mode(event.sender_id, display_name)
    await register_live_msg(msg)
    try:
        # Gunakan TOPIC_ID dari .env jika ada, 
        # kecuali perintah datang dari topik/chat lain
        current_thread = reply_to if reply_to else TOPIC_ID

        success = await process_drama_full(
            drama_id, AUTO_CHANNEL,
            status_msg=msg,
            thread_id=current_thread,
            triggered_by_id=event.sender_id,
            triggered_by_name=display_name
        )
        if success:
            processed_ids.add(str(drama_id))
            save_processed(processed_ids)
    finally:
        exit_manual_mode()
        resume = "🟢 Kembali aktif" if BotState.is_auto_running else "🔴 Tetap non-aktif"
        await broadcast_admins(
            f"✅ **Download manual selesai!**\n"
            f"👤 Oleh: **{display_name}** (`{event.sender_id}`)\n"
            f"🆔 Drama: `{drama_id}`\n"
            f"🔄 Auto-mode: {resume}",
            exclude_id=event.sender_id
        )
        await safe_edit(msg,
            f"✅ **Selesai!**\n"
            f"🎬 Drama `{drama_id}` berhasil diproses.\n"
            f"🔄 Auto-mode: {resume}"
        )


# ── Callback Handler ──────────────────────────────────────────────────────────
@client.on(events.CallbackQuery())
async def on_callback(event):
    if not is_admin(event.sender_id): return
    data = event.data.decode()

    if data == "start_auto":
        BotState.is_auto_running = True
        BotState.manual_override = False
        await event.answer("✅ Auto-mode aktif!")
        await event.edit("🎛 **DramaWave Control Panel**", buttons=get_panel_buttons())

    elif data == "stop_auto":
        BotState.is_auto_running = False
        await event.answer("⏹ Auto-mode dihentikan.")
        await event.edit("🎛 **DramaWave Control Panel**", buttons=get_panel_buttons())

    elif data == "status":
        if BotState.is_processing:
            src = "🤖 Auto" if BotState.is_auto_process else f"👤 {BotState.triggered_by_name}"
            txt = (f"⚙️ {BotState.current_drama_title[:25]}\n"
                   f"{BotState.current_stage} {BotState.current_progress}%\n{src}")
        elif BotState.is_auto_running:
            txt = "🟢 Auto-mode aktif"
        else:
            txt = "🔴 Auto-mode non-aktif"
        await event.answer(txt, alert=True)

    elif data.startswith("manual_"):
        drama_id     = data.replace("manual_", "")
        display_name = await get_display_name(event.sender_id)

        # ── Jika manual lain sedang jalan → beri status live ─────────────
        if BotState.is_processing and not BotState.is_auto_process:
            src = f"👤 **{BotState.triggered_by_name}**"
            await event.edit(
                f"⚠️ **Admin lain sedang memproses!**\n"
                f"🎬 Drama: **{BotState.current_drama_title}**\n"
                f"📋 Tahap: {BotState.current_stage}\n"
                f"{build_bar(BotState.current_progress)}\n"
                f"🙋 Dipicu oleh: {src}\n\n"
                f"⏳ _Pesan ini akan terupdate otomatis..._"
            )
            await register_live_msg(await event.get_message())
            return

        await event.answer("⚡ Mengambil alih dari auto-mode..." if BotState.is_processing else "🚀 Memulai proses manual...")

        # Edit pesan pencarian → jadi status pipeline
        await event.edit(
            f"⚡ **Manual Override Aktif** _(Prioritas Tinggi)_\n"
            f"👤 Admin: **{display_name}** (`{event.sender_id}`)\n"
            f"🆔 ID: `{drama_id}`\n"
            f"⏳ Menghentikan auto jika sedang berjalan..."
        )
        status_msg = await event.get_message()

        # ── Interrupt auto jika sedang berjalan ───────────────────────────
        if BotState.is_processing and BotState.is_auto_process:
            await interrupt_auto_if_running(status_msg)

        await safe_edit(status_msg,
            f"🔧 **Mode Manual Aktif** _(Prioritas Tinggi)_\n"
            f"👤 Admin: **{display_name}** (`{event.sender_id}`)\n"
            f"🆔 ID: `{drama_id}`\n"
            f"⏸ Auto-mode di-pause...\n"
            f"🔍 Mengambil detail drama..."
        )

        await broadcast_admins(
            f"⚡ **Manual Override!**\n"
            f"👤 Admin **{display_name}** memulai download manual (auto dihentikan).\n"
            f"🆔 ID: `{drama_id}`",
            exclude_id=event.sender_id
        )

        enter_manual_mode(event.sender_id, display_name)
        await register_live_msg(status_msg)
        try:
            success = await process_drama_full(
                drama_id, AUTO_CHANNEL,
                status_msg=status_msg,
                thread_id=TOPIC_ID,
                triggered_by_id=event.sender_id,
                triggered_by_name=display_name
            )
            if success:
                processed_ids.add(str(drama_id))
                save_processed(processed_ids)
        finally:
            exit_manual_mode()
            resume = "🟢 Kembali aktif" if BotState.is_auto_running else "🔴 Tetap non-aktif"
            await broadcast_admins(
                f"✅ **Proses manual selesai!**\n"
                f"👤 Oleh: **{display_name}** (`{event.sender_id}`)\n"
                f"🆔 Drama: `{drama_id}`\n"
                f"🔄 Auto-mode: {resume}",
                exclude_id=event.sender_id
            )
            await safe_edit(status_msg,
                f"✅ **Selesai!**\n"
                f"🎬 Drama `{drama_id}` berhasil diproses.\n"
                f"🔄 Auto-mode: {resume}"
            )

    elif data.startswith("dl_"):
        drama_id     = data.replace("dl_", "")
        display_name = await get_display_name(event.sender_id)
        if BotState.is_processing and not BotState.is_auto_process:
            await event.answer("⚠️ Admin lain sedang memproses.", alert=True)
            return
        await event.answer("🚀 Memulai...")
        await event.edit(f"🔧 **Manual** | `{drama_id}`\n⏳ Memproses...")
        status_msg = await event.get_message()
        if BotState.is_processing and BotState.is_auto_process:
            await interrupt_auto_if_running(status_msg)
        enter_manual_mode(event.sender_id, display_name)
        await register_live_msg(status_msg)
        try:
            await process_drama_full(drama_id, AUTO_CHANNEL, status_msg=status_msg,
                                     thread_id=TOPIC_ID, triggered_by_id=event.sender_id,
                                     triggered_by_name=display_name)
        finally:
            exit_manual_mode()


# ─────────────────────────────────────────────────────────────────────────────
# CORE PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

async def process_drama_full(
    drama_id, chat_id,
    status_msg=None, thread_id=None,
    triggered_by_id: int = 0,
    triggered_by_name: str = "Auto",
    is_auto: bool = False,
    extra_edit_fn=None    # async fn(text) — untuk edit semua pesan admin sekaligus (auto mode)
):
    """
    Pipeline: Fetch → Download → Hardsub → Merge → Upload.
    Satu pesan (status_msg) di-edit di setiap tahap.
    MEMERIKSA BotState.cancel_requested di setiap checkpoint —
    jika True (karena manual override masuk), proses berhenti bersih.
    """

    def is_cancelled() -> bool:
        return BotState.cancel_requested

    async def edit(text: str, buttons=None):
        if is_cancelled():
            return
        # Update semua pesan yang terdaftar (Live Tracking)
        await update_all_live_msgs(text, buttons=buttons)
        # Jika ada extra_edit_fn (untuk internal loop auto-mode)
        if extra_edit_fn and is_auto:
            try:
                await extra_edit_fn(text)
            except Exception as e:
                logger.warning(f"extra_edit_fn error: {e}")

    last_pct = [-1]

    async def update_stage(stage: str, pct: int, force: bool = False):
        BotState.current_stage    = stage
        BotState.current_progress = pct
        
        # Broadcast hanya pesan Mulai dan Selesai ke admin pasif (opsional)
        # Agar tidak dianggap spam.
        if force:
            if is_auto:
                # Untuk auto mode, kita tetap ingin admin tahu ada drama baru
                pass 
            else:
                # Beri tahu admin lain proses manual dimulai/selesai
                pass

    temp_dir = None
    try:
        # ── Checkpoint 0: Fetch ────────────────────────────────────────────
        if is_cancelled():
            logger.info(f"🛑 Pipeline {drama_id} dibatalkan (sebelum fetch)")
            return False

        await edit(f"🔍 **Mengambil detail drama...**\n🆔 `{drama_id}`")
        detail = await get_drama_detail(drama_id)
        if not detail:
            await edit(f"❌ **Drama tidak ditemukan**\n🆔 `{drama_id}`")
            return False

        title        = detail.get("name") or detail.get("title") or f"Drama_{drama_id}"
        description  = detail.get("description") or detail.get("intro") or "No description."
        poster       = detail.get("cover") or detail.get("poster") or ""
        detail_items = detail.get("items", [])
        total_eps    = len(detail_items) if detail_items else 0

        BotState.current_drama_title = title
        BotState.current_drama_id    = str(drama_id)
        BotState.triggered_by_id     = triggered_by_id
        BotState.triggered_by_name   = triggered_by_name
        BotState.is_auto_process     = is_auto

        # ── Checkpoint 1: Sebelum download ────────────────────────────────
        if is_cancelled():
            logger.info(f"🛑 Pipeline '{title}' dibatalkan (sebelum download)")
            return False

        temp_dir  = tempfile.mkdtemp(prefix=f"dw_{drama_id}_")
        video_dir = os.path.join(temp_dir, "episodes")
        os.makedirs(video_dir, exist_ok=True)

        await update_stage("📥 Download", 0, force=True)
        await edit(
            f"🎬 **{title}**\n"
            f"📥 **Download** (0/{total_eps})\n"
            f"{build_bar(0)}\n⏳ Memulai..."
        )

        semaphore = asyncio.Semaphore(5)
        completed = [0]

        async def download_task(ep_num):
            # ── Checkpoint dalam download ──────────────────────────────────
            if is_cancelled():
                return False
            async with semaphore:
                if is_cancelled():
                    return False
                if ep_num - 1 >= len(detail_items):
                    return False
                play_data = detail_items[ep_num - 1]
                video_url = (play_data.get("1080p_mp4") or
                             play_data.get("720p_mp4") or
                             play_data.get("video_url"))
                sub_list  = play_data.get("subtitle_list") or []
                sub_url   = None
                if isinstance(sub_list, list):
                    for s in sub_list:
                        lang = s.get("language") or s.get("lang") or ""
                        if lang in ["id-ID", "id", "in"]:
                            sub_url = s.get("subtitle") or s.get("vtt")
                            break
                    if not sub_url and sub_list:
                        sub_url = sub_list[0].get("subtitle") or sub_list[0].get("vtt")
                if not video_url:
                    return False
                result = await download_episode_with_subs(ep_num, video_url, sub_url, video_dir)
                completed[0] += 1
                pct = int((completed[0] / max(total_eps, 1)) * 30)
                await update_stage(f"📥 Download ({completed[0]}/{total_eps} ep)", pct)
                if not is_cancelled():
                    await edit(
                        f"🎬 **{title}**\n"
                        f"📥 **Download** ({completed[0]}/{total_eps})\n"
                        f"{build_bar(pct)}\n⏳ Sedang mengunduh..."
                    )
                return result

        dl_results = await asyncio.gather(*(download_task(i) for i in range(1, total_eps + 1)))

        # ── Checkpoint 2: Setelah download ────────────────────────────────
        if is_cancelled():
            logger.info(f"🛑 Pipeline '{title}' dibatalkan (setelah download)")
            return False

        failed = dl_results.count(False)
        await edit(
            f"🎬 **{title}**\n"
            f"📥 Download: {total_eps - failed}/{total_eps} ep\n"
            f"{build_bar(30)}\n"
            f"🔥 Memulai Hardsub & Merge..."
        )

        # ── Tahap 2: Hardsub ──────────────────────────────────────────────
        await update_stage("🔥 Hardsub & Merge", 30, force=True)

        async def merge_progress(text: str):
            if is_cancelled():
                return
            pct = BotState.current_progress
            for m in ["100%", "90%", "80%", "70%", "60%", "50%",
                      "40%", "30%", "20%", "10%", "0%"]:
                if m in text:
                    try:
                        raw = int(m.replace("%", ""))
                        pct = 30 + int(raw * 0.6)
                    except:
                        pass
                    break
            BotState.current_progress = max(30, min(90, pct))
            BotState.current_stage    = "🔥 Hardsub & Merge"
            if not is_cancelled():
                await edit(f"🎬 **{title}**\n{text}")

        # ── Checkpoint 3: Sebelum hardsub ─────────────────────────────────
        if is_cancelled():
            logger.info(f"🛑 Pipeline '{title}' dibatalkan (sebelum hardsub)")
            return False

        output_path   = os.path.join(temp_dir, f"{title}.mp4")
        merge_success = await merge_and_hardsub(video_dir, output_path, merge_progress, title)

        if is_cancelled():
            logger.info(f"🛑 Pipeline '{title}' dibatalkan (setelah hardsub)")
            return False

        if not merge_success:
            await edit(
                f"🎬 **{title}**\n"
                f"❌ **Hardsub/Merge Gagal!**"
            )
            return False

        # ── Tahap 3: Upload ───────────────────────────────────────────────
        await update_stage("📤 Upload ke Telegram", 90, force=True)
        await edit(
            f"🎬 **{title}**\n"
            f"📤 **Upload ke Telegram...**\n"
            f"{build_bar(90)}\n⏳ Sedang upload..."
        )

        upload_success = await upload_drama(
            client, chat_id, title, description, poster, output_path, thread_id=thread_id
        )

        if upload_success:
            await update_stage("✅ Selesai", 100, force=True)
            await edit(
                f"✅ **Sukses!**\n"
                f"🎬 **{title}**\n"
                f"{build_bar(100)}\n"
                f"📺 Berhasil diposting ke channel."
            )
            return True
        else:
            await edit(
                f"🎬 **{title}**\n"
                f"❌ **Upload Gagal!**"
            )
            return False

    except Exception as e:
        if not is_cancelled():
            logger.error(f"Error pipeline {drama_id}: {e}")
            await edit(f"❌ **Error:**\n`{str(e)[:300]}`")
            await broadcast_admins(
                f"❌ **Error proses drama!**\n"
                f"🎬 {BotState.current_drama_title}\n"
                f"🆔 `{drama_id}`\n`{str(e)[:200]}`"
            )
        return False
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# AUTO MODE LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def auto_mode_loop():
    logger.info("🚀 DramaWave Auto-Mode Active.")

    while True:
        # Pause jika auto off ATAU manual sedang jalan
        if not BotState.is_auto_running or BotState.manual_override:
            await asyncio.sleep(10)
            continue

        try:
            logger.info("🔍 Checking popular feed...")
            dramas = await get_popular_feed(page=1)

            for drama in dramas:
                if not BotState.is_auto_running or BotState.manual_override:
                    break

                drama_id = drama.get("playlet_id") or drama.get("id") or drama.get("key")
                if not drama_id:
                    continue

                drama_id = str(drama_id)
                if drama_id not in processed_ids:
                    title = drama.get("name") or drama.get("title") or "Unknown"
                    logger.info(f"✨ Auto: {title} ({drama_id})")

                    # Simpan semua pesan admin untuk diupdate
                    admin_msgs: list = []
                    init_text = (
                        f"🆕 **Auto-Detect Drama Baru!**\n"
                        f"🎬 {title}\n🆔 `{drama_id}`\n⏳ Memproses...\n"
                        f"⏳ _Pesan ini akan terupdate otomatis bagi admin yang memantau._"
                    )
                    for aid in ADMIN_IDS:
                        try:
                            m = await client.send_message(aid, init_text)
                            admin_msgs.append(m)
                            # Auto-register semua admin saat auto-detect
                            await register_live_msg(m)
                        except: pass

                    # status_msg = pesan admin pertama (yang akan di-edit oleh pipeline)
                    status_msg = admin_msgs[0] if admin_msgs else None

                    # Wrapper edit: edit SEMUA pesan admin secara bersamaan
                    async def edit_all_admin_msgs(text: str):
                        for m in admin_msgs:
                            await safe_edit(m, text)

                    BotState.is_processing   = True
                    BotState.is_auto_process = True
                    success = await process_drama_full(
                        drama_id, AUTO_CHANNEL,
                        status_msg=status_msg,
                        thread_id=TOPIC_ID,
                        triggered_by_name="Auto-mode",
                        is_auto=True,
                        extra_edit_fn=edit_all_admin_msgs
                    )
                    BotState.is_processing   = False
                    BotState.is_auto_process = False
                    BotState.current_progress = 0
                    BotState.current_stage    = ""
                    BotState.cancel_requested = False

                    if success:
                        processed_ids.add(drama_id)
                        save_processed(processed_ids)
                        await broadcast_admins(f"✅ **Auto Post Sukses:** **{title}**")
                    else:
                        if BotState.cancel_requested:
                            logger.info(f"⚡ Auto '{title}' dihentikan oleh manual override.")
                        else:
                            logger.error(f"Auto gagal: {title}")

                    await asyncio.sleep(15)

            await asyncio.sleep(3600)

        except Exception as e:
            logger.error(f"Error auto_mode: {e}")
            await asyncio.sleep(300)


if __name__ == '__main__':
    logger.info("🚀 Bot started — sesi lama sudah dibersihkan.")
    logger.info(f"👥 Admin terdaftar: {ADMIN_IDS}")
    client.loop.create_task(auto_mode_loop())
    client.run_until_disconnected()
