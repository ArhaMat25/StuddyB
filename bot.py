import os
import logging
import asyncio
import nest_asyncio      # обязательно для предотвращения ошибок event loop
import sqlite3
import re
import io
import uuid
import base64
import requests
from pathlib import Path
from datetime import datetime, timedelta, time
from typing import Optional, Dict, List

import pandas as pd
import pdfplumber
import pytesseract
from PIL import Image
from icalendar import Calendar, Event
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Разрешаем вложенные циклы событий (необходимо для python-telegram-bot)
nest_asyncio.apply()

# ============= КОНФИГУРАЦИЯ =============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = "8573998335:AAENV4S0UhOUAmc3RpzEeFDLuModI36aqhM"

GIGACHAT_CLIENT_ID = os.environ.get("GIGACHAT_CLIENT_ID", "019ac450-7c0b-7686-a4ec-e979dd4fa0f5")
GIGACHAT_CLIENT_SECRET = os.environ.get("GIGACHAT_CLIENT_SECRET", "8dc579fc-56ee-49bd-b8cd-a0cd3fe4ae56")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = Path("bot_data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot_data.db"

# ============= БАЗА ДАННЫХ (все таблицы) =============
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
    conn.execute('''
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_name TEXT,
            file_type TEXT,
            file_size INTEGER,
            upload_date DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            user_id INTEGER PRIMARY KEY,
            morning_reminder BOOLEAN DEFAULT 1
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS replacement_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            lesson_number INTEGER,
            old_subject TEXT,
            new_subject TEXT,
            classroom TEXT,
            is_cancellation BOOLEAN,
            replacement_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            original_message TEXT
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

def save_conversation(user_id: int, message: str, response: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO conversations (user_id, message, response) VALUES (?,?,?)", (user_id, message, response))
    conn.commit()
    conn.close()

def save_uploaded_file(user_id: int, file_name: str, file_type: str, file_size: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO uploaded_files (user_id, file_name, file_type, file_size) VALUES (?,?,?,?)", (user_id, file_name, file_type, file_size))
    conn.commit()
    conn.close()

def get_users_with_morning_reminders():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM notifications WHERE morning_reminder = 1")
    users = [row[0] for row in cur.fetchall()]
    conn.close()
    return users

# ============= ПАРСЕРЫ ФАЙЛОВ =============
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

parser = ScheduleParser()

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
        self.subject_keywords = {
            'математика': ['математика', 'матеша', 'алгебра', 'геометрия', 'мат'],
            'физика': ['физика', 'физ'],
            'химия': ['химия', 'хим'],
            'биология': ['биология', 'био'],
            'история': ['история', 'ист'],
            'география': ['география', 'гео'],
            'английский': ['английский', 'англ', 'english'],
            'русский': ['русский', 'русский язык', 'яз'],
            'литература': ['литература', 'литра'],
            'информатика': ['информатика', 'инфа', 'программирование'],
            'физкультура': ['физкультура', 'физра', 'спорт'],
            'обществознание': ['обществознание', 'общество']
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

    def _extract_day(self, text: str):
        if 'сегодня' in text:
            return self._get_day_offset(0)
        if 'завтра' in text:
            return self._get_day_offset(1)
        for key, val in self.days_mapping.items():
            if key in text:
                return val
        return None

    def _extract_lesson_number(self, text: str):
        match = re.search(r'(\d+)[-ыи]?м?\s+урок', text)
        return int(match.group(1)) if match else None

    def _extract_subjects(self, text: str):
        match = re.search(r'вместо\s+([^\s,]+(?:\s+[^\s,]+)*)\s+будет\s+([^\s,]+(?:\s+[^\s,]+)*)', text)
        if match:
            old = self._normalize_subject(match.group(1))
            new = self._normalize_subject(match.group(2))
            return old, new
        match_del = re.search(r'не будет\s+([^\s,]+(?:\s+[^\s,]+)*)', text)
        if match_del:
            old = self._normalize_subject(match_del.group(1))
            return old, None
        return None, None

    def _extract_classroom(self, text: str):
        match = re.search(r'кабинет[е]?\s*(\d+)', text)
        return match.group(1) if match else None

    def _normalize_subject(self, subject_text: str) -> str:
        subj_lower = subject_text.lower().strip()
        for subject, keywords in self.subject_keywords.items():
            for kw in keywords:
                if kw in subj_lower:
                    return subject
        return subject_text

    def _get_day_offset(self, offset: int):
        days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        today = datetime.now().weekday()
        return days[(today + offset) % 7]

replacement_parser = ReplacementParser()

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

    def _extract_day(self, text: str):
        days = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']
        for d in days:
            if d in text:
                return d.capitalize()
        return None

    def _extract_number(self, text: str):
        match = re.search(r'(\d+)[-ыи]?м?\s+урок', text)
        return int(match.group(1)) if match else None

    def _extract_subject(self, text: str):
        subjects = ['математика', 'физика', 'химия', 'биология', 'история', 'география',
                   'английский', 'русский', 'литература', 'информатика', 'физкультура']
        for subj in subjects:
            if subj in text:
                return subj
        return None

    def _extract_room(self, text: str):
        match = re.search(r'кабинет[е]?\s*(\d+)', text)
        return match.group(1) if match else ''

schedule_editor = ScheduleEditor(DB_PATH)

# ============= RAG СИСТЕМА =============
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

    def _extract_day(self, text: str):
        days = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']
        for d in days:
            if d in text:
                return d.capitalize()
        return None

    def _extract_lesson_num(self, text: str):
        match = re.search(r'(\d+)[-ыи]?й?\s*урок', text)
        if not match:
            match = re.search(r'урок\s*(\d+)', text)
        return int(match.group(1)) if match else None

    def _extract_subject(self, text: str):
        subjects = ['математика', 'физика', 'химия', 'биология', 'история', 'география',
                   'английский', 'русский', 'литература', 'информатика']
        for s in subjects:
            if s in text:
                return s
        return None

    def _get_today(self):
        days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        return days[datetime.now().weekday()]

rag_system = ScheduleRAGSystem()

# ============= АНАЛИЗАТОР СЛОЖНОСТИ =============
class DayComplexityAnalyzer:
    def __init__(self):
        self.weights = {'обычный_урок': 1, 'контрольная': 2, 'лабораторная': 1.5, 'экзамен': 3, 'зачет': 1.5}
        self.subject_difficulty = {
            'математика': 1.2, 'физика': 1.3, 'химия': 1.2, 'русский': 1.1, 'литература': 1.0,
            'история': 1.0, 'биология': 1.1, 'география': 1.0, 'английский': 1.1, 'информатика': 1.2,
            'алгебра': 1.3, 'геометрия': 1.3, 'обществознание': 1.0
        }

    def detect_lesson_type(self, subject: str, teacher: str = "") -> str:
        subj_lower = subject.lower()
        if any(w in subj_lower for w in ['контрольная', 'к/р', 'тест', 'проверочная']):
            return 'контрольная'
        if any(w in subj_lower for w in ['лабораторная', 'лаб', 'практикум']):
            return 'лабораторная'
        if any(w in subj_lower for w in ['экзамен', 'зачет']):
            return 'экзамен' if 'экзамен' in subj_lower else 'зачет'
        return 'обычный_урок'

    def calculate_day_complexity(self, lessons: list) -> dict:
        if not lessons:
            return {'score': 0, 'level': 'нет уроков', 'recommendations': ['😊 Отдыхайте'], 'lesson_count': 0}
        total_score = 0
        lesson_count = len(lessons)
        test_count = 0
        difficult_subjects = []
        for les in lessons:
            subject = les.get('subject', '')
            lesson_type = self.detect_lesson_type(subject, les.get('teacher', ''))
            base_weight = self.weights.get(lesson_type, 1)
            difficulty_multiplier = 1.0
            for subj, mul in self.subject_difficulty.items():
                if subj in subject.lower():
                    difficulty_multiplier = mul
                    break
            total_score += base_weight * difficulty_multiplier
            if lesson_type == 'контрольная':
                test_count += 1
            if difficulty_multiplier >= 1.2:
                difficult_subjects.append(subject)
        base_score = min(10, lesson_count * 0.8 + test_count * 1.5)
        difficulty_bonus = len(difficult_subjects) * 0.5
        normalized_score = min(10, round(base_score + difficulty_bonus, 1))
        if normalized_score <= 3:
            level = 'лёгкий'
        elif normalized_score <= 6:
            level = 'средний'
        elif normalized_score <= 8:
            level = 'сложный'
        else:
            level = 'очень сложный'
        recommendations = self._generate_recommendations(normalized_score, lesson_count, test_count, difficult_subjects)
        return {'score': normalized_score, 'level': level, 'lesson_count': lesson_count,
                'test_count': test_count, 'difficult_subjects': difficult_subjects, 'recommendations': recommendations}

    def _generate_recommendations(self, score, lesson_count, test_count, difficult_subjects):
        recs = []
        if score >= 8:
            recs.extend(["🔥 Это будет напряженный день!", "📚 Начни готовиться заранее", "⏰ Ложись спать пораньше", "🍎 Не забудь завтрак", "💧 Бери воду"])
        elif score >= 6:
            recs.extend(["📖 День потребует сосредоточенности", "🕔 Сделай домашку до 19:00", "🎵 Выдели время на отдых", "📋 Составь план"])
        elif score >= 4:
            recs.extend(["📝 День средней нагрузки", "🕠 Можешь делать домашку до 20:00", "🚶 Гуляй на свежем воздухе"])
        else:
            recs.extend(["😊 Легкий день - отличная возможность!", "📚 Закончи домашку быстро", "🎯 Займись чем-то полезным", "👥 Проведи время с семьёй"])
        if test_count >= 2:
            recs.append("✏️ Целых 2 контрольные! Повтори материалы вечером")
        elif test_count == 1:
            recs.append("📝 Завтра контрольная - удели ей особое внимание")
        if difficult_subjects:
            recs.append(f"🎯 Сложные предметы: {', '.join(difficult_subjects[:2])} - повтори первыми")
        return recs

analyzer = DayComplexityAnalyzer()

# ============= ЭКСПОРТ В КАЛЕНДАРЬ (полный) =============
class CalendarExporter:
    def __init__(self):
        self.default_lesson_times = {1: ("08:00","08:45"),2: ("09:00","09:45"),3: ("10:00","10:45"),
                                     4: ("11:00","11:45"),5: ("12:00","12:45"),6: ("13:00","13:45"),
                                     7: ("14:00","14:45"),8: ("15:00","15:45")}
        self.days_mapping = {'Понедельник':0,'Вторник':1,'Среда':2,'Четверг':3,'Пятница':4,'Суббота':5,'Воскресенье':6}

    def get_lesson_time(self, lesson):
        if lesson.get('start_time'):
            parts = lesson['start_time'].split('-')
            if len(parts)==2:
                try:
                    start_h,start_m = map(int,parts[0].strip().split(':'))
                    end_h,end_m = map(int,parts[1].strip().split(':'))
                    return (datetime.min.replace(hour=start_h, minute=start_m).time(),
                            datetime.min.replace(hour=end_h, minute=end_m).time())
                except: pass
        lesson_num = lesson.get('lesson_number',1)
        if lesson_num in self.default_lesson_times:
            start_str, end_str = self.default_lesson_times[lesson_num]
            sh, sm = map(int, start_str.split(':'))
            eh, em = map(int, end_str.split(':'))
            return (datetime.min.replace(hour=sh, minute=sm).time(),
                    datetime.min.replace(hour=eh, minute=em).time())
        return (datetime.min.replace(hour=8, minute=0).time(),
                datetime.min.replace(hour=8, minute=45).time())

    def generate_ics_file(self, lessons, weeks=4):
        cal = Calendar()
        cal.add('version','2.0')
        cal.add('prodid','-//School Schedule Bot//RU')
        cal.add('name','Расписание уроков')
        start = datetime.now()
        start_of_week = start - timedelta(days=start.weekday())
        next_week_start = start_of_week + timedelta(days=7)
        for week in range(weeks):
            week_dates = {}
            for day_name, day_offset in self.days_mapping.items():
                week_dates[day_name] = next_week_start + timedelta(days=day_offset) + timedelta(weeks=week)
            for lesson in lessons:
                day_name = lesson['day']
                if day_name in week_dates:
                    date = week_dates[day_name]
                    start_t, end_t = self.get_lesson_time(lesson)
                    start_dt = datetime.combine(date, start_t)
                    end_dt = datetime.combine(date, end_t)
                    event = Event()
                    event.add('summary', lesson['subject'])
                    event.add('dtstart', start_dt)
                    event.add('dtend', end_dt)
                    event.add('description', f"Урок №{lesson['lesson_number']}\nКабинет: {lesson.get('room','не указан')}")
                    if lesson.get('room'):
                        event.add('location', f"Кабинет {lesson['room']}")
                    alarm = Event()
                    alarm.add('action','DISPLAY')
                    alarm.add('description',f'Скоро урок: {lesson["subject"]}')
                    alarm.add('trigger',timedelta(minutes=-15))
                    event.add_component(alarm)
                    cal.add_component(event)
        return cal.to_ical()

    def generate_daily_reminders(self, lessons, days=30):
        cal = Calendar()
        cal.add('version','2.0')
        cal.add('prodid','-//School Schedule Reminders//RU')
        cal.add('name','Напоминания о расписании')
        today = datetime.now().date()
        for d in range(days):
            current_date = today + timedelta(days=d)
            day_name_ru = list(self.days_mapping.keys())[current_date.weekday()]
            day_lessons = [l for l in lessons if l['day'] == day_name_ru]
            if day_lessons:
                reminder = Event()
                reminder.add('summary','📚 Напоминание о расписании')
                schedule_text = "📅 Сегодня:\n"
                for les in sorted(day_lessons, key=lambda x: x['lesson_number']):
                    st, et = self.get_lesson_time(les)
                    schedule_text += f"• {st.strftime('%H:%M')} - {les['subject']}"
                    if les.get('room'): schedule_text += f" ({les['room']})"
                    schedule_text += "\n"
                reminder.add('description', schedule_text)
                reminder.add('dtstart', datetime.combine(current_date, datetime.min.replace(hour=7, minute=0).time()))
                reminder.add('dtend', datetime.combine(current_date, datetime.min.replace(hour=7, minute=15).time()))
                alarm = Event()
                alarm.add('action','DISPLAY')
                alarm.add('description','Посмотри расписание на сегодня')
                alarm.add('trigger',timedelta(minutes=0))
                reminder.add_component(alarm)
                cal.add_component(reminder)
        return cal.to_ical()

calendar_exporter = CalendarExporter()

# ============= GIGACHAT =============
class GigaChatService:
    def __init__(self):
        self.access_token = None
        self.expires_at = None

    def _get_token(self):
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

    def send_message(self, text):
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
            logger.error(f"GigaChat error: {e}")
        return "Не удалось получить ответ от AI"

giga = GigaChatService()

# ============= ОСНОВНЫЕ ОБРАБОТЧИКИ =============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📚 Помощь с учебой"), KeyboardButton("🤖 Задать вопрос")],
        [KeyboardButton("📅 Моё расписание"), KeyboardButton("📤 Загрузить расписание")],
        [KeyboardButton("📋 Скачать шаблон"), KeyboardButton("📊 Оценить завтра")],
        [KeyboardButton("➕ Добавить урок"), KeyboardButton("➖ Удалить урок")],
        [KeyboardButton("📅 Экспорт в календарь"), KeyboardButton("📈 Статистика")],
        [KeyboardButton("ℹ️ О боте")]
    ]
    await update.message.reply_text(
        "👋 Привет! Я бот-помощник для расписания.\n\n"
        "🎯 **Что я умею:**\n"
        "• Отвечать на вопросы по учебе (GigaChat)\n"
        "• Загружать расписание из Excel/PDF/фото\n"
        "• **Автоматически обрабатывать замены уроков**\n"
        "• Добавлять и удалять уроки\n"
        "• Отвечать на вопросы о расписании (RAG)\n"
        "• Оценивать сложность дня\n"
        "• Экспортировать расписание в календарь\n"
        "• Присылать утренние напоминания\n\n"
        "📎 **Поддерживаемые форматы:** Excel, PDF, фото\n\n"
        "💡 **Примеры:**\n"
        "• 'Какой завтра первый урок?'\n"
        "• 'Вместо физики будет история'\n"
        "• 'Добавь урок в понедельник 3-м уроком математику'",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lessons = get_schedule(user.id)
    if not lessons:
        await update.message.reply_text("📭 Расписание не загружено. Используйте кнопку «📤 Загрузить расписание»")
        return
    by_day = {}
    for d, num, _, subj, room, _ in lessons:
        by_day.setdefault(d, []).append((num, subj, room))
    resp = "📅 **Ваше расписание:**\n\n"
    for day in ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота']:
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
    lessons = []
    if ext in ['xlsx','xls']:
        lessons = parser.parse_excel(content)
    elif ext == 'pdf':
        lessons = parser.parse_pdf(content)
    else:
        await update.message.reply_text("❌ Неподдерживаемый формат. Используйте Excel, PDF или фото.")
        return
    if lessons:
        save_schedule(user.id, lessons)
        save_uploaded_file(user.id, filename, ext, len(content))
        days = set(l['day'] for l in lessons)
        await update.message.reply_text(f"✅ Загружено {len(lessons)} уроков!\n📅 Дни: {', '.join(days)}")
    else:
        await update.message.reply_text("❌ Не удалось распознать расписание.\n\nДля Excel нужны колонки: День, Номер_урока, Предмет, Кабинет")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    await update.message.reply_text("📥 Загружаю фото...\n🔍 Распознаю текст...")
    file = await photo.get_file()
    content = await file.download_as_bytearray()
    lessons = parser.parse_image(content)
    if lessons:
        save_schedule(user.id, lessons)
        save_uploaded_file(user.id, "schedule_photo.jpg", "jpg", len(content))
        days = set(l['day'] for l in lessons)
        await update.message.reply_text(f"✅ Распознано {len(lessons)} уроков!\n📅 Дни: {', '.join(days)}")
    else:
        await update.message.reply_text("❌ Не удалось распознать расписание на фото.\nПопробуйте сфотографировать чётче или используйте Excel.")

async def send_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = {
            'День': ['Понедельник','Понедельник','Вторник','Вторник'],
            'Номер_урока': [1,2,1,2],
            'Время': ['08:00-08:45','09:00-09:45','08:00-08:45','09:00-09:45'],
            'Предмет': ['Математика','Русский язык','Физика','Химия'],
            'Кабинет': ['201','105','301','208'],
            'Учитель': ['Иванова А.П.','Петрова И.С.','Сидоров В.П.','Козлова М.И.']
        }
        df = pd.DataFrame(data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Расписание', index=False)
        output.seek(0)
        await update.message.reply_document(
            document=InputFile(output, filename='шаблон_расписания.xlsx'),
            caption="📋 Шаблон Excel. Заполните и загрузите боту."
        )
    except Exception as e:
        logger.error(f"Template error: {e}")
        await update.message.reply_text("❌ Ошибка создания шаблона")

async def analyze_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ru_days = {'Monday':'Понедельник','Tuesday':'Вторник','Wednesday':'Среда','Thursday':'Четверг',
               'Friday':'Пятница','Saturday':'Суббота','Sunday':'Воскресенье'}
    tomorrow = (datetime.now()+timedelta(days=1)).strftime('%A')
    day_ru = ru_days[tomorrow]
    lessons_db = get_schedule(user.id, day_ru)
    if not lessons_db:
        await update.message.reply_text(f"📭 На {day_ru} нет уроков")
        return
    lessons = [{'day':l[0], 'lesson_number':l[1], 'subject':l[3], 'room':l[4], 'teacher':l[5]} for l in lessons_db]
    analysis = analyzer.calculate_day_complexity(lessons)
    resp = f"📊 **Анализ {day_ru}:**\n\n⚡ Сложность: {analysis['score']}/10 ({analysis['level']})\n📚 Уроков: {analysis['lesson_count']}\n\n**💡 Рекомендации:**\n"
    for rec in analysis['recommendations']:
        resp += f"• {rec}\n"
    await update.message.reply_text(resp)

async def export_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📅 Экспорт расписания (4 недели)")],
        [KeyboardButton("⏰ Ежедневные напоминания")],
        [KeyboardButton("🔙 Назад")]
    ]
    await update.message.reply_text(
        "Выберите тип экспорта:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def handle_calendar_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    lessons_db = get_schedule(user.id)
    if not lessons_db:
        await update.message.reply_text("❌ Сначала загрузите расписание")
        return
    lessons = [{'day':l[0], 'lesson_number':l[1], 'subject':l[3], 'room':l[4], 'teacher':l[5], 'start_time':l[2]} for l in lessons_db]
    if text == "📅 Экспорт расписания (4 недели)":
        ics = calendar_exporter.generate_ics_file(lessons, weeks=4)
        caption = "📅 Расписание на 4 недели. Добавьте в календарь."
        fname = "schedule_4weeks.ics"
    elif text == "⏰ Ежедневные напоминания":
        ics = calendar_exporter.generate_daily_reminders(lessons, days=30)
        caption = "⏰ Ежедневные напоминания о расписании на 30 дней."
        fname = "daily_reminders.ics"
    else:
        return
    await update.message.reply_document(
        document=InputFile(io.BytesIO(ics), filename=fname),
        caption=caption
    )

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM conversations WHERE user_id=?", (user.id,))
    conv_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM schedule WHERE user_id=?", (user.id,))
    lessons_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM uploaded_files WHERE user_id=?", (user.id,))
    files_count = cur.fetchone()[0]
    conn.close()
    resp = f"📊 **Ваша статистика:**\n\n💬 Диалогов: {conv_count}\n📅 Уроков: {lessons_count}\n📎 Файлов: {files_count}"
    if lessons_count:
        lessons = get_schedule(user.id)
        subjects = set(l[3] for l in lessons)
        days = set(l[0] for l in lessons)
        resp += f"\n📚 Предметов: {len(subjects)}\n📅 Дней с уроками: {len(days)}"
    await update.message.reply_text(resp)

async def about_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Бот для управления расписанием**\n\n"
        "Версия 2.0 (полная).\nРаботает на Render.\n\n"
        "Функции: загрузка Excel/PDF/фото, автоматические замены, RAG-вопросы, "
        "анализ сложности, экспорт в календарь, GigaChat, утренние напоминания."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user

    # Кнопки
    if text == "📚 Помощь с учебой":
        await update.message.reply_text("Напишите вопрос по предмету, я постараюсь ответить с помощью GigaChat.")
        return
    if text == "🤖 Задать вопрос":
        await update.message.reply_text("Задайте ваш вопрос:")
        return
    if text == "📅 Моё расписание":
        await show_schedule(update, context)
        return
    if text == "📤 Загрузить расписание":
        await update.message.reply_text("Отправьте Excel, PDF или фото расписания.")
        return
    if text == "📋 Скачать шаблон":
        await send_template(update, context)
        return
    if text == "📊 Оценить завтра":
        await analyze_tomorrow(update, context)
        return
    if text == "➕ Добавить урок":
        await update.message.reply_text("Напишите: Добавь урок в [день] [номер] уроком [предмет] [кабинет]\nПример: Добавь урок в понедельник 3-м уроком математику в 201")
        return
    if text == "➖ Удалить урок":
        await update.message.reply_text("Напишите: Удали урок в [день] [номер] урок\nПример: Удали урок в понедельник 3-й урок")
        return
    if text == "📅 Экспорт в календарь":
        await export_calendar(update, context)
        return
    if text == "📈 Статистика":
        await show_stats(update, context)
        return
    if text == "ℹ️ О боте":
        await about_bot(update, context)
        return
    if text == "⏰ Ежедневные напоминания" or text == "📅 Экспорт расписания (4 недели)":
        await handle_calendar_export(update, context)
        return
    if text == "🔙 Назад":
        await start(update, context)
        return

    # Замена уроков
    data = replacement_parser.parse_replacement_message(text)
    if data['success']:
        if data['lesson_number'] and data['new_subject']:
            res = schedule_editor.replace_lesson(user.id, data['day'], data['lesson_number'], data['new_subject'], data['classroom'] or '')
            await update.message.reply_text(res['message'])
        elif data['old_subject'] and data['is_cancellation']:
            res = schedule_editor.remove_lesson(user.id, data['day'], subject=data['old_subject'])
            await update.message.reply_text(res['message'])
        else:
            await update.message.reply_text("❌ Не удалось распознать замену.\nПример: 'Вместо физики будет история'")
        return

    # Добавление урока
    if any(k in text.lower() for k in ['добавь', 'внеси', 'запиши']) and 'урок' in text.lower():
        parsed = schedule_editor.parse_add_command(text)
        if parsed['success']:
            res = schedule_editor.add_lesson(user.id, parsed['day'], parsed['lesson_number'], parsed['subject'], parsed['room'])
            await update.message.reply_text(res['message'])
        else:
            await update.message.reply_text(parsed['message'])
        return

    # Удаление урока
    if any(k in text.lower() for k in ['удали', 'отмени', 'убери']) and 'урок' in text.lower():
        parsed = schedule_editor.parse_remove_command(text)
        if parsed['success']:
            res = schedule_editor.remove_lesson(user.id, parsed['day'], parsed.get('lesson_number'), parsed.get('subject'))
            await update.message.reply_text(res['message'])
        else:
            await update.message.reply_text(parsed['message'])
        return

    # Вопрос о расписании
    if any(k in text.lower() for k in ['урок', 'расписание', 'кабинет', 'когда', 'сколько', 'первый', 'второй']):
        ents = rag_system.parse_question(text)
        day = ents['day']
        lessons = get_schedule(user.id, day)
        if not lessons:
            await update.message.reply_text(f"📭 Нет расписания на {day}")
            return
        answer = rag_system.generate_precise_answer(ents, lessons, day)
        await update.message.reply_text(answer)
        return

    # Обычный вопрос -> GigaChat
    await update.message.reply_chat_action(action="typing")
    resp = giga.send_message(text)
    if resp:
        save_conversation(user.id, text, resp)
        await update.message.reply_text(resp)
    else:
        await update.message.reply_text("❌ Не удалось получить ответ. Попробуйте позже.")

async def send_morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    users = get_users_with_morning_reminders()
    if not users:
        return
    today_day = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'][datetime.now().weekday()]
    for user_id in users:
        lessons = get_schedule(user_id, today_day)
        if not lessons:
            continue
        msg = f"🌅 **Доброе утро!** ☀️\n\n📅 Расписание на сегодня ({today_day}):\n"
        for day, num, start, subj, room, teacher in sorted(lessons, key=lambda x: x[1]):
            time_str = f"🕒 {start}" if start else f"{num}."
            room_str = f" 🚪 {room}" if room else ""
            msg += f"{time_str} {subj}{room_str}\n"
        await context.bot.send_message(chat_id=user_id, text=msg)
        await asyncio.sleep(0.1)

# ============= ЗАПУСК =============
async def run_bot():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if app.job_queue:
        app.job_queue.run_daily(send_morning_reminder, time=time(hour=7, minute=0, second=0), days=tuple(range(7)))
        logger.info("⏰ Утренние напоминания настроены на 7:00")
    else:
        logger.warning("JobQueue недоступна")

    logger.info("✅ Бот запущен")
    await app.run_polling(poll_interval=1.0, timeout=60, drop_pending_updates=True)

if __name__ == '__main__':
    asyncio.run(run_bot())
