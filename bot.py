import os
import logging
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- Настройки ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHANNEL_ID = os.environ["CHANNEL_ID"]        # ID канала, например -1001234567890
ADMIN_CHAT_ID = os.environ["ADMIN_CHAT_ID"]  # Твой Telegram ID для получения вариантов

logging.basicConfig(level=logging.INFO)

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
    "Короткий/сухой",
    "Опечатки/усталость", 
    "Согласие с пессимизмом",
    "Провокация на утешение",
    "Псевдонаучный расчёт",
    "Физика/математика",
    "Временной масштаб",
    "Неожиданное согласие",
    "Цитата себя",
]

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Хранилище: message_id поста → {style: text}
pending_posts = {}


def generate_variants(post_text: str) -> dict:
    """Генерирует один вариант на каждый стиль."""
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
    """Ловит новый пост в канале и присылает варианты админу."""
    post = update.channel_post
    if not post or str(post.chat.id) != CHANNEL_ID:
        return

    post_text = post.text or post.caption or ""
    if not post_text:
        return

    post_id = post.message_id
    variants = generate_variants(post_text)
    pending_posts[post_id] = {"variants": variants, "post_id": post_id}

    # Отправляем варианты админу
    text = f"📬 Новый пост в канале:\n\n{post_text[:200]}...\n\n─────────────\nВыбери стиль Марвина:"
    keyboard = []
    for style in STYLES:
        preview = variants[style][:40] + "..."
        keyboard.append([InlineKeyboardButton(
            f"{style}",
            callback_data=f"style:{post_id}:{style}"
        )])
    keyboard.append([InlineKeyboardButton("🔄 Перегенерировать всё", callback_data=f"regen:{post_id}")])

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие кнопки."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("style:"):
        _, post_id, style = data.split(":", 2)
        post_id = int(post_id)
        comment_text = pending_posts[post_id]["variants"][style]

        # Показываем текст и кнопки подтверждения
        keyboard = [
            [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{post_id}:{style}")],
            [InlineKeyboardButton("✏️ Другой стиль", callback_data=f"back:{post_id}")]
        ]
        await query.edit_message_text(
            f"Стиль: {style}\n\n{comment_text}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("publish:"):
        _, post_id, style = data.split(":", 2)
        post_id = int(post_id)
        comment_text = pending_posts[post_id]["variants"][style]

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=comment_text,
            reply_to_message_id=post_id
        )
        await query.edit_message_text(f"✅ Опубликовано ({style}):\n\n{comment_text}")
        del pending_posts[post_id]

    elif data.startswith("back:"):
        post_id = int(data.split(":")[1])
        variants = pending_posts[post_id]["variants"]
        post_preview = "пост"

        keyboard = []
        for style in STYLES:
            keyboard.append([InlineKeyboardButton(style, callback_data=f"style:{post_id}:{style}")])
        keyboard.append([InlineKeyboardButton("🔄 Перегенерировать всё", callback_data=f"regen:{post_id}")])

        await query.edit_message_text(
            "Выбери стиль:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("regen:"):
        post_id = int(data.split(":")[1])
        await query.edit_message_text("⏳ Генерирую новые варианты...")

        # Нужен оригинальный текст поста — пока заглушка
        await query.edit_message_text("🔄 Функция перегенерации будет добавлена в следующей версии.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
