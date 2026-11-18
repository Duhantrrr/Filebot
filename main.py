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
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==============================================================================
# 🌐 1. RENDER PORTUNU İLK SANİYEDE AÇ (Render Botu Öldüremesin)
# ==============================================================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive 24/7!")

    def log_message(self, format, *args):
        return  # Log kirliliğini engelle

def start_dummy_port():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

# Arka planda portu hemen başlatıyoruz
threading.Thread(target=start_dummy_port, daemon=True).start()

# ==============================================================================
# ⚙️ 2. AYARLAR
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9183664352:AAHKDCWH0vttkhTzcqUysKXULwtJhx2UGmQ")

ADMIN_IDS = [7231282775]
admin_ids_env = os.getenv("ADMIN_IDS", "")
if admin_ids_env:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_env.split(",") if x.strip().isdigit()]

DEFAULT_DB_URL = "postgresql://neondb_owner:npg_BZpD1wXYWQ7u@ep-sweet-meadow-bd0j2mpn-pooler.c-8.us-east-2.aws.neon.tech/neondb?sslmode=require"
RAW_DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
DATABASE_URL = RAW_DATABASE_URL.replace("&channel_binding=require", "").replace("?channel_binding=require", "")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db_pool: asyncpg.Pool | None = None


# ==============================================================================
# 🗄️ 3. VERİTABANI BAĞLANTISI (Neon Uykudaysa Bekler)
# ==============================================================================
async def get_db_pool():
    global db_pool
    if db_pool is None or db_pool._closed:
        while True:
            try:
                db_pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=5,
                    statement_cache_size=0,
                    command_timeout=60
                )
                break
            except Exception as e:
                logger.warning(f"Neon veritabanı uyanıyor... Tekrar deneniyor: {e}")
                await asyncio.sleep(3)
    return db_pool

async def init_db():
    pool = await get_db_pool()
    async with pool.acquire() as conn:
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
            ALTER TABLE files ADD COLUMN IF NOT EXISTS download_count INT DEFAULT 0;
            ALTER TABLE files ADD COLUMN IF NOT EXISTS is_active INT DEFAULT 1;
            CREATE INDEX IF NOT EXISTS idx_file_code ON files(file_code);
            CREATE INDEX IF NOT EXISTS idx_merge_admin ON merge_queue(admin_id);
        """)
    logger.info("✅ Veritabanı tabloları hazır.")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==============================================================================
# 🤖 4. BOT HANDLER'LARI
# ==============================================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pool = await get_db_pool()

    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET username = $2, first_name = $3
            """, user.id, user.username, user.first_name)
    except Exception as e:
        logger.error(f"Kullanıcı kayıt hatası: {e}")

    # İndirme linkine tıklandıysa
    if context.args and len(context.args) > 0 and context.args[0].startswith("file_"):
        file_code = context.args[0].replace("file_", "")
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE files SET download_count = download_count + 1 
                WHERE file_code = $1 AND is_active = 1 
                RETURNING *
            """, file_code)

        if not row:
            await update.message.reply_text("❌ <b>Bu dosya bulunamadı veya silinmiş.</b>", parse_mode=ParseMode.HTML)
            return

        file_id = row["file_id"]
        downloads = row["download_count"]
        header_info = f"📥 <b>Bu dosya toplam {downloads} kez indirildi!</b>"
        await update.message.reply_document(file_id, caption=header_info, parse_mode=ParseMode.HTML)
        return

    # Normal /start
    if is_admin(user.id):
        text = (
            f"👑 <b>Hoş geldin Patron {html.escape(user.first_name)}!</b>\n\n"
            f"🆔 Telegram ID'n: <code>{user.id}</code> (Yetkili)\n\n"
            "📁 Birleştirmek istediğin <code>.txt</code> dosyalarını arka arkaya gönder.\n"
            "🔗 İşin bitince <b>/merge</b> yaz, hepsini tek dosyada birleştireyim.\n"
            "📊 Kuyruğu sıfırlamak istersen: <b>/clear</b>"
        )
    else:
        text = (
            f"👋 Merhaba <b>{html.escape(user.first_name)}</b>!\n"
            f"🆔 Senin ID'n: <code>{user.id}</code>\n\n"
            "Bu bot üzerinden paylaşılan linklere tıklayarak dosyaları indirebilirsin.\n"
            "<i>(Yalnızca yöneticiler dosya yükleyebilir.)</i>"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text(
            f"⛔ <b>Yetkisiz İşlem!</b>\nSenin ID'n: <code>{user.id}</code>\n"
            f"Bu botta sadece yönetici dosya yükleyebilir.",
            parse_mode=ParseMode.HTML
        )
        return

    document = msg.document
    if not document:
        await msg.reply_text("⚠️ Lütfen bir <code>.txt</code> belgesi gönderin.")
        return

    status_msg = await msg.reply_text("⏳ Dosya inceleniyor...")

    try:
        file_obj = await context.bot.get_file(document.file_id)
        file_bytes = await file_obj.download_as_bytearray()
        
        text_content = file_bytes.decode('utf-8', errors='ignore')
        line_count = text_content.count('\n') + (1 if text_content and not text_content.endswith('\n') else 0)
    except Exception as e:
        logger.error(f"Dosya okuma hatası: {e}")
        await status_msg.edit_text(f"❌ Dosya okunamadı: {str(e)}")
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO merge_queue (admin_id, file_content, line_count, file_name)
            VALUES ($1, $2, $3, $4)
        """, user.id, text_content, line_count, document.file_name or "file.txt")

        total_files = await conn.fetchval("SELECT COUNT(*) FROM merge_queue WHERE admin_id = $1", user.id)
        total_lines = await conn.fetchval("SELECT COALESCE(SUM(line_count), 0) FROM merge_queue WHERE admin_id = $1", user.id)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Kuyruğu Temizle", callback_data="merge_clear")]
    ])

    await status_msg.edit_text(
        f"✅ <b>Dosya Kuyruğa Eklendi!</b>\n"
        f"📄 Eklenen Dosya Satırı: <code>{line_count:,}</code>\n"
        f"📦 Kuyruktaki Toplam Dosya: <b>{total_files}</b>\n"
        f"📊 Toplam Satır Sayısı: <b>{total_lines:,}</b>\n\n"
        f"👉 Başka dosya atabilirsin veya bitince <b>/merge</b> yazabilirsin.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )


async def merge_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("⛔ Bu komutu sadece yönetici kullanabilir.")
        return

    status_msg = await update.message.reply_text("⏳ Dosyalar @CalcyCloud olarak birleştiriliyor...")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT file_content FROM merge_queue WHERE admin_id = $1", user.id)

    if not rows:
        await status_msg.edit_text("⚠️ Kuyruğun boş! Önce bota birkaç <code>.txt</code> dosyası gönder.")
        return

    try:
        output = StringIO()
        for row in rows:
            output.write(row['file_content'] + "\n")
        
        combined_text = output.getvalue()
        output.close()

        total_lines = combined_text.count('\n')
        formatted_name = f"@CalcyCloud-{total_lines}.txt"

        raw_bytes = combined_text.encode('utf-8')
        file_bytes = BytesIO(raw_bytes)
        file_bytes.name = formatted_name
        file_bytes.seek(0)

        sent_msg = await context.bot.send_document(
            chat_id=user.id,
            document=file_bytes,
            filename=formatted_name,
            caption=f"📁 <b>Birleştirilen Dosya:</b> <code>{formatted_name}</code>\n📊 <b>Toplam Satır:</b> <code>{total_lines:,}</code>",
            parse_mode=ParseMode.HTML,
            read_timeout=180,
            write_timeout=180
        )
        
        telegram_file_id = sent_msg.document.file_id
        file_size = sent_msg.document.file_size or len(raw_bytes)

    except Exception as e:
        logger.error(f"Birleştirme hatası: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Dosya oluşturulurken hata: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return

    file_code = secrets.token_urlsafe(6)

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO files (file_code, user_id, file_id, file_type, file_name, file_size, download_count, caption, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, 0, $7, 1)
        """, file_code, user.id, telegram_file_id, 'document', formatted_name, file_size, formatted_name)

        await conn.execute("DELETE FROM merge_queue WHERE admin_id = $1", user.id)

    bot_username = context.bot.username
    download_url = f"https://t.me/{bot_username}?start=file_{file_code}"
    size_mb = round(file_size / (1024 * 1024), 2)

    resp = (
        "✅ <b>Dosyalar Başarıyla Birleştirildi!</b>\n\n"
        f"🏷️ Dosya Adı: <code>{formatted_name}</code>\n"
        f"📊 Boyut: <code>{size_mb} MB</code> | Satır: <code>{total_lines:,}</code>\n\n"
        f"🔗 <b>İndirme Linki:</b>\n<code>{download_url}</code>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Telegram'da Paylaş", url=f"https://t.me/share/url?url={download_url}&text=Dosyayı%20İndir")]
    ])

    await status_msg.delete()
    await context.bot.send_message(
        chat_id=user.id,
        text=resp,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def clear_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM merge_queue WHERE admin_id = $1", user.id)
    await update.message.reply_text("🗑️ Kuyruk temizlendi.")


async def merge_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        return

    await query.answer()
    if query.data == "merge_clear":
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM merge_queue WHERE admin_id = $1", user_id)
        await query.edit_message_text("🗑️ Kuyruk temizlendi.")


async def fallback_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        text = update.message.text
        if text.startswith("/"):
            return
        await update.message.reply_text(
            "ℹ️ Lütfen birleştirmek istediğiniz <code>.txt</code> dosyalarını belge olarak gönderin.\n"
            "Birleştirmek için: <b>/merge</b>",
            parse_mode=ParseMode.HTML
        )


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Global Hata:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            f"⚠️ <b>Hata:</b> <code>{html.escape(str(context.error))}</code>",
            parse_mode=ParseMode.HTML
        )

async def post_init(application: Application):
    """Bot başlarken çalışan kanca"""
    await init_db()
    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info("🚀 Bot hazır, Telegram dinleniyor...")


# ==============================================================================
# 🚀 5. EN SAĞLAM BAŞLATICI (run_polling)
# ==============================================================================
def main():
    t_request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=60.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=60.0
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(t_request)
        .post_init(post_init)
        .build()
    )

    application.add_error_handler(global_error_handler)

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("merge", merge_command_handler))
    application.add_handler(CommandHandler("clear", clear_command_handler))
    application.add_handler(CallbackQueryHandler(merge_callbacks, pattern="^merge_"))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_incoming_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text_handler))

    # Kütüphanenin kendi kendini toparlayan resmi döngüsü
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
