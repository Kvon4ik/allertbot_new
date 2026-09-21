import os
import json
import asyncio
import logging
from datetime import datetime, time, date
from zoneinfo import ZoneInfo
from aiohttp import ClientSession, ClientTimeout

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus

# --- НАЛАШТУВАННЯ ---
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "8874808047:AAFiKnVwq4RL48l0BOByre54v7wlee0DsfA")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1003041243074"))
MESSAGE_THREAD_ID = int(os.getenv("MESSAGE_THREAD_ID", "5581"))

ALERTS_API_TOKEN = os.getenv("ALERTS_API_TOKEN", "0822b0a0c61350c8b6cdabcea940345a5a0ce3ccab2203")

# 31 - м. Київ (Київська область - 10)
LOCATION_UID = int(os.getenv("LOCATION_UID", "31"))

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"
CHECK_INTERVAL_SECONDS = 30
TZ_KYIV = ZoneInfo("Europe/Kyiv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Глобальний стан
BOT_MANUALLY_ENABLED = True
current_alert_state = None
# False означає, що відбій за замовчуванням вже вважається дійсним і слати його повторно не треба
last_notified_state = False
region_title = f"UID {LOCATION_UID}"

dp = Dispatcher()


# --- ПЕРЕВІРКА ПРАВ АДМІНІСТРАТОРА ---
async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception as e:
        logging.error(f"Помилка перевірки прав: {e}")
        return False


# --- РОБОТА З РОЗКЛАДОМ ТА API ---
def load_schedule(filepath: str = "schedule.json") -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def get_current_week_number(ref_date_str: str, ref_week: int, current_date: date) -> int:
    ref_d = datetime.strptime(ref_date_str, "%Y-%m-%d").date()
    days_diff = (current_date - ref_d).days
    weeks_passed = days_diff // 7
    return 1 if (weeks_passed % 2 == 0 and ref_week == 1) else 2


def is_time_allowed(schedule_data: dict, now: datetime) -> bool:
    try:
        week_num = get_current_week_number(
            schedule_data["reference_date"],
            schedule_data.get("reference_week", 1),
            now.date()
        )
        
        day_abbrs = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        current_day = day_abbrs[now.weekday()]
        
        intervals = schedule_data["weeks"].get(str(week_num), {}).get(current_day, [])
        current_time = now.time()

        for start_str, end_str in intervals:
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
            if time(sh, sm) <= current_time <= time(eh, em):
                return True

        return False
    except Exception as e:
        logging.error(f"Помилка розкладу: {e}")
        return True


async def get_active_alert_status(session: ClientSession, location_uid: int) -> tuple[bool, str, str]:
    headers = {"Authorization": f"Bearer {ALERTS_API_TOKEN}"}
    timeout = ClientTimeout(total=8)

    try:
        async with session.get(API_URL, headers=headers, timeout=timeout) as response:
            if response.status != 200:
                body = await response.text()
                logging.error(f"Помилка API ({response.status}): {body}")
                return False, "", ""

            data = await response.json()
            alerts = data.get("alerts", [])

            for alert in alerts:
                if str(alert.get("location_uid")) == str(location_uid):
                    title = alert.get("location_title", f"UID {location_uid}")
                    alert_type = alert.get("alert_type", "air_raid")
                    threat = alert.get("threat", alert.get("notes", ""))
                    
                    level_desc = "Повітряна тривога"
                    if "yellow" in alert_type.lower() or "drone" in str(threat).lower():
                        level_desc = "🟡 Жовтий рівень (Загроза БПЛА)"
                    elif "red" in alert_type.lower() or "missile" in str(threat).lower():
                        level_desc = "🔴 Червоний рівень (Ракетна небезпека)"

                    return True, title, level_desc

            return False, "", ""
    except Exception as e:
        logging.error(f"Мережева помилка alerts: {e}")
        return False, "", ""


async def check_and_send_alert(bot: Bot, session: ClientSession, check_only_alerts: bool = False):
    """
    Перевіряє статус тривоги.
    Якщо check_only_alerts=True (при команді /on) — повідомлення надсилається ТІЛЬКИ якщо зараз тривога.
    """
    global current_alert_state, last_notified_state, region_title

    now = datetime.now(TZ_KYIV)
    is_active, title, threat_desc = await get_active_alert_status(session, LOCATION_UID)
    if title:
        region_title = title

    current_alert_state = is_active

    try:
        schedule_data = load_schedule()
        schedule_allowed = is_time_allowed(schedule_data, now)
    except Exception as e:
        logging.error(f"Помилка schedule.json: {e}")
        schedule_allowed = True

    allowed = BOT_MANUALLY_ENABLED and schedule_allowed

    # Якщо викликано при /on, а тривоги немає — просто фіксуємо спокійний стан і нічого не шлемо в чат
    if check_only_alerts and not current_alert_state:
        last_notified_state = False
        logging.info("Перевірка після /on: тривоги немає, повідомлення не надсилається.")
        return

    # Відправляємо лише коли дозволено і статус реально змінився
    if allowed and (current_alert_state != last_notified_state):
        if current_alert_state:
            msg = (
                f"🚨 <b>ПОВІТРЯНА ТРИВОГА!</b>\n\n"
                f"📍 <b>{region_title}</b>\n"
                f"⚠️ <b>Рівень:</b> {threat_desc}\n\n"
                f"Пари нєма!"
            )
        else:
            msg = (
                f"🟢 <b>ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ!</b>\n\n"
                f"📍 <b>{region_title}</b>\n"
                f"Пара є!"
            )

        try:
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                message_thread_id=MESSAGE_THREAD_ID,
                text=msg,
                parse_mode="HTML"
            )
            last_notified_state = current_alert_state
            logging.info(f"✅ Надіслано сповіщення: {'Тривога' if current_alert_state else 'Відбій'}")
        except Exception as e:
            logging.error(f"❌ Помилка надсилання сповіщення: {e}")


# --- ХЕНДЛЕРИ КОМАНД ---
@dp.message(Command("off"))
async def cmd_off(message: types.Message, bot: Bot):
    global BOT_MANUALLY_ENABLED

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Ця команда доступна лише адміністраторам.")
        return

    if not BOT_MANUALLY_ENABLED:
        await message.reply("ℹ️ Сповіщення вже були вимкнені.")
        return

    BOT_MANUALLY_ENABLED = False
    logging.info(f"🛑 Сповіщення вимкнено адміністратором @{message.from_user.username or message.from_user.id}")
    await message.reply("🛑 <b>Сповіщення про тривоги вимкнено!</b>\nБот не надсилатиме повідомлень до команди /on.", parse_mode="HTML")


@dp.message(Command("on"))
async def cmd_on(message: types.Message, bot: Bot):
    global BOT_MANUALLY_ENABLED

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Ця команда доступна лише адміністраторам.")
        return

    BOT_MANUALLY_ENABLED = True

    logging.info(f"▶️ Сповіщення увімкнено адміністратором @{message.from_user.username or message.from_user.id}")
    await message.reply("▶️ <b>Сповіщення увімкнено!</b>\nПеревіряю статус тривоги в м. Київ...", parse_mode="HTML")

    # Миттєва перевірка: надішле сповіщення ТІЛЬКИ якщо зараз активна тривога
    async with ClientSession() as session:
        await check_and_send_alert(bot, session, check_only_alerts=True)


# --- ЦИКЛ МОНІТОРИНГУ ---
async def monitor_alerts(bot: Bot):
    async with ClientSession() as session:
        while True:
            await check_and_send_alert(bot, session)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# --- ТОЧКА ВХОДУ ---
async def main():
    bot = Bot(token=TG_BOT_TOKEN)
    try:
        logging.info("Скрипт моніторингу та обробки команд запущено...")
        await asyncio.gather(
            dp.start_polling(bot),
            monitor_alerts(bot)
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())