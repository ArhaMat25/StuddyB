import os
import threading
from flask import Flask
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# --- ЗДЕСЬ ВАШ ОСНОВНОЙ КОД БОТА (handler'ы, классы и т.д.) ---
async def start(update, context):
    await update.message.reply_text('Бот работает!')
    
def main():
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    # ... добавьте остальные handler'ы ...
    app.run_polling()
# --------------------------------------------------------------

# --- ЭТА ЧАСТЬ ОТВЕЧАЕТ ЗА РАБОТУ НА RENDER ---
# Создаем Flask-приложение для healthcheck'ов
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return "OK"

# Функция для запуска бота в фоновом потоке
def run_bot():
    main()

if __name__ == '__main__':
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    # Запускаем Flask-сервер на порту, который дал Render
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
