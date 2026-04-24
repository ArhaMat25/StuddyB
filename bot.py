import os
import logging
import sqlite3
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify

import pandas as pd
from telegram import Update, Bot, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ============= КОНФИГУРАЦИЯ =============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = "8573998335:AAENV4S0UhOUAmc3RpzEeFDLuModI36aqhM"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Пути
DATA_DIR = Path("bot_data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot_data.db"

# Flask приложение
app = Flask(__name__)

# Глобальная переменная для Application
telegram_app = None

# ============= БАЗА ДАННЫХ =============
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            lesson_number INTEGER,
            subject TEXT,
            room TEXT
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def get_schedule(user_id: int, day: str = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if day:
        cur.execute("SELECT day, lesson_number, subject, room FROM schedule WHERE user_id=? AND day=? ORDER BY lesson_number", (user_id, day))
    else:
        cur.execute("SELECT day, lesson_number, subject, room FROM schedule WHERE user_id=? ORDER BY day, lesson_number", (user_id,))
    result = cur.fetchall()
    conn.close()
    return result

def save_schedule(user_id: int, lessons: list):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM schedule WHERE user_id=?", (user_id,))
        for les in lessons:
            conn.execute(
                "INSERT INTO schedule (user_id, day, lesson_number, subject, room) VALUES (?,?,?,?,?)",
                (user_id, les['day'], les['lesson_number'], les['subject'], les.get('room', ''))
            )
        conn.commit()
        conn.close()
        logger.info(f"Сохранено {len(lessons)} уроков")
        return True
    except Exception as e:
        logger.error(f"Save error: {e}")
        return False

# ============= ПАРСЕР EXCEL =============
def parse_excel(content: bytes):
    try:
        df = pd.read_excel(content)
        lessons = []
        for _, row in df.iterrows():
            day = str(row.get('День', row.get('день', ''))).strip()
            num = int(row.get('Номер_урока', row.get('номер_урока', 0)))
            subject = str(row.get('Предмет', row.get('предмет', ''))).strip()
            room = str(row.get('Кабинет', row.get('кабинет', ''))).strip()
            if day and num and subject:
                lessons.append({'day': day, 'lesson_number': num, 'subject': subject, 'room': room})
        return lessons
    except Exception as e:
        logger.error(f"Excel parse error: {e}")
        return []

# ============= ОБРАБОТЧИКИ КОМАНД =============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📅 Расписание")],
        [KeyboardButton("📤 Загрузить расписание")],
        [KeyboardButton("ℹ️ Помощь")]
    ]
    await update.message.reply_text(
        "👋 Привет! Я бот для расписания.\n\n"
        "📌 Отправь Excel файл с расписанием\n"
        "📌 Спроси: 'Какой завтра первый урок?'\n\n"
        "Колонки в Excel: День, Номер_урока, Предмет, Кабинет",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lessons = get_schedule(user.id)
    if not lessons:
        await update.message.reply_text("📭 Расписание не загружено. Отправьте Excel файл.")
        return
    
    by_day = {}
    for d, num, subj, room in lessons:
        by_day.setdefault(d, []).append((num, subj, room))
    
    resp = "📅 **Ваше расписание:**\n\n"
    for day in ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']:
        if day in by_day:
            resp += f"*{day}:*\n"
            for num, subj, room in sorted(by_day[day]):
                room_txt = f" (каб.{room})" if room else ""
                resp += f"  {num}. {subj}{room_txt}\n"
            resp += "\n"
    await update.message.reply_text(resp, parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document
    filename = doc.file_name
    ext = filename.split('.')[-1].lower()
    
    await update.message.reply_text(f"📥 Загружаю {filename}...")
    file = await doc.get_file()
    content = await file.download_as_bytearray()
    
    if ext in ['xlsx', 'xls']:
        lessons = parse_excel(content)
        if lessons:
            save_schedule(user.id, lessons)
            days = set(l['day'] for l in lessons)
            await update.message.reply_text(f"✅ Загружено {len(lessons)} уроков!\n📅 Дни: {', '.join(days)}")
        else:
            await update.message.reply_text("❌ Не удалось распознать расписание.\n\nПроверьте колонки: День, Номер_урока, Предмет, Кабинет")
    else:
        await update.message.reply_text("❌ Поддерживаются только Excel файлы (.xlsx, .xls)")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "📅 Расписание":
        await show_schedule(update, context)
    elif text == "📤 Загрузить расписание":
        await update.message.reply_text("📎 Отправьте Excel файл с расписанием.\n\nКолонки: День, Номер_урока, Предмет, Кабинет")
    elif text == "ℹ️ Помощь":
        await update.message.reply_text(
            "📌 **Как пользоваться:**\n\n"
            "1. Создайте Excel файл с колонками:\n"
            "   - День\n"
            "   - Номер_урока\n"
            "   - Предмет\n"
            "   - Кабинет\n\n"
            "2. Отправьте файл боту\n"
            "3. Спрашивайте: 'Какой завтра первый урок?'"
        )
    else:
        await update.message.reply_text(
            "Я понимаю не все команды.\n\n"
            "Используйте кнопки меню:\n"
            "• 📅 Расписание\n"
            "• 📤 Загрузить расписание\n"
            "• ℹ️ Помощь"
        )

# ============= WEBHOOK =============
@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        global telegram_app
        # Получаем обновление от Telegram
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, telegram_app.bot)
        
        # Обрабатываем обновление
        await telegram_app.process_update(update)
        
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/')
@app.route('/health')
def health():
    return "OK", 200

# ============= ЗАПУСК =============
if __name__ == '__main__':
    init_db()
    
    # Создаём Application
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем обработчики
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    telegram_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Настройка вебхука
    webhook_url = os.environ.get("WEBHOOK_URL")
    
    # Запускаем Flask в отдельном потоке
    import threading
    port = int(os.environ.get("PORT", 5000))
    
    if webhook_url:
        # На продакшене (Render) - используем вебхук
        webhook_path = f"{webhook_url}/webhook"
        
        async def setup_webhook():
            await telegram_app.bot.set_webhook(webhook_path)
            logger.info(f"✅ Webhook установлен: {webhook_path}")
        
        # Запускаем настройку вебхука в event loop
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(setup_webhook())
        
        logger.info(f"🌐 Flask сервер запущен на порту {port}")
        app.run(host="0.0.0.0", port=port)
    else:
        # Локально - используем polling
        logger.info("⚠️ WEBHOOK_URL не задан, запуск в polling режиме")
        telegram_app.run_polling()
