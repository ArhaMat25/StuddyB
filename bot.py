import os
import logging
import sqlite3
import re
import uuid
import io
import threading
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from contextlib import contextmanager

import requests
import base64
import pandas as pd
import pdfplumber
import pytesseract
from PIL import Image
from icalendar import Calendar, Event
from flask import Flask, request, jsonify

# Telegram
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ============= КОНФИГУРАЦИЯ =============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = "8573998335:AAENV4S0UhOUAmc3RpzEeFDLuModI36aqhM"

GIGACHAT_CLIENT_ID = os.environ.get("GIGACHAT_CLIENT_ID", "019ac450-7c0b-7686-a4ec-e979dd4fa0f5")
GIGACHAT_CLIENT_SECRET = os.environ.get("GIGACHAT_CLIENT_SECRET", "8dc579fc-56ee-49bd-b8cd-a0cd3fe4ae56")

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
flask_app = Flask(__name__)

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
            room TEXT,
            start_time TEXT,
            teacher TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def get_schedule(user_id: int, day: str = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if day:
        cur.execute("SELECT day, lesson_number, start_time, subject, room, teacher FROM schedule WHERE user_id=? AND day=? ORDER BY lesson_number", (user_id, day))
    else:
        cur.execute("SELECT day, lesson_number, start_time, subject, room, teacher FROM schedule WHERE user_id=? ORDER BY day, lesson_number", (user_id,))
    result = cur.fetchall()
    conn.close()
    return result

def save_schedule(user_id: int, lessons: List[Dict]) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM schedule WHERE user_id=?", (user_id,))
        for les in lessons:
            conn.execute(
                "INSERT INTO schedule (user_id, day, lesson_number, subject, room, start_time, teacher) VALUES (?,?,?,?,?,?,?)",
                (user_id, les['day'], les.get('lesson_number', 0), les['subject'], 
                 les.get('room', ''), les.get('start_time', ''), les.get('teacher', ''))
            )
        conn.commit()
        conn.close()
        logger.info(f"Сохранено {len(lessons)} уроков")
        return True
    except Exception as e:
        logger.error(f"Save error: {e}")
        return False

# ============= ПАРСЕР ЗАМЕН =============
class ReplacementParser:
    def __init__(self):
        self.days_mapping = {
            'понедельник': 'Понедельник', 'пн': 'Понедельник',
            'вторник': 'Вторник', 'вт': 'Вторник',
            'среда': 'Среда', 'ср': 'Среда',
            'четверг': 'Четверг', 'чт': 'Четверг',
            'пятница': 'Пятница', 'пт': 'Пятница',
            'суббота': 'Суббота', 'сб': 'Суббота'
        }

    def parse_replacement_message(self, message: str) -> dict:
        msg = message.lower()
        day = self._extract_day(msg)
        lesson_num = self._extract_lesson_number(msg)
        old, new = self._extract_subjects(msg)
        room = self._extract_classroom(msg)
        return {
            'success': bool(day and (old or new)),
            'day': day,
            'lesson_number': lesson_num,
            'old_subject': old,
            'new_subject': new,
            'classroom': room,
            'is_cancellation': new is None
        }

    def _extract_day(self, text: str) -> Optional[str]:
        if 'сегодня' in text:
            return self._get_day_offset(0)
        if 'завтра' in text:
            return self._get_day_offset(1)
        for key, val in self.days_mapping.items():
            if key in text:
                return val
        return None

    def _extract_lesson_number(self, text: str) -> Optional[int]:
        match = re.search(r'(\d+)[-ыи]?м?\s+урок', text)
        return int(match.group(1)) if match else None

    def _extract_subjects(self, text: str) -> tuple:
        match = re.search(r'вместо\s+([^\s,]+(?:\s+[^\s,]+)*)\s+будет\s+([^\s,]+(?:\s+[^\s,]+)*)', text)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        match_del = re.search(r'не будет\s+([^\s,]+(?:\s+[^\s,]+)*)', text)
        if match_del:
            return match_del.group(1).strip(), None
        return None, None

    def _extract_classroom(self, text: str) -> Optional[str]:
        match = re.search(r'кабинет[е]?\s*(\d+)', text)
        return match.group(1) if match else None

    def _get_day_offset(self, offset: int) -> str:
        days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        today = datetime.now().weekday()
        return days[(today + offset) % 7]

# ============= РЕДАКТОР РАСПИСАНИЯ =============
class ScheduleEditor:
    def __init__(self, db_path):
        self.db_path = db_path

    def add_lesson(self, user_id: int, day: str, lesson_num: int, subject: str, room: str = "") -> dict:
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM schedule WHERE user_id=? AND day=? AND lesson_number=?", (user_id, day, lesson_num))
            if cur.fetchone():
                conn.close()
                return {'success': False, 'message': '❌ Слот уже занят'}
            cur.execute("INSERT INTO schedule (user_id, day, lesson_number, subject, room) VALUES (?,?,?,?,?)",
                        (user_id, day, lesson_num, subject, room))
            conn.commit()
            conn.close()
            return {'success': True, 'message': f'✅ Урок {subject} добавлен'}
        except Exception as e:
            return {'success': False, 'message': f'Ошибка: {e}'}

    def replace_lesson(self, user_id: int, day: str, lesson_num: int, subject: str, room: str = "") -> dict:
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("UPDATE schedule SET subject=?, room=? WHERE user_id=? AND day=? AND lesson_number=?",
                        (subject, room, user_id, day, lesson_num))
            conn.commit()
            conn.close()
            return {'success': True, 'message': f'🔄 Урок заменён на {subject}'}
        except Exception as e:
            return {'success': False, 'message': f'Ошибка: {e}'}

    def remove_lesson(self, user_id: int, day: str, lesson_num: int = None, subject: str = None) -> dict:
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            if lesson_num:
                cur.execute("DELETE FROM schedule WHERE user_id=? AND day=? AND lesson_number=?", (user_id, day, lesson_num))
            elif subject:
                cur.execute("DELETE FROM schedule WHERE user_id=? AND day=? AND subject LIKE ?", (user_id, day, f'%{subject}%'))
            else:
                conn.close()
                return {'success': False, 'message': '❌ Укажите номер урока или предмет'}
            conn.commit()
            conn.close()
            return {'success': True, 'message': '🗑️ Урок удалён'}
        except Exception as e:
            return {'success': False, 'message': f'Ошибка: {e}'}

    def parse_add_command(self, text: str) -> dict:
        day = self._extract_day(text.lower())
        lesson_num = self._extract_number(text)
        subject = self._extract_subject(text)
        if not day or not lesson_num or not subject:
            return {'success': False, 'message': '❌ Не удалось распознать команду.\nПример: Добавь урок в понедельник 3-м уроком математику'}
        room = self._extract_room(text)
        return {'success': True, 'day': day, 'lesson_number': lesson_num, 'subject': subject, 'room': room}

    def parse_remove_command(self, text: str) -> dict:
        day = self._extract_day(text.lower())
        if not day:
            return {'success': False, 'message': '❌ Не указан день'}
        lesson_num = self._extract_number(text)
        if not lesson_num:
            subject = self._extract_subject(text)
            if not subject:
                return {'success': False, 'message': '❌ Укажите номер урока или предмет'}
            return {'success': True, 'day': day, 'subject': subject}
        return {'success': True, 'day': day, 'lesson_number': lesson_num}

    def _extract_day(self, text: str) -> Optional[str]:
        days = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']
        for d in days:
            if d in text:
                return d.capitalize()
        return None

    def _extract_number(self, text: str) -> Optional[int]:
        match = re.search(r'(\d+)[-ыи]?м?\s+урок', text)
        return int(match.group(1)) if match else None

    def _extract_subject(self, text: str) -> Optional[str]:
        subjects = ['математика', 'физика', 'химия', 'биология', 'история', 'география',
                   'английский', 'русский', 'литература', 'информатика', 'физкультура']
        for subj in subjects:
            if subj in text:
                return subj
        return None

    def _extract_room(self, text: str) -> str:
        match = re.search(r'кабинет[е]?\s*(\d+)', text)
        return match.group(1) if match else ''

# ============= RAG-СИСТЕМА =============
class ScheduleRAGSystem:
    def parse_question(self, question: str) -> dict:
        q = question.lower()
        day = self._extract_day(q)
        if not day:
            day = self._get_today()
        lesson_num = self._extract_lesson_num(q)
        subject = self._extract_subject(q)
        return {'day': day, 'lesson_number': lesson_num, 'subject': subject}

    def generate_precise_answer(self, entities: dict, lessons: list, day: str) -> str:
        if not lessons:
            return f"📭 Нет расписания на {day}"
        if entities['lesson_number']:
            for les in lessons:
                if les[1] == entities['lesson_number']:
                    room = f" (каб. {les[4]})" if les[4] else ""
                    return f"📚 {day}, {entities['lesson_number']}-й урок: {les[3]}{room}"
            return f"❌ {entities['lesson_number']}-го урока нет в расписании"
        if entities['subject']:
            for les in lessons:
                if entities['subject'] in les[3].lower():
                    return f"📖 {entities['subject']} на {day} — {les[1]}-й урок"
            return f"❌ {entities['subject']} не найден на {day}"
        resp = f"📅 Расписание на {day}:\n"
        for les in sorted(lessons, key=lambda x: x[1]):
            room = f" (каб. {les[4]})" if les[4] else ""
            resp += f"{les[1]}. {les[3]}{room}\n"
        return resp

    def _extract_day(self, text: str) -> Optional[str]:
        days = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']
        for d in days:
            if d in text:
                return d.capitalize()
        return None

    def _extract_lesson_num(self, text: str) -> Optional[int]:
        match = re.search(r'(\d+)[-ыи]?й?\s*урок', text)
        if not match:
            match = re.search(r'урок\s*(\d+)', text)
        return int(match.group(1)) if match else None

    def _extract_subject(self, text: str) -> Optional[str]:
        subjects = ['математика', 'физика', 'химия', 'биология', 'история', 'география',
                   'английский', 'русский', 'литература', 'информатика']
        for s in subjects:
            if s in text:
                return s
        return None

    def _get_today(self) -> str:
        days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        return days[datetime.now().weekday()]

# ============= ПАРСЕР ФАЙЛОВ =============
class ScheduleParser:
    @staticmethod
    def parse_excel(content: bytes) -> List[Dict]:
        try:
            df = pd.read_excel(io.BytesIO(content))
            lessons = []
            for _, row in df.iterrows():
                day = str(row.get('День', row.get('день', ''))).strip()
                num = int(row.get('Номер_урока', row.get('номер_урока', 0)))
                subject = str(row.get('Предмет', row.get('предмет', ''))).strip()
                room = str(row.get('Кабинет', row.get('кабинет', ''))).strip()
                if day and num and subject:
                    lessons.append({'day': day, 'lesson_number': num, 'subject': subject, 'room': room, 'start_time': '', 'teacher': ''})
            return lessons
        except Exception as e:
            logger.error(f"Excel parse error: {e}")
            return []

    @staticmethod
    def parse_pdf(content: bytes) -> List[Dict]:
        try:
            lessons = []
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        lines = text.split('\n')
                        cur_day = None
                        for line in lines:
                            for d in ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница']:
                                if d.lower() in line.lower():
                                    cur_day = d
                                    break
                            nums = re.findall(r'\b\d+\b', line)
                            if cur_day and nums:
                                num = int(nums[0])
                                words = re.findall(r'[А-Яа-я]+', line)
                                if words:
                                    subject = ' '.join(words[:3])[:50]
                                    lessons.append({'day': cur_day, 'lesson_number': num, 'subject': subject, 'room': '', 'start_time': '', 'teacher': ''})
            return lessons
        except Exception as e:
            logger.error(f"PDF parse error: {e}")
            return []

    @staticmethod
    def parse_image(content: bytes) -> List[Dict]:
        try:
            image = Image.open(io.BytesIO(content))
            text = pytesseract.image_to_string(image, lang='rus')
            lessons = []
            cur_day = None
            for line in text.split('\n'):
                for d in ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница']:
                    if d.lower() in line.lower():
                        cur_day = d
                        break
                nums = re.findall(r'\b\d+\b', line)
                if cur_day and nums:
                    num = int(nums[0])
                    words = re.findall(r'[А-Яа-я]+', line)
                    if words:
                        subject = ' '.join(words[:3])[:50]
                        lessons.append({'day': cur_day, 'lesson_number': num, 'subject': subject, 'room': '', 'start_time': '', 'teacher': ''})
            return lessons
        except Exception as e:
            logger.error(f"Image parse error: {e}")
            return []

# ============= АНАЛИЗАТОР СЛОЖНОСТИ =============
class DayComplexityAnalyzer:
    def calculate_day_complexity(self, lessons: List[Dict]) -> dict:
        count = len(lessons)
        if count == 0:
            return {'score': 0, 'level': 'нет уроков', 'recommendations': ['😊 Отдыхайте'], 'lesson_count': 0}
        score = min(10, count * 0.8)
        if score <= 3:
            level, rec = 'лёгкий', ['Можно расслабиться после школы']
        elif score <= 6:
            level, rec = 'средний', ['Планируйте время на домашку']
        else:
            level, rec = 'сложный', ['Готовьтесь заранее, будет тяжело']
        return {'score': round(score, 1), 'level': level, 'recommendations': rec, 'lesson_count': count}

# ============= ЭКСПОРТ В КАЛЕНДАРЬ =============
class CalendarExporter:
    def generate_ics_file(self, lessons: List[Dict], weeks: int = 4) -> bytes:
        cal = Calendar()
        cal.add('version', '2.0')
        start = datetime.now()
        for w in range(weeks):
            for les in lessons:
                day_idx = self._day_to_index(les['day'])
                if day_idx is None:
                    continue
                date = start + timedelta(days=(day_idx - start.weekday() + 7 * w))
                event = Event()
                event.add('summary', les['subject'])
                event.add('dtstart', datetime(date.year, date.month, date.day, 8, 0))
                event.add('dtend', datetime(date.year, date.month, date.day, 8, 45))
                cal.add_component(event)
        return cal.to_ical()

    def _day_to_index(self, day: str) -> Optional[int]:
        m = {'Понедельник': 0, 'Вторник': 1, 'Среда': 2, 'Четверг': 3, 'Пятница': 4, 'Суббота': 5}
        return m.get(day)

# ============= GIGACHAT =============
class GigaChatService:
    def __init__(self):
        self.access_token = None
        self.expires_at = None

    def _get_token(self) -> Optional[str]:
        if self.access_token and self.expires_at and datetime.now() < self.expires_at:
            return self.access_token
        try:
            creds = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}"
            encoded = base64.b64encode(creds.encode()).decode()
            resp = requests.post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers={'Authorization': f'Basic {encoded}', 'RqUID': str(uuid.uuid4())},
                data={'scope': 'GIGACHAT_API_PERS'},
                verify=False,
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                self.access_token = data['access_token']
                self.expires_at = datetime.now() + timedelta(seconds=1500)
                return self.access_token
        except Exception as e:
            logger.error(f"GigaChat token error: {e}")
        return None

    def send_message(self, text: str) -> str:
        token = self._get_token()
        if not token:
            return "🤖 AI-помощник временно недоступен"
        try:
            resp = requests.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                json={
                    "model": "GigaChat",
                    "messages": [{"role": "user", "content": text[:1000]}],
                    "temperature": 0.7,
                    "max_tokens": 500
                },
                verify=False,
                timeout=15
            )
            if resp.status_code == 200:
                return resp.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"GigaChat request error: {e}")
        return "Не удалось получить ответ от AI"

# ============= ОСНОВНОЙ БОТ =============
class TelegramBot:
    def __init__(self):
        self.parser = ScheduleParser()
        self.analyzer = DayComplexityAnalyzer()
        self.calendar = CalendarExporter()
        self.rag = ScheduleRAGSystem()
        self.repl_parser = ReplacementParser()
        self.editor = ScheduleEditor(DB_PATH)
        self.giga = GigaChatService()
        init_db()

    def get_schedule(self, user_id: int, day: str = None):
        return get_schedule(user_id, day)

    def save_schedule(self, user_id: int, lessons: List[Dict]) -> bool:
        return save_schedule(user_id, lessons)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [KeyboardButton("📚 Помощь"), KeyboardButton("🤖 Вопрос")],
            [KeyboardButton("📅 Расписание"), KeyboardButton("📤 Загрузить")],
            [KeyboardButton("➕ Добавить урок"), KeyboardButton("➖ Удалить урок")],
            [KeyboardButton("📊 Оценить завтра"), KeyboardButton("📅 Экспорт календаря")],
            [KeyboardButton("ℹ️ О боте")]
        ]
        await update.message.reply_text(
            "👋 Привет! Я бот-помощник для расписания.\n\n"
            "📌 **Что я умею:**\n"
            "• Загружать расписание из Excel/PDF/фото\n"
            "• Отвечать на вопросы о расписании\n"
            "• Автоматически обрабатывать замены уроков\n"
            "• Добавлять и удалять уроки\n"
            "• Оценивать сложность дня\n"
            "• Экспортировать расписание в календарь\n\n"
            "💡 **Примеры:**\n"
            "• 'Какой завтра первый урок?'\n"
            "• 'Вместо физики будет история'\n"
            "• 'Добавь урок в понедельник 3-м уроком математику'\n\n"
            "Используй кнопки ниже или просто напиши вопрос!",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text

        if text == "📚 Помощь":
            await update.message.reply_text("Напишите вопрос по учёбе, я отвечу с помощью ИИ.\n\nПримеры:\n• Объясни теорему Пифагора\n• Как решать квадратные уравнения?")
            return
        if text == "🤖 Вопрос":
            await update.message.reply_text("Задайте ваш вопрос:")
            return
        if text == "📅 Расписание":
            await self.show_schedule(update, context)
            return
        if text == "📤 Загрузить":
            await update.message.reply_text("📎 Отправьте файл с расписанием в одном из форматов:\n• Excel (.xlsx, .xls)\n• PDF\n• Фото расписания")
            return
        if text == "➕ Добавить урок":
            await update.message.reply_text("➕ **Добавление урока:**\n\nНапишите в формате:\n`Добавь урок в [день] [номер] уроком [предмет]`\n\nПример:\n`Добавь урок в понедельник 3-м уроком математику в 201 кабинете`")
            return
        if text == "➖ Удалить урок":
            await update.message.reply_text("➖ **Удаление урока:**\n\nНапишите в формате:\n`Удали урок в [день] [номер] урок`\n\nПример:\n`Удали урок в понедельник 3-й урок`")
            return
        if text == "📊 Оценить завтра":
            await self.analyze_tomorrow(update, context)
            return
        if text == "📅 Экспорт календаря":
            await self.export_calendar(update, context)
            return
        if text == "ℹ️ О боте":
            await update.message.reply_text("🤖 Бот для управления расписанием. Версия 2.0. Работает на Render.com")
            return

        if self._is_replacement(text):
            await self.handle_replacement(update, context, text)
            return
        if self._is_add_command(text):
            await self.handle_add_lesson(update, context, text)
            return
        if self._is_remove_command(text):
            await self.handle_remove_lesson(update, context, text)
            return
        if self._is_schedule_question(text):
            await self.handle_schedule_query(update, context, text)
            return

        await update.message.reply_chat_action(action="typing")
        resp = self.giga.send_message(text)
        await update.message.reply_text(resp or "❌ Не удалось получить ответ.")

    def _is_replacement(self, text: str) -> bool:
        return 'вместо' in text.lower() or ('не будет' in text.lower() and 'урок' in text.lower())

    def _is_add_command(self, text: str) -> bool:
        return ('добавь' in text.lower() or 'внеси' in text.lower() or 'запиши' in text.lower()) and 'урок' in text.lower()

    def _is_remove_command(self, text: str) -> bool:
        return ('удали' in text.lower() or 'отмени' in text.lower() or 'убери' in text.lower()) and 'урок' in text.lower()

    def _is_schedule_question(self, text: str) -> bool:
        kw = ['урок', 'расписание', 'кабинет', 'когда', 'сколько', 'первый', 'второй', 'третий', 'четвертый', 'пятый']
        return any(k in text.lower() for k in kw)

    async def handle_replacement(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        data = self.repl_parser.parse_replacement_message(text)
        if not data['success']:
            await update.message.reply_text("❌ Не удалось распознать замену.\n\nПример: 'Завтра 5-м уроком вместо физики будет история'")
            return
        user = update.effective_user
        if data['lesson_number'] and data['new_subject']:
            res = self.editor.replace_lesson(user.id, data['day'], data['lesson_number'], data['new_subject'], data['classroom'] or '')
            if res['success']:
                await update.message.reply_text(f"✅ Замена применена!\n\n📅 {data['day']}\n🔄 {data['lesson_number']}-й урок: → {data['new_subject']}")
            else:
                await update.message.reply_text(f"❌ Ошибка: {res['message']}")
        elif data['old_subject'] and data['is_cancellation']:
            res = self.editor.remove_lesson(user.id, data['day'], subject=data['old_subject'])
            await update.message.reply_text("✅ Урок отменён" if res['success'] else "❌ Не удалось отменить урок")
        else:
            await update.message.reply_text("❌ Не удалось применить замену. Проверьте формат сообщения.")

    async def handle_add_lesson(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        parsed = self.editor.parse_add_command(text)
        if not parsed['success']:
            await update.message.reply_text(parsed['message'])
            return
        res = self.editor.add_lesson(update.effective_user.id, parsed['day'], parsed['lesson_number'], parsed['subject'], parsed['room'])
        await update.message.reply_text(res['message'])

    async def handle_remove_lesson(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        parsed = self.editor.parse_remove_command(text)
        if not parsed['success']:
            await update.message.reply_text(parsed['message'])
            return
        lesson_num = parsed.get('lesson_number')
        subject = parsed.get('subject')
        res = self.editor.remove_lesson(update.effective_user.id, parsed['day'], lesson_num, subject)
        await update.message.reply_text(res['message'])

    async def handle_schedule_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        user = update.effective_user
        ents = self.rag.parse_question(text)
        day = ents['day']
        lessons = self.get_schedule(user.id, day)
        if not lessons:
            await update.message.reply_text(f"📭 Нет расписания на {day}\n\nЗагрузите расписание через кнопку «📤 Загрузить»")
            return
        answer = self.rag.generate_precise_answer(ents, lessons, day)
        await update.message.reply_text(answer)

    async def show_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        lessons = self.get_schedule(user.id)
        if not lessons:
            await update.message.reply_text("📭 Расписание не загружено.\n\nИспользуйте кнопку «📤 Загрузить» и отправьте файл с расписанием.")
            return
        by_day = {}
        for d, num, _, subj, room, _ in lessons:
            by_day.setdefault(d, []).append((num, subj, room))
        resp = "📅 **Ваше расписание:**\n\n"
        for day in ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']:
            if day in by_day:
                resp += f"*{day}:*\n"
                for num, subj, room in sorted(by_day[day]):
                    room_txt = f" (каб.{room})" if room else ""
                    resp += f"  {num}. {subj}{room_txt}\n"
                resp += "\n"
        await update.message.reply_text(resp)

    async def analyze_tomorrow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%A')
        ru_days = {'Monday': 'Понедельник', 'Tuesday': 'Вторник', 'Wednesday': 'Среда', 
                   'Thursday': 'Четверг', 'Friday': 'Пятница', 'Saturday': 'Суббота', 'Sunday': 'Воскресенье'}
        day_ru = ru_days[tomorrow]
        lessons_db = self.get_schedule(user.id, day_ru)
        if not lessons_db:
            await update.message.reply_text(f"📭 На {day_ru} нет уроков\n\nЗагрузите расписание, чтобы получать аналитику.")
            return
        lessons = [{'day': l[0], 'lesson_number': l[1], 'subject': l[3]} for l in lessons_db]
        analysis = self.analyzer.calculate_day_complexity(lessons)
        resp = f"📊 **Анализ {day_ru}:**\n\n"
        resp += f"⚡ Сложность: {analysis['score']}/10 ({analysis['level']})\n"
        resp += f"📚 Уроков: {analysis['lesson_count']}\n\n"
        resp += "**💡 Рекомендации:**\n"
        for rec in analysis['recommendations']:
            resp += f"• {rec}\n"
        await update.message.reply_text(resp)

    async def export_calendar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        lessons_db = self.get_schedule(user.id)
        if not lessons_db:
            await update.message.reply_text("📭 Нет расписания для экспорта.\n\nСначала загрузите расписание.")
            return
        lessons = []
        for day, num, _, subj, room, _ in lessons_db:
            lessons.append({'day': day, 'lesson_number': num, 'subject': subj, 'room': room})
        ics_content = self.calendar.generate_ics_file(lessons, weeks=4)
        await update.message.reply_document(
            document=InputFile(io.BytesIO(ics_content), filename="schedule.ics"),
            caption="📅 Ваше расписание в календаре\n\nИмпортируйте файл в Google Calendar, Яндекс.Календарь или телефон."
        )

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        doc = update.message.document
        filename = doc.file_name
        ext = filename.split('.')[-1].lower()
        await update.message.reply_text(f"📥 Загружаю {filename}...")
        file = await doc.get_file()
        content = await file.download_as_bytearray()
        if ext in ['xlsx', 'xls']:
            lessons = self.parser.parse_excel(content)
        elif ext == 'pdf':
            lessons = self.parser.parse_pdf(content)
        else:
            await update.message.reply_text("❌ Неподдерживаемый формат.\n\nИспользуйте Excel (.xlsx, .xls) или PDF.")
            return
        if lessons:
            self.save_schedule(user.id, lessons)
            days = set(l['day'] for l in lessons)
            await update.message.reply_text(f"✅ Расписание загружено!\n\n📊 Статистика:\n• Уроков: {len(lessons)}\n• Дней: {len(days)}\n• Дни: {', '.join(days)}")
        else:
            await update.message.reply_text("❌ Не удалось распознать расписание в файле.\n\n💡 Совет: Используйте шаблон с колонками: День, Номер_урока, Предмет, Кабинет")

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        photo = update.message.photo[-1]
        await update.message.reply_text("🔍 Распознаю текст на фото...")
        file = await photo.get_file()
        content = await file.download_as_bytearray()
        lessons = self.parser.parse_image(content)
        if lessons:
            self.save_schedule(user.id, lessons)
            await update.message.reply_text(f"✅ Распознано {len(lessons)} уроков!\n\nПроверьте расписание кнопкой «📅 Расписание»")
        else:
            await update.message.reply_text("❌ Не удалось распознать текст на фото.\n\n💡 Советы:\n• Сфотографируйте при хорошем освещении\n• Держите камеру прямо\n• Используйте Excel файл для лучшего результата")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Ошибка: {context.error}")
        if update and update.effective_message:
            await update.effective_message.reply_text("⚠️ Произошла ошибка. Попробуйте позже.")

# ============= СОЗДАНИЕ БОТА =============
def create_bot() -> Application:
    """Создание и настройка Application"""
    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
    
    bot_instance = TelegramBot()
    app.add_handler(CommandHandler("start", bot_instance.start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_instance.handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, bot_instance.handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, bot_instance.handle_photo))
    app.add_error_handler(bot_instance.error_handler)
    
    return app

# ============= WEBHOOK ENDPOINT (СИНХРОННЫЙ) =============
@flask_app.route('/webhook', methods=['POST'])
def webhook():
    """Синхронный обработчик вебхука с подробным логированием"""
    global telegram_app
    try:
        # Логируем входящий запрос
        logger.info("📨 Получен POST запрос на /webhook")
        
        # Получаем данные
        update_data = request.get_json(force=True)
        logger.info(f"📦 Данные получены: {str(update_data)[:200]}...")
        
        # Проверяем, что telegram_app существует
        if telegram_app is None:
            logger.error("❌ telegram_app не инициализирован!")
            return jsonify({'status': 'error', 'message': 'App not initialized'}), 500
        
        # Проверяем, что у telegram_app есть bot
        if telegram_app.bot is None:
            logger.error("❌ telegram_app.bot не инициализирован!")
            return jsonify({'status': 'error', 'message': 'Bot not initialized'}), 500
        
        # Создаём event loop для обработки в текущем потоке
        logger.info("🔄 Создаём event loop...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Создаём Update объект и обрабатываем
        logger.info("📝 Создаём Update объект...")
        update = Update.de_json(update_data, telegram_app.bot)
        
        logger.info("⚙️ Обрабатываем обновление...")
        loop.run_until_complete(telegram_app.process_update(update))
        
        loop.close()
        logger.info("✅ Обновление обработано успешно")
        return jsonify({'status': 'ok'}), 200
        
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ============= ЗАПУСК =============
if __name__ == '__main__':
    # Инициализация БД
    init_db()
    
    # Создаём Application
    telegram_app = create_bot()
    
    # Настройка вебхука
    webhook_url = os.environ.get("WEBHOOK_URL")
    port = int(os.environ.get("PORT", 5000))
    
    if webhook_url:
        webhook_path = f"{webhook_url}/webhook"
        
        # Устанавливаем вебхук
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(telegram_app.bot.set_webhook(webhook_path))
        loop.close()
        logger.info(f"✅ Webhook установлен: {webhook_path}")
    else:
        logger.warning("⚠️ WEBHOOK_URL не задан, вебхук не установлен")
    
    logger.info(f"🌐 Flask сервер запущен на порту {port}")
    
    # Запускаем Flask
    flask_app.run(host="0.0.0.0", port=port)
