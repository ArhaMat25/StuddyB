import os
import logging
import sqlite3
import re
import io
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ============= КОНФИГУРАЦИЯ =============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = "8573998335:AAENV4S0UhOUAmc3RpzEeFDLuModI36aqhM"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Пути
DATA_DIR = Path("bot_data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot_data.db"

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
        df = pd.read_excel(io.BytesIO(content))
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
            return match.group(1).strip(), match.group(2).strip()
        match_del = re.search(r'не будет\s+([^\s,]+(?:\s+[^\s,]+)*)', text)
        if match_del:
            return match_del.group(1).strip(), None
        return None, None

    def _extract_classroom(self, text: str):
        match = re.search(r'кабинет[е]?\s*(\d+)', text)
        return match.group(1) if match else None

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
                    room = f" (каб. {les[3]})" if les[3] else ""
                    return f"📚 {day}, {entities['lesson_number']}-й урок: {les[2]}{room}"
            return f"❌ {entities['lesson_number']}-го урока нет в расписании"
        if entities['subject']:
            for les in lessons:
                if entities['subject'] in les[2].lower():
                    return f"📖 {entities['subject']} на {day} — {les[1]}-й урок"
            return f"❌ {entities['subject']} не найден на {day}"
        resp = f"📅 Расписание на {day}:\n"
        for les in sorted(lessons, key=lambda x: x[1]):
            room = f" (каб. {les[3]})" if les[3] else ""
            resp += f"{les[1]}. {les[2]}{room}\n"
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
    def calculate_day_complexity(self, lessons: list) -> dict:
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

analyzer = DayComplexityAnalyzer()

# ============= ЭКСПОРТ В КАЛЕНДАРЬ =============
def generate_ics_file(lessons: list) -> bytes:
    from icalendar import Calendar, Event
    cal = Calendar()
    cal.add('version', '2.0')
    start = datetime.now()
    for les in lessons:
        date = start + timedelta(days=7)
        event = Event()
        event.add('summary', les[2])
        event.add('dtstart', datetime(date.year, date.month, date.day, 8, 0))
        event.add('dtend', datetime(date.year, date.month, date.day, 8, 45))
        cal.add_component(event)
    return cal.to_ical()

# ============= ОБРАБОТЧИКИ КОМАНД =============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📅 Расписание"), KeyboardButton("📤 Загрузить файл")],
        [KeyboardButton("➕ Добавить урок"), KeyboardButton("➖ Удалить урок")],
        [KeyboardButton("📊 Оценить завтра"), KeyboardButton("📅 Экспорт календаря")],
        [KeyboardButton("ℹ️ Помощь")]
    ]
    await update.message.reply_text(
        "👋 Привет! Я бот для расписания.\n\n"
        "📌 **Что я умею:**\n"
        "• Загружать расписание из Excel файла\n"
        "• Отвечать на вопросы о расписании\n"
        "• Автоматически обрабатывать замены уроков\n"
        "• Добавлять и удалять уроки\n"
        "• Оценивать сложность дня\n"
        "• Экспортировать расписание в календарь\n\n"
        "📌 **Примеры:**\n"
        "• 'Какой завтра первый урок?'\n"
        "• 'Вместо физики будет история'\n"
        "• 'Добавь урок в понедельник 3-м уроком математику'",
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
    user = update.effective_user

    # Кнопки меню
    if text == "📅 Расписание":
        await show_schedule(update, context)
        return
    if text == "📤 Загрузить файл":
        await update.message.reply_text("📎 Отправьте Excel файл с расписанием.\n\nКолонки: День, Номер_урока, Предмет, Кабинет")
        return
    if text == "➕ Добавить урок":
        await update.message.reply_text("➕ **Добавление урока:**\n\nНапишите в формате:\n`Добавь урок в [день] [номер] уроком [предмет]`\n\nПример: `Добавь урок в понедельник 3-м уроком математику в 201 кабинете`")
        return
    if text == "➖ Удалить урок":
        await update.message.reply_text("➖ **Удаление урока:**\n\nНапишите в формате:\n`Удали урок в [день] [номер] урок`\n\nПример: `Удали урок в понедельник 3-й урок`")
        return
    if text == "📊 Оценить завтра":
        await analyze_tomorrow(update, context)
        return
    if text == "📅 Экспорт календаря":
        await export_calendar(update, context)
        return
    if text == "ℹ️ Помощь":
        await update.message.reply_text(
            "📌 **Как пользоваться ботом:**\n\n"
            "1. **Загрузить расписание:** Отправьте Excel файл (.xlsx) с колонками:\n"
            "   • День (Понедельник, Вторник...)\n"
            "   • Номер_урока (1, 2, 3...)\n"
            "   • Предмет (Математика, Физика...)\n"
            "   • Кабинет (201, 301...)\n\n"
            "2. **Вопросы о расписании:**\n"
            "   • 'Какой завтра первый урок?'\n"
            "   • 'В каком кабинете физика?'\n\n"
            "3. **Замены:** Просто напишите сообщение вроде:\n"
            "   'Завтра 5-м уроком вместо физики будет история в 302 кабинете'\n\n"
            "4. **Управление:** Добавляйте и удаляйте уроки, экспортируйте в календарь."
        )
        return

    # Замена уроков (автоматическая)
    data = replacement_parser.parse_replacement_message(text)
    if data['success']:
        if data['lesson_number'] and data['new_subject']:
            res = schedule_editor.replace_lesson(user.id, data['day'], data['lesson_number'], data['new_subject'], data['classroom'] or '')
            await update.message.reply_text(res['message'])
        elif data['old_subject'] and data['is_cancellation']:
            res = schedule_editor.remove_lesson(user.id, data['day'], subject=data['old_subject'])
            await update.message.reply_text(res['message'])
        else:
            await update.message.reply_text("❌ Не удалось распознать замену.\n\nПример: 'Вместо физики будет история'")
        return

    # Добавление урока
    if 'добавь' in text.lower() and 'урок' in text.lower():
        parsed = schedule_editor.parse_add_command(text)
        if parsed['success']:
            res = schedule_editor.add_lesson(user.id, parsed['day'], parsed['lesson_number'], parsed['subject'], parsed['room'])
            await update.message.reply_text(res['message'])
        else:
            await update.message.reply_text(parsed['message'])
        return

    # Удаление урока
    if ('удали' in text.lower() or 'отмени' in text.lower()) and 'урок' in text.lower():
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

    # Если ничего не подошло
    await update.message.reply_text(
        "Я не понял команду.\n\n"
        "Используйте кнопки меню или напишите:\n"
        "• 'Какой завтра первый урок?'\n"
        "• 'Вместо физики будет история'\n"
        "• 'Добавь урок в понедельник 3-м уроком математику'"
    )

async def analyze_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%A')
    ru_days = {'Monday': 'Понедельник', 'Tuesday': 'Вторник', 'Wednesday': 'Среда', 
               'Thursday': 'Четверг', 'Friday': 'Пятница', 'Saturday': 'Суббота', 'Sunday': 'Воскресенье'}
    day_ru = ru_days[tomorrow]
    lessons_db = get_schedule(user.id, day_ru)
    if not lessons_db:
        await update.message.reply_text(f"📭 На {day_ru} нет уроков")
        return
    lessons = [{'day': l[0], 'lesson_number': l[1], 'subject': l[2]} for l in lessons_db]
    analysis = analyzer.calculate_day_complexity(lessons)
    resp = f"📊 **Анализ {day_ru}:**\n\n"
    resp += f"⚡ Сложность: {analysis['score']}/10 ({analysis['level']})\n"
    resp += f"📚 Уроков: {analysis['lesson_count']}\n\n"
    resp += "**💡 Рекомендации:**\n"
    for rec in analysis['recommendations']:
        resp += f"• {rec}\n"
    await update.message.reply_text(resp)

async def export_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lessons_db = get_schedule(user.id)
    if not lessons_db:
        await update.message.reply_text("📭 Нет расписания для экспорта.\n\nСначала загрузите расписание.")
        return
    ics_content = generate_ics_file(lessons_db)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(ics_content), filename="schedule.ics"),
        caption="📅 Ваше расписание в календаре"
    )

# ============= ЗАПУСК =============
def run_bot():
    logger.info("🚀 Запуск Telegram бота...")
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не задан!")
        return
    
    init_db()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    logger.info("✅ Бот запущен и ждёт сообщения")
    app.run_polling(poll_interval=1.0, timeout=60, drop_pending_updates=True)

if __name__ == '__main__':
    run_bot()
