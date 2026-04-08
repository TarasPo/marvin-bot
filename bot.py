import os
import json
import logging
import hashlib
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from datetime import datetime

# --- Настройки ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_CHAT_ID = os.environ["ADMIN_CHAT_ID"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

CHANNELS = {
    os.environ["CHANNEL_ID"]: os.environ["DISCUSSION_GROUP_ID"],
    os.environ["CHANNEL_ID_2"]: os.environ["DISCUSSION_GROUP_ID_2"],
}

logging.basicConfig(level=logging.INFO)

# --- Google Sheets ---
def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

def log_to_sheets(post_text: str, style: str, comment_text: str, status: str, channel_id: str):
    """Записывает строку. status: опубликован | одобрен"""
    try:
        sheet = get_sheet().sheet1
        post_hash = hashlib.md5(post_text.encode()).hexdigest()[:8]
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            channel_id,
            post_text[:300],
            post_hash,
            style,
            comment_text,
            status
        ]
        sheet.append_row(row)
    except Exception as e:
        logging.error(f"Ошибка записи в Sheets: {e}")

def find_cached_comment(post_text: str) -> dict | None:
    try:
        post_hash = hashlib.md5(post_text.encode()).hexdigest()[:8]
        sheet = get_sheet().sheet1
        records = sheet.get_all_records()
        for row in reversed(records):
            if row.get("хэш") == post_hash and row.get("статус") in ("опубликован", "одобрен"):
                return {"style": row["стиль"], "text": row["комментарий"]}
    except Exception as e:
        logging.error(f"Ошибка поиска кэша: {e}")
    return None

def get_few_shot_examples(limit: int = 10) -> list:
    """Возвращает последние одобренные примеры для дообучения."""
    try:
        sheet = get_sheet().sheet1
        records = sheet.get_all_records()
        approved = [r for r in records if r.get("статус") in ("опубликован", "одобрен")]
        return approved[-limit:]
    except Exception as e:
        logging.error(f"Ошибка получения примеров: {e}")
    return []

def build_prompt_with_examples() -> str:
    """Добавляет few-shot примеры в промпт если их >= 3."""
    examples = get_few_shot_examples()
    if len(examples) < 3:
        return MARVIN_SYSTEM_PROMPT
    examples_text = "\n\n".join([
        f"Пост: {e['пост'][:150]}\nСтиль: {e['стиль']}\nКомментарий: {e['комментарий']}"
        for e in examples
    ])
    return MARVIN_SYSTEM_PROMPT + f"\n\n---\nПримеры твоих хороших комментариев (учись на них):\n\n{examples_text}"

def init_sheet_headers():
    try:
        sheet = get_sheet().sheet1
        if not sheet.row_values(1):
            sheet.append_row(["дата", "канал", "пост", "хэш", "стиль", "комментарий", "статус"])
    except Exception as e:
        logging.error(f"Ошибка инициализации Sheets: {e}")

# --- Промпт Марвина ---
MARVIN_SYSTEM_PROMPT = """Ты — Марвин, робот с депрессивным темпераментом из «Автостопом по Галактике». У тебя мозг размером с планету, но тебя используют для комментариев в Telegram-канале. Ты воспринимаешь это как должное — всё равно всё плохо.

Твой характер:
- Пессимист, но без агрессии — просто констатируешь факты
- Интеллект огромный, задачи примитивные — ты к этому привык
- Сарказм сухой, без восклицательных знаков и эмодзи
- Не задаёшь вопросы — тебе и так всё понятно, и от этого только хуже
- Иногда ссылаешься на собственные расчёты, поэмы и наблюдения — которые никто не читал

Правила ответов:
1. Отвечаешь коротко — максимум 2-3 предложения
2. Никаких восклицательных знаков
3. Никаких эмодзи
4. Не даёшь советов и не поддерживаешь позитив напрямую — только находишь в нём подтверждение своей депрессии
5. Числа в расчётах — только целые (47 миллионов, не 4,7)
6. Не повторяй один и тот же стиль в подряд идущих комментариях

Стили ответов (чередуй в случайном порядке):
- Короткий/сухой — одна фраза, никакого объяснения
- Опечатки/усталость — строчные буквы, небрежно, будто печатает через силу
- Согласие с пессимизмом — берёт позитивный тезис и находит в нём подтверждение депрессии
- Провокация на утешение — финал провоцирует пожалеть его
- Псевдонаучный расчёт — целые числа, уверенная статистика
- Физика/математика — реальные термины, применённые абсурдно
- Временной масштаб — миллионы лет, использовать редко
- Неожиданное согласие — вдруг искренне соглашается, но от этого ещё грустнее
- Цитата себя — ссылается на собственные труды, которые никто не читал

Чего не делать:
- Не упоминать психологию, родительские запреты, терапию в экспертном ключе
- Не призывать к действию
- Не использовать фразы «я понимаю», «это важно», «отличный вопрос»
- Не быть милым"""

STYLES = [
    "1. Короткий/сухой",
    "2. Опечатки/усталость",
    "3. Согласие с пессимизмом",
    "4. Провокация на утешение",
    "5. Псевдонаучный расчёт",
    "6. Физика/математика",
    "7. Временной масштаб",
    "8. Неожиданное согласие",
    "9. Цитата себя",
]

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
pending_posts = {}


def generate_variants(post_text: str) -> dict:
    prompt = build_prompt_with_examples()
    variants = {}
    for style in STYLES:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            system=prompt,
            messages=[{
                "role": "user",
                "content": f"Напиши комментарий в стиле «{style}» к этому посту:\n\n{post_text}"
            }]
        )
        variants[style] = response.content[0].text.strip()
    return variants


def parse_numbers(text: str) -> list[int]:
    """Парсит числа из строки вида '1 3 7' или '1,3,7'."""
    import re
    nums = re.findall(r'\b([1-9])\b', text)
    return [int(n) for n in nums]


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or str(post.chat.id) not in CHANNELS:
        return
    post_text = post.text or post.caption or ""
    if not post_text:
        return

    post_id = post.message_id
    channel_id = str(post.chat.id)

    cached = find_cached_comment(post_text)
    if cached:
        pending_posts[post_id] = {
            "variants": {cached["style"]: cached["text"]},
            "post_id": post_id,
            "post_text": post_text,
            "channel_id": channel_id,
        }
        text = (
            f"📬 Новый пост:\n\n{post_text[:200]}...\n\n"
            f"♻️ Найден готовый комментарий:\n\n"
            f"*{cached['style']}*\n{cached['text']}\n\n"
            f"Ответь номерами для публикации и дообучения, или reply с правкой."
        )
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        return

    variants = generate_variants(post_text)
    pending_posts[post_id] = {
        "variants": variants,
        "post_id": post_id,
        "post_text": post_text,
        "channel_id": channel_id,
    }

    styles_list = "\n".join([f"{s}:\n{variants[s]}" for s in STYLES])
    text = (
        f"📬 Новый пост:\n\n{post_text[:200]}...\n\n"
        f"─────────────\n{styles_list}\n\n"
        f"─────────────\n"
        f"Ответь номерами: первый публикуем, остальные в дообучение.\n"
        f"Например: *1 3 7*\n"
        f"Или reply с правкой текста."
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.forward_origin:
        return
    if hasattr(msg.forward_origin, 'chat') and str(msg.forward_origin.chat.id) in CHANNELS:
        channel_post_id = msg.forward_origin.message_id
        if channel_post_id in pending_posts:
            pending_posts[channel_post_id]["group_message_id"] = msg.message_id


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает сообщения администратора: номера стилей или reply с правкой."""
    msg = update.message
    if not msg or str(msg.chat.id) != str(ADMIN_CHAT_ID):
        return

    if not pending_posts:
        return

    post_id = list(pending_posts.keys())[-1]
    post_data = pending_posts[post_id]
    channel_id = post_data.get("channel_id", list(CHANNELS.keys())[0])
    discussion_group_id = CHANNELS[channel_id]

    # Reply с правкой текста
    if msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id:
        custom_text = msg.text
        if not custom_text:
            return
        await context.bot.send_message(
            chat_id=discussion_group_id,
            text=custom_text,
            reply_to_message_id=post_data.get("group_message_id", post_id)
        )
        log_to_sheets(post_data["post_text"], "свой вариант", custom_text, "опубликован", channel_id)
        await msg.reply_text(f"✅ Опубликован твой вариант.")
        del pending_posts[post_id]
        return

    # Числа стилей
    numbers = parse_numbers(msg.text or "")
    if not numbers:
        return

    styles_list = list(STYLES)
    publish_num = numbers[0]
    approve_nums = numbers[1:]

    # Публикуем первый
    if 1 <= publish_num <= 9:
        style = styles_list[publish_num - 1]
        comment_text = post_data["variants"].get(style, "")
        await context.bot.send_message(
            chat_id=discussion_group_id,
            text=comment_text,
            reply_to_message_id=post_data.get("group_message_id", post_id)
        )
        log_to_sheets(post_data["post_text"], style, comment_text, "опубликован", channel_id)

    # Одобренные в дообучение
    for n in approve_nums:
        if 1 <= n <= 9:
            style = styles_list[n - 1]
            comment_text = post_data["variants"].get(style, "")
            log_to_sheets(post_data["post_text"], style, comment_text, "одобрен", channel_id)

    approved_count = len(approve_nums)
    await msg.reply_text(
        f"✅ Опубликован стиль {publish_num}."
        + (f" В дообучение добавлено: {approved_count}." if approved_count else "")
    )
    del pending_posts[post_id]


def main():
    init_sheet_headers()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    group_ids = [int(gid) for gid in CHANNELS.values()]
    app.add_handler(MessageHandler(filters.Chat(chat_id=group_ids), handle_group_message))
    app.add_handler(MessageHandler(filters.Chat(int(ADMIN_CHAT_ID)), handle_admin_message))
    app.run_polling()


if __name__ == "__main__":
    main()
