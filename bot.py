import os
import logging
import sqlite3
import re
import uuid
import io
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from contextlib import contextmanager

import requests
import pandas as pd
import pdfplumber
import pytesseract
from PIL import Image
from icalendar import Calendar, Event
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ============= КОНФИГУРАЦИЯ =============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в переменных окружения")

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

# ============= УПРОЩЁННАЯ БАЗА ДАННЫХ =============
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
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
    cursor = conn.cursor()
    if day:
        cursor.execute(
            "SELECT day, lesson_number, subject, room FROM schedule WHERE user_id=? AND day=? ORDER BY lesson_number",
            (user_id, day)
        )
    else:
        cursor.execute(
            "SELECT day, lesson_number, subject, room FROM schedule WHERE user_id=? ORDER BY day, lesson_number",
            (user_id,)
        )
    result = cursor.fetchall()
    conn.close()
    return result

def save_schedule(user_id: int, lessons: List[Dict]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM schedule WHERE user_id=?", (user_id,))
    for lesson in lessons:
        cursor.execute(
            "INSERT INTO schedule (user_id, day, lesson_number, subject, room) VALUES (?,?,?,?,?)",
            (user_id, lesson['day'], lesson['lesson_number'], lesson['subject'], lesson.get('room', ''))
        )
    conn.commit()
    conn.close()
    return True

# ============= ОБРАБОТЧИКИ БОТА =============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📅 Расписание"), KeyboardButton("📤 Загрузить")],
        [KeyboardButton("➕ Добавить урок"), KeyboardButton("➖ Удалить урок")],
        [KeyboardButton("ℹ️ О боте")]
    ]
    await update.message.reply_text(
        "👋 Привет! Я бот-помощник для расписания.\n\n"
        "📌 Отправь Excel, PDF или фото с расписанием\n"
        "📌 Спрашивай: 'Какой завтра первый урок?'\n"
        "📌 Пиши: 'Вместо физики будет история'",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lessons = get_schedule(user.id)
    if not lessons:
        await update.message.reply_text("📭 Расписание не загружено. Отправьте файл с расписанием.")
        return
    
    by_day = {}
    for day, num, subject, room in lessons:
        by_day.setdefault(day, []).append((num, subject, room))
    
    response = "📅 **Ваше расписание:**\n\n"
    for day in ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']:
        if day in by_day:
            response += f"*{day}:*\n"
            for num, subject, room in sorted(by_day[day]):
                room_txt = f" (каб.{room})" if room else ""
                response += f"  {num}. {subject}{room_txt}\n"
            response += "\n"
    await update.message.reply_text(response)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document
    filename = doc.file_name
    ext = filename.split('.')[-1].lower()
    
    await update.message.reply_text(f"📥 Загружаю {filename}...")
    file = await doc.get_file()
    content = await file.download_as_bytearray()
    
    if ext in ['xlsx', 'xls']:
        # Простой парсинг Excel
        df = pd.read_excel(io.BytesIO(content))
        lessons = []
        for _, row in df.iterrows():
            day = str(row.get('День', ''))
            num = int(row.get('Номер_урока', 0))
            subject = str(row.get('Предмет', ''))
            room = str(row.get('Кабинет', ''))
            if day and num and subject:
                lessons.append({'day': day, 'lesson_number': num, 'subject': subject, 'room': room})
        
        if lessons:
            save_schedule(user.id, lessons)
            await update.message.reply_text(f"✅ Загружено {len(lessons)} уроков!")
        else:
            await update.message.reply_text("❌ Не удалось распознать расписание")
    else:
        await update.message.reply_text("❌ Поддерживаются только Excel файлы (.xlsx, .xls)")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "📅 Расписание":
        await show_schedule(update, context)
    elif text == "📤 Загрузить":
        await update.message.reply_text("Отправьте Excel файл с расписанием.\n\nКолонки: День, Номер_урока, Предмет, Кабинет")
    elif text == "➕ Добавить урок":
        await update.message.reply_text("Напишите: Добавь урок в [день] [номер] уроком [предмет]\nПример: Добавь урок в понедельник 3-м уроком математику")
    elif text == "➖ Удалить урок":
        await update.message.reply_text("Напишите: Удали урок в [день] [номер] урок\nПример: Удали урок в понедельник 3-й урок")
    elif text == "ℹ️ О боте":
        await update.message.reply_text("🤖 Бот для управления расписанием\nВерсия 2.0\nРаботает на Render.com")
    else:
        await update.message.reply_text("Я понимаю не все команды. Используйте кнопки меню!")

# ============= ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА =============
def run_bot():
    """Запуск бота в отдельном потоке"""
    logger.info("🚀 Запуск Telegram бота...")
    init_db()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    logger.info("✅ Бот запущен и готов к работе!")
    app.run_polling(poll_interval=1.0, timeout=30, drop_pending_updates=True)
