import os
import html
import secrets
import asyncio
import logging
from io import BytesIO
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request, Response, status
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

# 📢 MANDATORY CHANNELS (Optional)
REQUIRED_CHANNELS = [] 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db_pool: asyncpg.Pool | None = None
ptb_app: Application | None = None


# ==============================================================================
# 🗄️ DATABASE (Persist, won't delete on code updates)
# ==============================================================================
async def init_db():
    global db_pool
    if not db_pool:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4, statement_cache_size=0)
    
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
            -- Permanent Database-backed Merge Queue for 140k+ lines support
            CREATE TABLE IF NOT EXISTS merge_queue (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                file_content TEXT NOT NULL,
                line_count INT NOT NULL,
                file_name TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_file_code ON files(file_code);
            CREATE INDEX IF NOT EXISTS idx_merge_admin ON merge_queue(admin_id);
        """)

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

    # Deep-link download
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
            "📁 Send me your `.txt` files to add them to your permanent merge queue (Supports 140k+ lines).\n"
            "⚙️ Use /admin for control panel."
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
# 📂 ROBUST 140K+ FILE MERGING QUEUE (Database-backed)
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

    status_msg = await msg.reply_text("⏳ Downloading and analyzing file lines...")

    try:
        file_obj = await context.bot.get_file(document.file_id)
        file_bytes = await file_obj.download_as_bytearray()
        
        # High performance line counting and decoding for huge files (140k+)
        text_content = file_bytes.decode('utf-8', errors='ignore')
        line_count = text_content.count('\n') + (1 if text_content and not text_content.endswith('\n') else 0)
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        await status_msg.edit_text("❌ Error reading the file. Make sure it's a valid text file.")
        return

    # Save to Database merge queue (Safe against code updates and restarts)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO merge_queue (admin_id, file_content, line_count, file_name)
            VALUES ($1, $2, $3, $4)
        """, user.id, text_content, line_count, document.file_name or "file.txt")

        total_files = await conn.fetchval("SELECT COUNT(*) FROM merge_queue WHERE admin_id = $1", user.id)
        total_lines = await conn.fetchval("SELECT COALESCE(SUM(line_count), 0) FROM merge_queue WHERE admin_id = $1", user.id)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Merge & Create Link", callback_data="merge_done")],
        [InlineKeyboardButton("🗑️ Clear Queue", callback_data="merge_clear")]
    ])

    await status_msg.edit_text(
        f"📄 <b>File Added to Permanent Queue!</b>\n"
        f"➕ File Lines: <code>{line_count}</code>\n"
        f"📦 Total Files in Queue: <b>{total_files}</b>\n"
        f"📊 Total Combined Lines: <b>{total_lines:,}</b>\n\n"
        f"Send more files or click <b>Merge & Create Link</b> when ready:",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
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
        return

    if data == "merge_done":
        async with db_pool.acquire() as conn:
            total_files = await conn.fetchval("SELECT COUNT(*) FROM merge_queue WHERE admin_id = $1", user_id)
            total_lines = await conn.fetchval("SELECT COALESCE(SUM(line_count), 0) FROM merge_queue WHERE admin_id = $1", user_id)

        if not total_files or total_files == 0:
            await query.edit_message_text("⚠️ Your queue is empty!")
            return

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔥 HOTMAIL", callback_data="type_HOTMAIL"),
                InlineKeyboardButton("⚡ MIX", callback_data="type_MIX")
            ],
            [
                InlineKeyboardButton("🌐 YANDEX", callback_data="type_YANDEX"),
                InlineKeyboardButton("📦 LOGS", callback_data="type_LOGS")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="type_cancel")]
        ])

        await query.edit_message_text(
            f"🔗 <b>Ready to Merge {total_files} Files!</b>\n"
            f"📊 Total Lines: <b>{total_lines:,}</b>\n\n"
            f"Select the final log type:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )


async def file_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    data = query.data
    if data == "type_cancel":
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM merge_queue WHERE admin_id = $1", user_id)
        await query.edit_message_text("❌ Operation cancelled.")
        return

    file_type_str = data.replace("type_", "")
    
    await query.edit_message_text("⏳ Merging large files and generating final link... Please wait.")

    # Fetch all queued files for this admin from DB
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT file_content, line_count FROM merge_queue WHERE admin_id = $1", user_id)

    if not rows:
        await query.edit_message_text("❌ Queue expired or empty.")
        return

    # High performance combination using list join (Optimized for 140k+ lines)
    all_contents = [row['file_content'] for row in rows]
    combined_content = "\n".join(all_contents)
    total_lines = sum(row['line_count'] for row in rows)

    formatted_name = f"@RatLogsCloud-{total_lines}-{file_type_str}.txt"

    file_bytes = BytesIO(combined_content.encode('utf-8'))
    file_bytes.name = formatted_name

    try:
        sent_msg = await context.bot.send_document(
            chat_id=user_id,
            document=file_bytes,
            caption=f"Merged File: {formatted_name}"
        )
        telegram_file_id = sent_msg.document.file_id
        file_size = sent_msg.document.file_size or len(combined_content)
    except Exception as e:
        logger.error(f"Failed to send merged file: {e}")
        await query.edit_message_text("❌ Failed to generate combined document.")
        return

    file_code = secrets.token_urlsafe(6)

    async with db_pool.acquire() as conn:
        # Save to permanent files table
        await conn.execute("""
            INSERT INTO files (file_code, user_id, file_id, file_type, file_name, file_size, download_count, caption, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, 0, $7, 1)
        """, file_code, user_id, telegram_file_id, 'document', formatted_name, file_size, formatted_name)

        # Clear merge queue after success
        await conn.execute("DELETE FROM merge_queue WHERE admin_id = $1", user_id)

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

    await query.edit_message_text(resp, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


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
# 🌐 FASTAPI & LIFESPAN
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb_app
    await init_db()

    ptb_app = Application.builder().token(BOT_TOKEN).updater(None).build()

    ptb_app.add_handler(CommandHandler("start", start_handler))
    ptb_app.add_handler(CommandHandler("admin", admin_panel))
    ptb_app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    ptb_app.add_handler(CallbackQueryHandler(admin_callback))
    
    ptb_app.add_handler(CallbackQueryHandler(merge_callbacks, pattern="^merge_"))
    ptb_app.add_handler(CallbackQueryHandler(file_type_selected, pattern="^type_"))
    
    ptb_app.add_handler(MessageHandler(filters.Document.ALL & filters.User(user_id=ADMIN_IDS), handle_incoming_file))

    await ptb_app.initialize()
    await ptb_app.start()
    yield
    await ptb_app.stop()
    await ptb_app.shutdown()
    if db_pool:
        await db_pool.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def home():
    return {"status": "active", "bot": "Log Share & 140k Merge Bot Running 24/7"}

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@app.get("/api/set_webhook")
async def set_webhook(request: Request):
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    webhook_url = f"https://{host}/api/webhook"
    success = await ptb_app.bot.set_webhook(url=webhook_url)
    return {"status": "success", "webhook_url": webhook_url}
