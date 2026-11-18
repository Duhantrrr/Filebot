import os
import html
import secrets
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO, StringIO
import asyncpg
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==============================================================================
# ⚙️ SETTINGS
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9183664352:AAHKDCWH0vttkhTzcqUysKXULwtJhx2UGmQ")

admin_ids_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_env.split(",") if x.strip().isdigit()] if admin_ids_env else [7231282775]

DEFAULT_DB_URL = "postgresql://neondb_owner:npg_BZpD1wXYWQ7u@ep-sweet-meadow-bd0j2mpn-pooler.c-8.us-east-2.aws.neon.tech/neondb?sslmode=require"
RAW_DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
DATABASE_URL = RAW_DATABASE_URL.replace("&channel_binding=require", "").replace("?channel_binding=require", "")

REQUIRED_CHANNELS = [] 

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db_pool: asyncpg.Pool | None = None


# ==============================================================================
# 🌐 RENDER PORT KANDIRMASI
# ==============================================================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running 24/7!")

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    logger.info(f"🌐 Native dummy web server running on port {port}")
    server.serve_forever()


# ==============================================================================
# 🗄️ DATABASE
# ==============================================================================
async def init_db():
    global db_pool
    if not db_pool:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS files (
                id SERIAL PRIMARY KEY,
                file_code TEXT UNIQUE NOT NULL,
                user_id BIGINT NOT NULL,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_name TEXT,
                file_size BIGINT DEFAULT 0,
                download_count INT DEFAULT 0,
                caption TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS merge_queue (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                file_content TEXT NOT NULL,
                line_count INT NOT NULL,
                file_name TEXT
            );
        """)
        
        await conn.execute("""
            ALTER TABLE files ADD COLUMN IF NOT EXISTS download_count INT DEFAULT 0;
            ALTER TABLE files ADD COLUMN IF NOT EXISTS is_active INT DEFAULT 1;
        """)
        
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_code ON files(file_code);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_merge_admin ON merge_queue(admin_id);")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def check_channels(user_id: int, bot) -> list:
    if not REQUIRED_CHANNELS:
        return []
    not_joined = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(channel)
        except Exception:
            pass
    return not_joined


# ==============================================================================
# 🤖 BOT HANDLERS
# ==============================================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot = context.bot

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET username = $2, first_name = $3
        """, user.id, user.username, user.first_name)

    not_joined = await check_channels(user.id, bot)
    if not_joined:
        buttons = [[InlineKeyboardButton(f"📢 Join Channel", url=f"https://t.me/{ch.replace('@', '')}")] for ch in not_joined]
        buttons.append([InlineKeyboardButton("🔄 Verify / Start", callback_data="check_sub")])
        await update.message.reply_text(
            "⚠️ <b>You must join our channels to use this bot:</b>",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML
        )
        return

    # Deep-link indirmesi
    if context.args and len(context.args) > 0 and context.args[0].startswith("file_"):
        file_code = context.args[0].replace("file_", "")
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE files SET download_count = download_count + 1 
                WHERE file_code = $1 AND is_active = 1 
                RETURNING *
            """, file_code)

        if not row:
            await update.message.reply_text("❌ <b>File not found or deleted.</b>", parse_mode=ParseMode.HTML)
            return

        file_id = row["file_id"]
        caption = row["caption"] or ""
        file_type = row["file_type"]
        downloads = row["download_count"]

        header_info = f"📥 <b>This file has been downloaded {downloads} times!</b>\n"
        full_caption = f"{header_info}\n{caption}" if caption else header_info

        if file_type == "document":
            await update.message.reply_document(file_id, caption=full_caption, parse_mode=ParseMode.HTML)
        elif file_type == "photo":
            await update.message.reply_photo(file_id, caption=full_caption, parse_mode=ParseMode.HTML)
        elif file_type == "video":
            await update.message.reply_video(file_id, caption=full_caption, parse_mode=ParseMode.HTML)
        elif file_type == "audio":
            await update.message.reply_audio(file_id, caption=full_caption, parse_mode=ParseMode.HTML)
        elif file_type == "voice":
            await update.message.reply_voice(file_id, caption=full_caption, parse_mode=ParseMode.HTML)
        return

    if is_admin(user.id):
        text = (
            f"👑 Welcome Boss <b>{html.escape(user.first_name)}</b>!\n\n"
            "📁 Send me your `.txt` files.\n"
            "🔗 When you're ready, simply type <b>/merge</b> to combine them into <code>@CalcyCloud-[LINES].txt</code>.\n"
            "⚙️ Use /admin for statistics."
        )
    else:
        text = f"👋 Hello <b>{html.escape(user.first_name)}</b>! You can download files using the shared links."
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    not_joined = await check_channels(user.id, context.bot)
    
    if not_joined:
        await query.answer("❌ You haven't joined all channels yet!", show_alert=True)
    else:
        await query.message.delete()
        await context.bot.send_message(chat_id=user.id, text="✅ Verified! Please send /start again.")


# ==============================================================================
# 📂 DOSYA ALMA & DOĞRUDAN /merge KOMUTU (BUTONSUZ)
# ==============================================================================

async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    msg = update.message
    document = msg.document

    if not document:
        await msg.reply_text("⚠️ Please send a valid `.txt` file.")
        return

    status_msg = await msg.reply_text("⏳ Analyzing file lines...")

    try:
        file_obj = await context.bot.get_file(document.file_id)
        file_bytes = await file_obj.download_as_bytearray()
        
        text_content = file_bytes.decode('utf-8', errors='ignore')
        line_count = text_content.count('\n') + (1 if text_content and not text_content.endswith('\n') else 0)
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        await status_msg.edit_text("❌ Error reading the file.")
        return

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO merge_queue (admin_id, file_content, line_count, file_name)
            VALUES ($1, $2, $3, $4)
        """, user.id, text_content, line_count, document.file_name or "file.txt")

        total_files = await conn.fetchval("SELECT COUNT(*) FROM merge_queue WHERE admin_id = $1", user.id)
        total_lines = await conn.fetchval("SELECT COALESCE(SUM(line_count), 0) FROM merge_queue WHERE admin_id = $1", user.id)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Clear Queue", callback_data="merge_clear")]
    ])

    await status_msg.edit_text(
        f"✅ <b>File Added to Queue!</b>\n"
        f"➕ Lines: <code>{line_count:,}</code>\n"
        f"📦 Total Files in Queue: <b>{total_files}</b>\n"
        f"📊 Total Combined Lines: <b>{total_lines:,}</b>\n\n"
        f"👉 Send more files or type <b>/merge</b> to combine them instantly.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )


async def merge_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcı /merge yazdığında HİÇBİR ŞEY SORMADAN direkt birleştirir."""
    user = update.effective_user
    if not is_admin(user.id):
        return

    status_msg = await update.message.reply_text("⏳ Merging all queued files into @CalcyCloud... Please wait.")

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT file_content FROM merge_queue WHERE admin_id = $1", user.id)

    if not rows:
        await status_msg.edit_text("⚠️ Your merge queue is empty! Please send some `.txt` files first.")
        return

    try:
        output = StringIO()
        for row in rows:
            output.write(row['file_content'] + "\n")
        
        combined_text = output.getvalue()
        output.close()

        total_lines = combined_text.count('\n')
        # İstenen yeni dosya formatı:
        formatted_name = f"@CalcyCloud-{total_lines}.txt"

        raw_bytes = combined_text.encode('utf-8')
        file_bytes = BytesIO(raw_bytes)
        file_bytes.name = formatted_name
        file_bytes.seek(0)

        # Telegram'a gönder
        sent_msg = await context.bot.send_document(
            chat_id=user.id,
            document=file_bytes,
            filename=formatted_name,
            caption=f"📁 <b>File:</b> <code>{formatted_name}</code>\n📊 <b>Total Lines:</b> <code>{total_lines:,}</code>",
            parse_mode=ParseMode.HTML,
            read_timeout=60,
            write_timeout=60
        )
        
        telegram_file_id = sent_msg.document.file_id
        file_size = sent_msg.document.file_size or len(raw_bytes)

    except Exception as e:
        logger.error(f"Failed to merge/send file: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return

    file_code = secrets.token_urlsafe(6)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO files (file_code, user_id, file_id, file_type, file_name, file_size, download_count, caption, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, 0, $7, 1)
        """, file_code, user.id, telegram_file_id, 'document', formatted_name, file_size, formatted_name)

        # Kuyruğu sıfırla
        await conn.execute("DELETE FROM merge_queue WHERE admin_id = $1", user.id)

    bot_username = context.bot.username
    download_url = f"https://t.me/{bot_username}?start=file_{file_code}"
    size_mb = round(file_size / (1024 * 1024), 2)

    resp = (
        "✅ <b>Files Successfully Merged & Link Created!</b>\n\n"
        f"🏷️ File Name: <code>{formatted_name}</code>\n"
        f"📊 Size: <code>{size_mb} MB</code> | Total Lines: <code>{total_lines:,}</code>\n\n"
        f"🔗 <b>Download Link:</b>\n<code>{download_url}</code>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Share on Telegram", url=f"https://t.me/share/url?url={download_url}&text=Download%20File")]
    ])

    await status_msg.delete()
    await context.bot.send_message(
        chat_id=user.id,
        text=resp,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def merge_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        return

    await query.answer()
    data = query.data

    if data == "merge_clear":
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM merge_queue WHERE admin_id = $1", user_id)
        await query.edit_message_text("🗑️ Merge queue cleared.")


# ==============================================================================
# ⚙️ ADMIN PANEL
# ==============================================================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("❌ Close", callback_data="admin_close")]
    ])
    text = f"⚙️ <b>Admin Control Panel</b>\nBoss: <b>{html.escape(user.first_name)}</b>"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if query.data == "admin_stats":
        async with db_pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_files = await conn.fetchval("SELECT COUNT(*) FROM files WHERE is_active = 1")
            total_downloads = await conn.fetchval("SELECT COALESCE(SUM(download_count), 0) FROM files")
            
        text = (
            "📊 <b>System Statistics:</b>\n\n"
            f"👤 Total Users: <b>{total_users}</b>\n"
            f"📁 Active Files: <b>{total_files}</b>\n"
            f"📥 Total Downloads: <b>{total_downloads}</b>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]])
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif query.data == "admin_back":
        await admin_panel(update, context)
    elif query.data == "admin_close":
        await query.message.delete()


# ==============================================================================
# 🚀 MAIN RUNNER
# ==============================================================================
async def main():
    await init_db()

    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("merge", merge_command_handler))
    
    application.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(merge_callbacks, pattern="^merge_"))
    
    application.add_handler(MessageHandler(filters.Document.ALL & filters.User(user_id=ADMIN_IDS), handle_incoming_file))

    await application.initialize()
    await application.start()

    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info("🚀 Webhook dropped & Bot polling started successfully!")

    await application.updater.start_polling(drop_pending_updates=True)
    
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == "__main__":
    asyncio.run(main())
