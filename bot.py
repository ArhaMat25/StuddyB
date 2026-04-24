import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context):
    await update.message.reply_text("✅ Бот работает на Render!")

async def echo(update: Update, context):
    await update.message.reply_text(f"Вы сказали: {update.message.text}")

def run_bot():
    logger.info("🚀 Запуск Telegram бота...")
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не задан!")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    logger.info("✅ Бот запущен и ждёт сообщения")
    app.run_polling()

if __name__ == '__main__':
    run_bot()
