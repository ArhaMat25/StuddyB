import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# Токен бота
TELEGRAM_TOKEN = "8573998335:AAENV4S0UhOUAmc3RpzEeFDLuModI36aqhM"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Обработчик команды /start
async def start(update: Update, context):
    user = update.effective_user
    logger.info(f"Пользователь {user.id} (@{user.username}) запустил бота")
    await update.message.reply_text(
        "✅ Бот успешно запущен и работает!\n\n"
        "Отправьте любое сообщение, и я отвечу."
    )

# Обработчик текстовых сообщений
async def echo(update: Update, context):
    user = update.effective_user
    text = update.message.text
    logger.info(f"Получено сообщение от {user.id}: {text}")
    await update.message.reply_text(f"🔊 Вы написали: {text}")

# Основная функция
def main():
    logger.info("🚀 Запуск Telegram бота...")
    
    # Создаём приложение
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    
    # Запускаем бота
    logger.info("✅ Бот запущен и готов к работе!")
    app.run_polling(
        poll_interval=1.0,
        timeout=30,
        drop_pending_updates=True
    )

if __name__ == '__main__':
    main()
