import os
import sys
import logging
import threading
from flask import Flask

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN не задан!")
    sys.exit(1)

# Импорт бота (замените на ваш bot.py)
try:
    from bot import run_bot
    logger.info("✅ Бот импортирован успешно")
except ImportError as e:
    logger.error(f"❌ Ошибка импорта бота: {e}")
    logger.info("💡 Создайте файл bot.py с функцией run_bot()")
    run_bot = None

flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return "OK", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🌐 Flask сервер запущен на порту {port}")
    
    if run_bot:
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        logger.info("🤖 Бот запущен в фоновом потоке")
    else:
        logger.warning("⚠️ Бот не запущен: функция run_bot не найдена")
    
    flask_app.run(host="0.0.0.0", port=port)
