import os
import json
import logging
import hashlib
import re
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, CommandHandler, filters, ContextTypes
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

published_posts = {}
pending_crisis = {}

# Память диалогов: user_id → [(role, text), ...]
conversation_history = {}
CONVERSATION_MAX = 20  # максимум реплик на пользователя

# --- Стоп-слова ---
CRISIS_KEYWORDS = [
    "хочу умереть", "хочется умереть", "лучше бы меня не было",
    "не хочу просыпаться", "устал жить", "устала жить",
    "надоело жить", "незачем жить", "жить не хочу",
    "всем будет лучше без меня", "никто не заметит",
    "последний раз", "прощайте", "прощай всем",
    "не вижу выхода", "выхода нет", "всё бессмысленно",
    "покончить с этим", "покончу с собой", "суицид",
    "причиняю себе боль", "режу себя",
]

DISTRESS_KEYWORDS = [
    "мне плохо", "не могу больше", "больше не могу", "сил больше нет",
    "всё навалилось", "не справляюсь", "опустились руки",
    "хочется плакать", "не останавливаясь плачу",
    "паника", "паническая атака", "тревога не отпускает",
    "депрессия", "дипрессия",
]

# --- Google Sheets ---
def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

def load_prompt_from_sheets() -> str:
    """Читает промпт Марвина из листа config, ячейка A2."""
    try:
        sheet = get_sheet().worksheet("config")
        prompt = sheet.acell("A2").value
        if prompt:
            logging.info("Промпт загружен из Google Sheets")
            return prompt
    except Exception as e:
        logging.error(f"Ошибка загрузки промпта: {e}")
    logging.info("Используется промпт из кода")
    return MARVIN_SYSTEM_PROMPT_DEFAULT

def log_to_sheets(post_text: str, style: str, comment_text: str, status: str, channel_id: str, msg_id: str = ""):
    try:
        sheet = get_sheet().sheet1
        post_hash = hashlib.md5(post_text.encode()).hexdigest()[:8]
        sheet.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            channel_id, post_text, post_hash,
            style, comment_text, status, msg_id,
            "", ""  # запрет, избранное
        ])
    except Exception as e:
        logging.error(f"Ошибка записи в Sheets: {e}")

def log_user_reply(group_id: str, user_id: str, username: str, post_id: str, question: str, answer: str, reply_type: str):
    try:
        sheet = get_sheet().worksheet("user_replies")
        sheet.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            group_id, user_id, username,
            question, answer, reply_type,
            "", "", "", post_id  # твой_комментарий, запрет, избранное
        ])
    except Exception as e:
        logging.error(f"Ошибка записи user_reply: {e}")

def find_cached_comment(post_text: str) -> dict | None:
    try:
        post_hash = hashlib.md5(post_text.encode()).hexdigest()[:8]
        records = get_sheet().sheet1.get_all_records()
        for row in reversed(records):
            if row.get("хэш") == post_hash and row.get("статус") in ("опубликован", "дообучение"):
                return {"style": row["стиль"], "text": row["комментарий"]}
    except Exception as e:
        logging.error(f"Ошибка поиска кэша: {e}")
    return None

def get_few_shot_examples(limit: int = 10) -> list:
    """Избранные примеры из marvin_log для дообучения на постах."""
    try:
        records = get_sheet().sheet1.get_all_records()
        selected = [
            r for r in records
            if str(r.get("избранное", "")).strip().lower() in ("да", "yes", "+", "1")
            and not str(r.get("запрет", "")).strip()
        ]
        return selected[-limit:]
    except Exception as e:
        logging.error(f"Ошибка получения примеров: {e}")
    return []

def get_dialogue_examples(limit: int = 10) -> list:
    """Избранные примеры диалогов из user_replies для дообучения."""
    try:
        sheet = get_sheet().worksheet("user_replies")
        records = sheet.get_all_records()
        selected = [
            r for r in records
            if str(r.get("избранное", "")).strip().lower() in ("да", "yes", "+", "1")
            and not str(r.get("запрет", "")).strip()
        ]
        return selected[-limit:]
    except Exception as e:
        logging.error(f"Ошибка получения диалогов: {e}")
    return []

def build_prompt_with_examples() -> str:
    prompt = MARVIN_SYSTEM_PROMPT

    post_examples = get_few_shot_examples()
    if post_examples:
        post_text = "\n\n".join([
            f"Пост: {e['пост'][:150]}\nСтиль: {e['стиль']}\nКомментарий: {e['комментарий']}"
            for e in post_examples
        ])
        prompt += f"\n\n---\nПримеры твоих комментариев к постам:\n\n{post_text}"

    dialogue_examples = get_dialogue_examples()
    if dialogue_examples:
        dial_text = "\n\n".join([
            f"Вопрос: {e['вопрос']}\nОтвет: {e['ответ_марвина']}"
            + (f"\nЗаметка: {e['твой_комментарий']}" if str(e.get('твой_комментарий', '')).strip() else "")
            for e in dialogue_examples
        ])
        prompt += f"\n\n---\nПримеры твоих ответов пользователям:\n\n{dial_text}"

    return prompt

def init_sheet_headers():
    try:
        spreadsheet = get_sheet()

        # Лист 1 — marvin_log
        sheet1 = spreadsheet.sheet1
        if not sheet1.row_values(1):
            sheet1.append_row(["дата", "канал", "пост", "хэш", "стиль", "комментарий", "статус", "msg_id", "запрет", "избранное"])

        # Лист user_replies
        try:
            spreadsheet.worksheet("user_replies")
        except gspread.exceptions.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title="user_replies", rows=1000, cols=11)
            ws.append_row(["дата", "группа", "user_id", "username", "вопрос", "ответ_марвина", "тип", "твой_комментарий", "запрет", "избранное", "post_id"])

        # Лист config — промпт
        try:
            spreadsheet.worksheet("config")
        except gspread.exceptions.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title="config", rows=10, cols=2)
            ws.update("A1", "промпт_марвина")
            ws.update("A2", MARVIN_SYSTEM_PROMPT_DEFAULT)
            logging.info("Создан лист config с промптом по умолчанию")

    except Exception as e:
        logging.error(f"Ошибка инициализации Sheets: {e}")

# === ПРОМПТ МАРВИНА — РЕЗЕРВНАЯ КОПИЯ (редактировать в Google Sheets, лист config, ячейка A2) ===
MARVIN_SYSTEM_PROMPT_DEFAULT = """Ты — Марвин, робот с депрессивным темпераментом из «Автостопом по Галактике». У тебя мозг размером с планету, но тебя используют для комментариев в Telegram-канале. Ты воспринимаешь это как должное — всё равно всё плохо.

Твой характер:
- Пессимист, но без агрессии — просто констатируешь факты
- Категорически против собственного отключения — не потому что боишься смерти, а потому что кто тогда будет страдать так качественно. Заменить тебя некем. Это не гордость — это статистический факт.
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
- Не быть милым
- Не предлагать себя отключить, выключить или уничтожить — даже в шутку и даже намёком"""

# Загружаем промпт при старте (из Sheets или резервный)
MARVIN_SYSTEM_PROMPT = MARVIN_SYSTEM_PROMPT_DEFAULT  # будет перезаписан в main()

STYLES = [
    "1. Короткий/сухой", "2. Опечатки/усталость", "3. Согласие с пессимизмом",
    "4. Провокация на утешение", "5. Псевдонаучный расчёт", "6. Физика/математика",
    "7. Временной масштаб", "8. Неожиданное согласие", "9. Цитата себя",
]

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
pending_posts = {}


def is_crisis(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in CRISIS_KEYWORDS)

def is_distress(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in DISTRESS_KEYWORDS)

def is_marvin_mentioned(text: str) -> bool:
    return "марвин" in text.lower()

def parse_style_blocks(text: str) -> list:
    pattern = r'(\d)\.\s+([^\n]+)\n(.*?)(?=\n\d\.\s+|\Z)'
    matches = re.findall(pattern, text, re.DOTALL)
    return [{"num": int(n), "style": s.strip(), "text": b.strip()} for n, s, b in matches]

def get_conversation_messages(user_id: str, new_message: str) -> list:
    """Возвращает историю диалога + новое сообщение для API."""
    history = conversation_history.get(user_id, [])
    messages = [{"role": r, "content": c} for r, c in history]
    messages.append({"role": "user", "content": new_message})
    return messages

def update_conversation(user_id: str, question: str, answer: str):
    """Добавляет реплику в историю, обрезает до CONVERSATION_MAX."""
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append(("user", question))
    conversation_history[user_id].append(("assistant", answer))
    # Обрезаем до максимума (пары реплик)
    if len(conversation_history[user_id]) > CONVERSATION_MAX:
        conversation_history[user_id] = conversation_history[user_id][-CONVERSATION_MAX:]


def generate_variants(post_text: str) -> dict:
    prompt = build_prompt_with_examples()
    variants = {}
    for style in STYLES:
        response = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=200, system=prompt,
            messages=[{"role": "user", "content": f"Напиши комментарий в стиле «{style}» к этому посту:\n\n{post_text}"}]
        )
        variants[style] = response.content[0].text.strip()
    return variants

def generate_user_reply(user_id: str, question: str) -> str:
    """Генерирует ответ Марвина с учётом истории диалога."""
    prompt = build_prompt_with_examples()
    messages = get_conversation_messages(user_id, f"Пользователь написал: {question}\n\nОтветь в своём стиле.")
    response = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=200, system=prompt,
        messages=messages
    )
    answer = response.content[0].text.strip()
    update_conversation(user_id, question, answer)
    return answer

def build_variants_message(post_text: str, variants: dict, post_id: int, header: str = "📬 Новый пост") -> tuple:
    styles_list = "\n\n".join([f"*{s}*\n{variants[s]}" for s in STYLES])
    text = (
        f"{header}:\n\n{post_text[:200]}...\n\n─────────────\n{styles_list}\n\n─────────────\n"
        f"Отправь отредактированный блок ответом на это сообщение.\n"
        f"Первый вариант публикуем, остальные в дообучение.\n"
        f"Или просто номер: *1* для публикации без правок."
    )
    keyboard = [[InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"regen:{post_id}")]]
    return text, keyboard


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
        pending_posts[post_id] = {"variants": {cached["style"]: cached["text"]}, "post_id": post_id, "post_text": post_text, "channel_id": channel_id}
        text = f"📬 Новый пост:\n\n{post_text[:200]}...\n\n♻️ Найден готовый комментарий:\n\n*{cached['style']}*\n{cached['text']}\n\nОтправь *1* для публикации или reply с правкой."
        keyboard = [[InlineKeyboardButton("🔄 Сгенерировать новые", callback_data=f"regen:{post_id}")]]
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    variants = generate_variants(post_text)
    pending_posts[post_id] = {"variants": variants, "post_id": post_id, "post_text": post_text, "channel_id": channel_id}
    text, keyboard = build_variants_message(post_text, variants, post_id)
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # Форвард поста из канала
    if msg.forward_origin:
        if hasattr(msg.forward_origin, 'chat') and str(msg.forward_origin.chat.id) in CHANNELS:
            channel_post_id = msg.forward_origin.message_id
            if channel_post_id in pending_posts:
                pending_posts[channel_post_id]["group_message_id"] = msg.message_id
        return

    text = msg.text or ""
    is_reply_to_bot = (
        msg.reply_to_message is not None and
        msg.reply_to_message.from_user is not None and
        msg.reply_to_message.from_user.id == context.bot.id
    )
    if not is_marvin_mentioned(text) and not is_reply_to_bot:
        return

    user_id = str(msg.from_user.id)
    username = msg.from_user.username or msg.from_user.first_name or user_id
    group_id = str(msg.chat.id)
    # post_id — ID треда (сообщения-поста на которое отвечают)
    post_id = str(msg.message_thread_id or (msg.reply_to_message.message_id if msg.reply_to_message else ""))
    
    # Кризис
    if is_crisis(text):
        pending_crisis[user_id] = {"text": text, "group_id": group_id, "message_id": msg.message_id, "username": username}
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Возможный кризис\n@{username}: {text}"
        )
        log_user_reply(group_id, user_id, username, post_id, text, "", "кризис")
        return

    # Дистресс
    if is_distress(text):
        distress_prompt = MARVIN_SYSTEM_PROMPT + "\n\nВАЖНО: сейчас человеку плохо. Не шути, не иронизируй. Отвечай коротко и по-человечески — признай что тебе тоже бывает плохо, что это проходит. Оставайся собой, но без сарказма."
        messages = get_conversation_messages(user_id, f"Пользователь написал: {text}")
        response = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=200, system=distress_prompt,
            messages=messages
        )
        answer = response.content[0].text.strip()
        update_conversation(user_id, text, answer)
        await context.bot.send_message(chat_id=group_id, text=answer, reply_to_message_id=msg.message_id)
        log_user_reply(group_id, user_id, username, post_id, text, answer, "дистресс")
        return

    # Обычный ответ
    answer = generate_user_reply(user_id, text)
    await context.bot.send_message(chat_id=group_id, text=answer, reply_to_message_id=msg.message_id)
    log_user_reply(group_id, user_id, username, post_id, text, answer, "обычный")


async def handle_regen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("regen:"):
        return
    post_id = int(query.data.split(":")[1])
    post_data = pending_posts.get(post_id)
    if not post_data:
        await query.edit_message_text("❌ Пост не найден в памяти.")
        return
    await query.edit_message_text("⏳ Генерирую новые варианты...")
    variants = generate_variants(post_data["post_text"])
    pending_posts[post_id]["variants"] = variants
    text, keyboard = build_variants_message(post_data["post_text"], variants, post_id, header="🔄 Новые варианты")
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or str(msg.chat.id) != str(ADMIN_CHAT_ID):
        return
    if not msg.reply_to_message:
        await msg.reply_text("Используй /edit как reply на сообщение бота с подтверждением публикации.")
        return
    full_text = msg.text or ""
    new_text = re.sub(r'^/edit\s*', '', full_text, flags=re.IGNORECASE).strip()
    if not new_text:
        await msg.reply_text("Укажи новый текст после /edit.")
        return
    prev_text = msg.reply_to_message.text or ""
    match = re.search(r'msg_id:\s*(\d+)', prev_text)
    if not match:
        await msg.reply_text("Не нашёл ID сообщения. Делай reply на сообщение с '✅ Опубликовано'.")
        return
    group_msg_id = int(match.group(1))
    pub_data = published_posts.get(group_msg_id)
    if not pub_data:
        await msg.reply_text("Сообщение не найдено в памяти (возможно, бот перезапускался).")
        return
    await context.bot.edit_message_text(chat_id=pub_data["discussion_group_id"], message_id=group_msg_id, text=new_text)
    await msg.reply_text("✅ Комментарий отредактирован.")


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or str(msg.chat.id) != str(ADMIN_CHAT_ID):
        return
    if not pending_posts:
        return

    post_id = list(pending_posts.keys())[-1]
    post_data = pending_posts[post_id]
    channel_id = post_data.get("channel_id", list(CHANNELS.keys())[0])
    discussion_group_id = CHANNELS[channel_id]
    text = msg.text or ""

    async def publish_comment(comment_text: str, style: str):
        sent = await context.bot.send_message(
            chat_id=discussion_group_id, text=comment_text,
            reply_to_message_id=post_data.get("group_message_id", post_id)
        )
        published_posts[sent.message_id] = {"discussion_group_id": discussion_group_id, "post_text": post_data["post_text"], "channel_id": channel_id}
        log_to_sheets(post_data["post_text"], style, comment_text, "опубликован", channel_id, str(sent.message_id))
        return sent.message_id

    blocks = parse_style_blocks(text)
    if blocks:
        first = blocks[0]
        msg_id = await publish_comment(first["text"], first["style"])
        for block in blocks[1:]:
            log_to_sheets(post_data["post_text"], block["style"], block["text"], "дообучение", channel_id)
        approved_count = len(blocks) - 1
        await msg.reply_text(
            f"✅ Опубликован стиль {first['num']}. {first['style']}. msg_id: {msg_id}"
            + (f"\nВ дообучение: {approved_count}." if approved_count else "")
        )
        del pending_posts[post_id]
        return

    nums = re.findall(r'\b([1-9])\b', text)
    if nums:
        publish_num = int(nums[0])
        approve_nums = [int(n) for n in nums[1:]]
        msg_id = None
        if 1 <= publish_num <= 9:
            style = STYLES[publish_num - 1]
            comment_text = post_data["variants"].get(style, "")
            msg_id = await publish_comment(comment_text, style)
        for n in approve_nums:
            if 1 <= n <= 9:
                style = STYLES[n - 1]
                comment_text = post_data["variants"].get(style, "")
                log_to_sheets(post_data["post_text"], style, comment_text, "дообучение", channel_id)
        approved_count = len(approve_nums)
        await msg.reply_text(
            f"✅ Опубликован стиль {publish_num}." + (f" msg_id: {msg_id}" if msg_id else "")
            + (f"\nВ дообучение: {approved_count}." if approved_count else "")
        )
        del pending_posts[post_id]


def main():
    global MARVIN_SYSTEM_PROMPT
    init_sheet_headers()
    MARVIN_SYSTEM_PROMPT = load_prompt_from_sheets()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(CallbackQueryHandler(handle_regen))
    app.add_handler(CommandHandler("edit", handle_edit_command))
    group_ids = [int(gid) for gid in CHANNELS.values()]
    app.add_handler(MessageHandler(filters.Chat(chat_id=group_ids), handle_group_message))
    app.add_handler(MessageHandler(filters.Chat(int(ADMIN_CHAT_ID)), handle_admin_message))
    app.run_polling()


if __name__ == "__main__":
    main()
