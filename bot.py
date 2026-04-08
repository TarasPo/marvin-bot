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

# Два канала и их группы обсуждений
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

def log_to_sheets(post_text: str, style: str, comment_text: str, published: bool, channel_id: str):
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
            "да" if published else "нет"
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
            if row.get("хэш") == post_hash and row.get("опубликован") == "да":
                return {"style": row["стиль"], "text": row["комментарий"]}
    except Exception as e:
        logging.error(f"Ошибка поиска кэша: {e}")
    return None

def init_sheet_headers():
    try:
        sheet = get_sheet().sheet1
        if not sheet.row_values(1):
            sheet.append_row(["дата", "канал", "пост", "хэш", "стиль", "комментарий", "опубликован"])
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
    variants = {}
    for style in STYLES:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            system=MARVIN_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Напиши комментарий в стиле «{style}» к этому посту:\n\n{post_text}"
            }]
        )
        variants[style] = response.content[0].text.strip()
    return variants


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
            f"Использовать или сгенерировать новые?"
        )
        keyboard = [
            [InlineKeyboardButton("✅ Опубликовать готовый", callback_data=f"publish:{post_id}:{cached['style']}")],
            [InlineKeyboardButton("🔄 Сгенерировать новые", callback_data=f"regen:{post_id}")],
        ]
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    variants = generate_variants(post_text)
    pending_posts[post_id] = {
        "variants": variants,
        "post_id": post_id,
        "post_text": post_text,
        "channel_id": channel_id,
    }
    variants_text = "\n\n".join([f"*{s}*\n{t}" for s, t in variants.items()])
    text = f"📬 Новый пост:\n\n{post_text[:200]}...\n\n─────────────\n{variants_text}\n\n─────────────\nВыбери стиль:"
    keyboard = [[InlineKeyboardButton(s, callback_data=f"style:{post_id}:{s}")] for s in STYLES]
    keyboard.append([InlineKeyboardButton("🔄 Перегенерировать всё", callback_data=f"regen:{post_id}")])
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.forward_origin:
        return
    if hasattr(msg.forward_origin, 'chat') and str(msg.forward_origin.chat.id) in CHANNELS:
        channel_post_id = msg.forward_origin.message_id
        if channel_post_id in pending_posts:
            pending_posts[channel_post_id]["group_message_id"] = msg.message_id

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит reply администратора на сообщение бота — предлагает опубликовать свой текст."""
    msg = update.message
    if not msg or str(msg.chat.id) != str(ADMIN_CHAT_ID):
        return
    if not msg.reply_to_message or msg.reply_to_message.from_user.id != context.bot.id:
        return

    custom_text = msg.text
    if not custom_text:
        return

    # Ищем post_id в pending_posts — берём последний
    if not pending_posts:
        await msg.reply_text("Нет активных постов в памяти.")
        return

    post_id = list(pending_posts.keys())[-1]
    post_data = pending_posts[post_id]
    # Сохраняем кастомный текст
    pending_posts[post_id]["custom_text"] = custom_text

    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish_custom:{post_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"back:{post_id}")]
    ]
    await msg.reply_text(
        f"Твой вариант:\n\n{custom_text}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("style:"):
        _, post_id, style = data.split(":", 2)
        post_id = int(post_id)
        comment_text = pending_posts[post_id]["variants"][style]
        keyboard = [
            [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{post_id}:{style}")],
            [InlineKeyboardButton("↩️ Другой стиль", callback_data=f"back:{post_id}")]
        ]
        await query.edit_message_text(f"Стиль: {style}\n\n{comment_text}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("publish_custom:"):
        post_id = int(data.split(":")[1])
        post_data = pending_posts[post_id]
        comment_text = post_data.get("custom_text", "")
        channel_id = post_data.get("channel_id", list(CHANNELS.keys())[0])
        discussion_group_id = CHANNELS[channel_id]
        await context.bot.send_message(
            chat_id=discussion_group_id,
            text=comment_text,
            reply_to_message_id=post_data.get("group_message_id", post_id)
        )
        log_to_sheets(post_data["post_text"], "Свой вариант", comment_text, True, channel_id)
        await query.edit_message_text(f"✅ Опубликовано (свой вариант):\n\n{comment_text}")
        del pending_posts[post_id]
    
    elif data.startswith("publish:"):
        _, post_id, style = data.split(":", 2)
        post_id = int(post_id)
        post_data = pending_posts[post_id]
        comment_text = post_data["variants"][style]
        channel_id = post_data.get("channel_id", list(CHANNELS.keys())[0])
        discussion_group_id = CHANNELS[channel_id]
        await context.bot.send_message(
            chat_id=discussion_group_id,
            text=comment_text,
            reply_to_message_id=post_data.get("group_message_id", post_id)
        )
        log_to_sheets(post_data["post_text"], style, comment_text, True, channel_id)
        await query.edit_message_text(f"✅ Опубликовано ({style}):\n\n{comment_text}")
        del pending_posts[post_id]

    elif data.startswith("back:"):
        post_id = int(data.split(":")[1])
        keyboard = [[InlineKeyboardButton(s, callback_data=f"style:{post_id}:{s}")] for s in STYLES]
        keyboard.append([InlineKeyboardButton("🔄 Перегенерировать всё", callback_data=f"regen:{post_id}")])
        await query.edit_message_text("Выбери стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("regen:"):
        post_id = int(data.split(":")[1])
        await query.edit_message_text("⏳ Генерирую новые варианты...")
        post_data = pending_posts.get(post_id)
        if not post_data:
            await query.edit_message_text("❌ Пост не найден в памяти.")
            return
        variants = generate_variants(post_data["post_text"])
        pending_posts[post_id]["variants"] = variants
        variants_text = "\n\n".join([f"*{s}*\n{t}" for s, t in variants.items()])
        text = f"🔄 Новые варианты:\n\n{variants_text}\n\n─────────────\nВыбери стиль:"
        keyboard = [[InlineKeyboardButton(s, callback_data=f"style:{post_id}:{s}")] for s in STYLES]
        keyboard.append([InlineKeyboardButton("🔄 Перегенерировать ещё", callback_data=f"regen:{post_id}")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def main():
    init_sheet_headers()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(CallbackQueryHandler(handle_callback))
    group_ids = [int(gid) for gid in CHANNELS.values()]
    app.add_handler(MessageHandler(filters.Chat(chat_id=group_ids), handle_group_message))
    app.add_handler(MessageHandler(filters.Chat(int(ADMIN_CHAT_ID)) & filters.REPLY, handle_admin_reply))
    app.run_polling()


if __name__ == "__main__":
    main()
