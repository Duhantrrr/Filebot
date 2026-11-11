import os
import html
import secrets
import asyncio
import logging
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
# ⚙️ ENTEGRE EDİLMİŞ BİLGİLER
# ==============================================================================

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN", "9183664352:AAHKDCWH0vttkhTzcqUysKXULwtJhx2UGmQ")

# Admin Telegram ID (Senin ID'n tanımlandı)
admin_ids_env = os.getenv("ADMIN_IDS", "")
if admin_ids_env:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_env.split(",") if x.strip().isdigit()]
else:
    ADMIN_IDS = [7231282775]

# Neon PostgreSQL Bağlantısı
DEFAULT_DB_URL = "postgresql://neondb_owner:npg_BZpD1wXYWQ7u@ep-sweet-meadow-bd0j2mpn-pooler.c-8.us-east-2.aws.neon.tech/neondb?sslmode=require"
RAW_DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB_URL)

# asyncpg'yi bozan parametreleri otomatik temizler
DATABASE_URL = RAW_DATABASE_URL.replace("&channel_binding=require", "").replace("?channel_binding=require", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global Değişkenler
db_pool: asyncpg.Pool | None = None
ptb_app: Application | None = None


# ==============================================================================
# 🗄️ VERİTABANI İŞLEMLERİ
# ==============================================================================

async def init_db():
    global db_pool
    if not db_pool:
        # statement_cache_size=0: Neon PgBouncer havuzu için zorunludur
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=4,
            statement_cache_size=0
        )
    
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
                caption TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_file_code ON files(file_code);
        """)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==============================================================================
# 🤖 BOT HANDLER'LARI
# ==============================================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Kullanıcıyı kaydet
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET username = $2, first_name = $3
        """, user.id, user.username, user.first_name)

    # 1. Özel İndirme Linkine Tıklanmışsa (/start file_ABC123)
    if context.args and len(context.args) > 0 and context.args[0].startswith("file_"):
        file_code = context.args[0].replace("file_", "")
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM files WHERE file_code = $1 AND is_active = 1", file_code)

        if not row:
            await update.message.reply_text("❌ <b>Bu dosya bulunamadı veya silinmiş.</b>", parse_mode=ParseMode.HTML)
            return

        file_id = row["file_id"]
        caption = row["caption"] or ""
        file_type = row["file_type"]

        if file_type == "document":
            await update.message.reply_document(file_id, caption=caption)
        elif file_type == "photo":
            await update.message.reply_photo(file_id, caption=caption)
        elif file_type == "video":
            await update.message.reply_video(file_id, caption=caption)
        elif file_type == "audio":
            await update.message.reply_audio(file_id, caption=caption)
        elif file_type == "voice":
            await update.message.reply_voice(file_id, caption=caption)
        return

    # 2. Normal /start Mesajı
    if is_admin(user.id):
        text = (
            f"👑 Merhaba Patron <b>{html.escape(user.first_name)}</b>!\n\n"
            "• Bana herhangi bir <b>dosya/foto/video</b> gönder, anında indirme linki oluşturayım.\n"
            "• Yönetim menüsü için: <code>/admin</code>"
        )
    else:
        text = (
            f"👋 Merhaba <b>{html.escape(user.first_name)}</b>!\n\n"
            "Bu bot üzerinden paylaşılan linklere tıklayarak dosyaları anında indirebilirsiniz.\n\n"
            "<i>(Not: Yalnızca yöneticiler dosya yükleyebilir.)</i>"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def file_receiver_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    # SADECE SEN DOSYA YÜKLEYEBİLİRSİN
    if not is_admin(user.id):
        await msg.reply_text(
            "⛔ <b>Yetkisiz İşlem!</b>\nBu botta sadece yönetici dosya yükleyebilir ve link oluşturabilir.",
            parse_mode=ParseMode.HTML
        )
        return

    file_id = None
    file_type = None
    file_name = "Dosya"
    file_size = 0
    caption = msg.caption or None

    if msg.document:
        file_id, file_type, file_name, file_size = msg.document.file_id, "document", msg.document.file_name or "Belge", msg.document.file_size or 0
    elif msg.video:
        file_id, file_type, file_name, file_size = msg.video.file_id, "video", msg.video.file_name or "Video", msg.video.file_size or 0
    elif msg.photo:
        photo = msg.photo[-1]
        file_id, file_type, file_name, file_size = photo.file_id, "photo", "Fotoğraf", photo.file_size or 0
    elif msg.audio:
        file_id, file_type, file_name, file_size = msg.audio.file_id, "audio", msg.audio.file_name or "Ses", msg.audio.file_size or 0
    elif msg.voice:
        file_id, file_type, file_name, file_size = msg.voice.file_id, "voice", "Sesli Mesaj", msg.voice.file_size or 0

    file_code = secrets.token_urlsafe(6)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO files (file_code, user_id, file_id, file_type, file_name, file_size, caption, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 1)
        """, file_code, user.id, file_id, file_type, file_name, file_size, caption)

    bot_username = context.bot.username
    download_url = f"https://t.me/{bot_username}?start=file_{file_code}"
    size_mb = round(file_size / (1024 * 1024), 2)

    resp = (
        "✅ <b>Dosya Kaydedildi ve Link Üretildi!</b>\n\n"
        f"📁 <b>Dosya Adı:</b> <code>{html.escape(file_name)}</code>\n"
        f"📊 <b>Boyut:</b> <code>{size_mb} MB</code>\n\n"
        f"🔗 <b>İndirme Linki:</b>\n<code>{download_url}</code>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Telegram'da Paylaş", url=f"https://t.me/share/url?url={download_url}&text=Dosyayı%20İndir")]
    ])
    await msg.reply_text(resp, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ Yetkiniz yok.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 İstatistikler", callback_data="admin_stats")],
        [InlineKeyboardButton("📁 Son Dosyalar", callback_data="admin_files")],
        [InlineKeyboardButton("❌ Paneli Kapat", callback_data="admin_close")]
    ])
    text = f"⚙️ <b>Yönetici Paneli</b>\nYetkili: <b>{html.escape(user.first_name)}</b>"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    data = query.data

    if data == "admin_stats":
        async with db_pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_files = await conn.fetchval("SELECT COUNT(*) FROM files WHERE is_active = 1")
            total_bytes = await conn.fetchval("SELECT COALESCE(SUM(file_size), 0) FROM files WHERE is_active = 1")
            
        mb = round(total_bytes / (1024 * 1024), 2)
        text = (
            "📊 <b>Sistem İstatistikleri:</b>\n\n"
            f"👤 Botu Kullanan Kişi: <b>{total_users}</b>\n"
            f"📁 Aktif Dosyalar: <b>{total_files}</b>\n"
            f"💾 Toplam Dosya Boyutu: <b>{mb} MB</b>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Geri", callback_data="admin_back")]])
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    elif data == "admin_files":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT file_code, file_name, file_size FROM files WHERE is_active = 1 ORDER BY id DESC LIMIT 5")

        if not rows:
            text = "📁 Henüz aktif yüklenmiş bir dosya yok."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Geri", callback_data="admin_back")]])
            await query.edit_message_text(text, reply_markup=kb)
            return

        text = "📁 <b>Son 5 Dosya:</b>\n\n"
        buttons = []
        for r in rows:
            mb = round(r['file_size'] / (1024 * 1024), 2)
            text += f"• <code>{html.escape(r['file_name'])}</code> ({mb} MB)\n"
            buttons.append([InlineKeyboardButton(f"🗑️ Sil: {r['file_name'][:15]}...", callback_data=f"del_{r['file_code']}")])

        buttons.append([InlineKeyboardButton("⬅️ Geri", callback_data="admin_back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

    elif data.startswith("del_"):
        file_code = data.replace("del_", "")
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE files SET is_active = 0 WHERE file_code = $1", file_code)
        await query.answer("Dosya silindi (artık indirilemez).", show_alert=True)
        await admin_panel(update, context)

    elif data == "admin_back":
        await admin_panel(update, context)

    elif data == "admin_close":
        await query.message.delete()


# ==============================================================================
# 🌐 FASTAPI & SERVERLESS WEBHOOK
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb_app
    await init_db()

    ptb_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    ptb_app.add_handler(CommandHandler("start", start_handler))
    ptb_app.add_handler(CommandHandler("admin", admin_panel))
    ptb_app.add_handler(CallbackQueryHandler(admin_callback))
    ptb_app.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.VIDEO | filters.PHOTO | filters.AUDIO | filters.VOICE,
            file_receiver_handler
        )
    )

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
    return {"durum": "aktif", "bot": "Telegram File Bot 7/24 Calisiyor"}

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    """Telegram mesajları buraya fırlatır."""
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Webhook Hatası: {e}")
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@app.get("/api/set_webhook")
async def set_webhook(request: Request):
    """Vercel domainini Telegram'a bağlayan sihirli link."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    webhook_url = f"https://{host}/api/webhook"
    
    success = await ptb_app.bot.set_webhook(url=webhook_url)
    if success:
        return {"status": "basarili", "webhook_url": webhook_url, "mesaj": "Bot 7/24 calismaya basladi!"}
    return {"status": "hata", "mesaj": "Webhook baglanamadi."}
