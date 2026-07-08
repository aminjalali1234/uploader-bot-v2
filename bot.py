import os
import logging
import secrets
import threading
import asyncio

import asyncpg
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Environment variables ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
LOG_CHANNEL_ID = int(os.environ["LOG_CHANNEL_ID"])
ADMIN_IDS = [int(x.strip()) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()]
PORT = int(os.environ.get("PORT", 10000))

db_pool: asyncpg.Pool | None = None


# ---------- Database helpers ----------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                chat_id BIGINT PRIMARY KEY,
                join_link TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                code TEXT PRIMARY KEY,
                message_id BIGINT NOT NULL,
                added_at TIMESTAMP DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT NOW()
            );
            """
        )


async def get_channels():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT chat_id, join_link FROM channels")
        return [(r["chat_id"], r["join_link"]) for r in rows]


async def add_channel(chat_id: int, join_link: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO channels (chat_id, join_link) VALUES ($1, $2) "
            "ON CONFLICT (chat_id) DO UPDATE SET join_link = EXCLUDED.join_link",
            chat_id,
            join_link,
        )


async def remove_channel(chat_id: int):
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM channels WHERE chat_id = $1", chat_id)
        return result != "DELETE 0"


async def save_file(code: str, message_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO files (code, message_id) VALUES ($1, $2)", code, message_id
        )


async def get_file(code: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT message_id FROM files WHERE code = $1", code)
        return row["message_id"] if row else None


async def touch_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id,
        )


async def get_stats():
    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        files_count = await conn.fetchval("SELECT COUNT(*) FROM files")
        channels_count = await conn.fetchval("SELECT COUNT(*) FROM channels")
        return users_count, files_count, channels_count


# ---------- Membership check ----------
async def user_is_member_of_all(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[bool, list]:
    """Returns (all_joined, list_of_channels_not_joined)."""
    channels = await get_channels()
    not_joined = []
    for chat_id, join_link in channels:
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ("left", "kicked"):
                not_joined.append((chat_id, join_link))
        except Exception as e:
            logger.warning(f"Could not check membership for {chat_id}: {e}")
            not_joined.append((chat_id, join_link))
    return (len(not_joined) == 0), not_joined


def build_join_keyboard(not_joined: list, code: str) -> InlineKeyboardMarkup:
    buttons = []
    for i, (chat_id, link) in enumerate(not_joined, start=1):
        buttons.append([InlineKeyboardButton(f"🔗 عضویت در کانال {i}", url=link)])
    buttons.append([InlineKeyboardButton("✅ عضو شدم", callback_data=f"check_{code}")])
    return InlineKeyboardMarkup(buttons)


# ---------- Handlers ----------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await touch_user(user_id)

    args = context.args
    if not args:
        await update.message.reply_text(
            "سلام! 👋\nبرای دریافت فایل، از لینکی که برات فرستاده شده استفاده کن."
        )
        return

    code = args[0]
    message_id = await get_file(code)
    if message_id is None:
        await update.message.reply_text("❌ این لینک نامعتبره یا فایل حذف شده.")
        return

    all_joined, not_joined = await user_is_member_of_all(context, user_id)
    if not all_joined:
        await update.message.reply_text(
            "برای دریافت فایل، اول باید عضو کانال‌های زیر بشی، بعد دکمه‌ی «✅ عضو شدم» رو بزن:",
            reply_markup=build_join_keyboard(not_joined, code),
        )
        return

    await deliver_file(update.effective_chat.id, message_id, context)


async def check_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    code = query.data.split("_", 1)[1]

    message_id = await get_file(code)
    if message_id is None:
        await query.answer("این لینک دیگه معتبر نیست.", show_alert=True)
        return

    all_joined, not_joined = await user_is_member_of_all(context, user_id)
    if not all_joined:
        await query.answer("هنوز عضو همه‌ی کانال‌ها نشدی!", show_alert=True)
        return

    await query.answer("✅ عضویت تایید شد!")
    await query.message.delete()
    await deliver_file(query.message.chat_id, message_id, context)


async def deliver_file(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.copy_message(
            chat_id=chat_id, from_chat_id=LOG_CHANNEL_ID, message_id=message_id
        )
    except Exception as e:
        logger.error(f"Failed to deliver file: {e}")
        await context.bot.send_message(chat_id, "❌ مشکلی در ارسال فایل پیش اومد.")


async def admin_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles files sent by the admin in private chat."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if update.effective_chat.type != "private":
        return

    msg = update.message
    has_media = msg.document or msg.video or msg.audio or msg.photo or msg.voice or msg.animation
    if not has_media:
        return

    forwarded = await context.bot.copy_message(
        chat_id=LOG_CHANNEL_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
    )

    code = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
    await save_file(code, forwarded.message_id)

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={code}"
    await msg.reply_text(f"✅ فایل ذخیره شد.\n\n🔗 لینک اختصاصی:\n{link}")


async def addchannel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "استفاده‌ی درست:\n/addchannel <chat_id> <join_link>\n"
            "مثال:\n/addchannel -1001234567890 https://t.me/mychannel"
        )
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ chat_id باید عددی باشه (با -100 شروع بشه).")
        return
    join_link = context.args[1]
    await add_channel(chat_id, join_link)
    await update.message.reply_text("✅ کانال اضافه شد.")


async def delchannel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) != 1:
        await update.message.reply_text("استفاده‌ی درست:\n/delchannel <chat_id>")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ chat_id باید عددی باشه.")
        return
    removed = await remove_channel(chat_id)
    if removed:
        await update.message.reply_text("✅ کانال حذف شد.")
    else:
        await update.message.reply_text("چنین کانالی توی لیست نبود.")


async def channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    channels = await get_channels()
    if not channels:
        await update.message.reply_text("هنوز هیچ کانال جوین اجباری اضافه نشده.")
        return
    text = "📋 کانال‌های جوین اجباری:\n\n"
    for chat_id, link in channels:
        text += f"• {chat_id} — {link}\n"
    await update.message.reply_text(text)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    users_count, files_count, channels_count = await get_stats()
    await update.message.reply_text(
        f"📊 آمار ربات:\n\n"
        f"👤 کاربران: {users_count}\n"
        f"📁 فایل‌های آپلود شده: {files_count}\n"
        f"📢 کانال‌های جوین اجباری: {channels_count}"
    )


# ---------- Flask keep-alive server ----------
flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return "Bot is running.", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


# ---------- Main ----------
async def post_init(application: Application):
    await init_db()
    logger.info("Database initialized.")


def main():
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("addchannel", addchannel_handler))
    application.add_handler(CommandHandler("delchannel", delchannel_handler))
    application.add_handler(CommandHandler("channels", channels_handler))
    application.add_handler(CommandHandler("stats", stats_handler))
    application.add_handler(CallbackQueryHandler(check_membership_callback, pattern=r"^check_"))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO | filters.VOICE | filters.ANIMATION),
            admin_upload_handler,
        )
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()