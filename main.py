import os
import sys
import logging
from datetime import datetime, timedelta
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Считываем токен из переменных окружения хостинга
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    sys.exit("ОШИБКА: Переменная окружения BOT_TOKEN не задана!")

# Путь к директории и файлу базы данных
DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "income_tracker.db")

# Создаем папку data, если её ещё нет
os.makedirs(DATA_DIR, exist_ok=True)

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# ------------------- РАБОТА С БАЗОЙ ДАННЫХ -------------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                hourly_rate REAL DEFAULT 0.0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS work_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                hours REAL,
                rate_at_time REAL,
                total_earned REAL,
                entry_date DATE,
                period_type TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)
        await db.commit()

async def get_user_rate(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT hourly_rate FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0.0

async def set_user_rate(user_id: int, rate: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, hourly_rate) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET hourly_rate = ?",
            (user_id, rate, rate)
        )
        await db.commit()

async def add_work_hours(user_id: int, hours: float) -> tuple[float, float]:
    rate = await get_user_rate(user_id)
    total_earned = hours * rate
    today = datetime.now()
    
    # 1-15 число -> авансовый период, 16-31 число -> основной период
    period_type = 'advance' if today.day <= 15 else 'main'
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO work_entries 
               (user_id, hours, rate_at_time, total_earned, entry_date, period_type) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, hours, rate, total_earned, today.strftime("%Y-%m-%d"), period_type)
        )
        await db.commit()
    return total_earned, rate

# ------------------- ХЕНДЛЕРЫ КОМАНД -------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я твой хранитель дохода.\n\n"
        "Команды:\n"
        "• `!зпчас {число}` — установить/изменить часовую ставку (например: !зпчас 189)\n"
        "• `+{число}` — добавить отработанные часы (например: +12 или +8.5)\n"
        "• `!стата` — посмотреть статистику за последние 3 месяца"
    )

@dp.message(F.text.startswith("!зпчас"))
async def process_set_rate(message: types.Message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError
        new_rate = float(parts[1].replace(",", "."))
        if new_rate < 0:
            return await message.answer("Ставка не может быть отрицательной!")
        
        await set_user_rate(message.from_user.id, new_rate)
        await message.answer(f"✅ Ставка успешно обновлена: **{new_rate:.2f} руб./час**")
    except ValueError:
        await message.answer("Ошибка! Используй формат: `!зпчас 189` или `!зпчас 250.5`")

@dp.message(F.text.startswith("+"))
async def process_add_hours(message: types.Message):
    text = message.text[1:].strip().replace(",", ".")
    try:
        hours = float(text)
        if hours <= 0:
            return await message.answer("Количество часов должно быть больше 0!")
        
        rate = await get_user_rate(message.from_user.id)
        if rate == 0:
            return await message.answer("⚠️ Сначала укажи ставку за час через команду `!зпчас {число}`!")
            
        earned, current_rate = await add_work_hours(message.from_user.id, hours)
        await message.answer(
            f"Записано: **+{hours} ч.**\n"
            f"По ставке: {current_rate:.2f} руб./ч.\n"
            f"Заработано за смену: **+{earned:.2f} руб.**"
        )
    except ValueError:
        pass  # Игнорируем обычные сообщения

@dp.message(Command("стата") | (F.text.lower() == "!стата"))
async def process_stats(message: types.Message):
    user_id = message.from_user.id
    now = datetime.now()
    
    stats_text = "📊 **Статистика доходов за последние 3 месяца:**\n\n"
    
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(3):
            # Вычисление месяца назад
            month_date = now - timedelta(days=i*30)
            year = month_date.year
            month = month_date.month
            month_str = f"{year}-{month:02d}"
            
            # Аванс (1-15 число)
            async with db.execute(
                """SELECT SUM(hours), SUM(total_earned) FROM work_entries 
                   WHERE user_id = ? AND strftime('%Y-%m', entry_date) = ? AND period_type = 'advance'""",
                (user_id, month_str)
            ) as cursor:
                adv_h, adv_sum = await cursor.fetchone()
                adv_h = adv_h or 0
                adv_sum = adv_sum or 0

            # Зарплата (16-31 число)
            async with db.execute(
                """SELECT SUM(hours), SUM(total_earned) FROM work_entries 
                   WHERE user_id = ? AND strftime('%Y-%m', entry_date) = ? AND period_type = 'main'""",
                (user_id, month_str)
            ) as cursor:
                main_h, main_sum = await cursor.fetchone()
                main_h = main_h or 0
                main_sum = main_sum or 0

            month_name = month_date.strftime("%B %Y")
            stats_text += (
                f"📅 **{month_name}**\n"
                f" ├ 💸 Аванс (1-15 число): **{adv_sum:.2f} руб.** ({adv_h} ч.)\n"
                f" ├ 💰 Зарплата (16-Конец): **{main_sum:.2f} руб.** ({main_h} ч.)\n"
                f" └ 📈 Всего за месяц: **{(adv_sum + main_sum):.2f} руб.**\n\n"
            )

    await message.answer(stats_text, parse_mode="Markdown")

# ------------------- АВТОМАТИЧЕСКИЕ ОТЧЕТЫ (CRON) -------------------

async def send_advance_report():
    """Отправляется 30 числа: отчет по авансу за период с 1 по 15 число текущего месяца"""
    today = datetime.now()
    month_str = today.strftime("%Y-%m")
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM users") as cursor:
            users = await cursor.fetchall()
            
        for (user_id,) in users:
            async with db.execute(
                """SELECT SUM(hours), SUM(total_earned) FROM work_entries 
                   WHERE user_id = ? AND strftime('%Y-%m', entry_date) = ? AND period_type = 'advance'""",
                (user_id, month_str)
            ) as cursor:
                h, total = await cursor.fetchone()
                h = h or 0
                total = total or 0
                
                try:
                    await bot.send_message(
                        user_id, 
                        f"📅 **Отчет по авансу (за 1–15 {today.strftime('%B')}):**\n"
                        f"Отработано: **{h} ч.**\n"
                        f"Сумма к выплате: **{total:.2f} руб.**", 
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logging.error(f"Не удалось отправить отчет {user_id}: {e}")

async def send_main_salary_report():
    """Отправляется 15 числа: отчет по зарплате за период с 16 по конец ПРОШЛОГО месяца"""
    first_of_this_month = datetime.now().replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    month_str = last_month.strftime("%Y-%m")
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM users") as cursor:
            users = await cursor.fetchall()
            
        for (user_id,) in users:
            async with db.execute(
                """SELECT SUM(hours), SUM(total_earned) FROM work_entries 
                   WHERE user_id = ? AND strftime('%Y-%m', entry_date) = ? AND period_type = 'main'""",
                (user_id, month_str)
            ) as cursor:
                h, total = await cursor.fetchone()
                h = h or 0
                total = total or 0
                
                try:
                    await bot.send_message(
                        user_id, 
                        f"📅 **Отчет по зарплате (за 16–{last_month.day} {last_month.strftime('%B')}):**\n"
                        f"Отработано: **{h} ч.**\n"
                        f"Сумма к выплате: **{total:.2f} руб.**", 
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logging.error(f"Не удалось отправить отчет {user_id}: {e}")

# ------------------- ЗАПУСК -------------------

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    # Отправка аванса 30 числа в 10:00
    scheduler.add_job(send_advance_report, 'cron', day=30, hour=10, minute=0)
    # Отправка зарплаты 15 числа в 10:00
    scheduler.add_job(send_main_salary_report, 'cron', day=15, hour=10, minute=0)
    
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())