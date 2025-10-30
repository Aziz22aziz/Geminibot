import os
import logging
import time
import httpx  # مكتبة HTTP غير المتزامنة
import asyncio
from typing import Optional, List
from datetime import datetime, timedelta
from telegram import Update, Chat, Message
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    PicklePersistence,
)
from telegram.constants import ChatAction

# ===================== Logging Setup =====================
# [تم النقل] يجب تعريف اللوجر هنا قبل استخدامه في الدوال التالية
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# =========================================================


# ===================== Configuration =====================
# [تم التعديل] ضع المتغيرات الخاصة بك هنا مباشرة

# 1. ضع توكن بوت تيليغرام الخاص بك هنا بين علامتي الاقتباس
TELEGRAM_TOKEN = "7610087593:AAHrTdBirJY2EN1OCjGLQwhL2qJuFojXu7g" 

# 2. ضع قائمة مفاتيح Gemini API الخاصة بك هنا
# أضف مفاتيحك داخل علامات الاقتباس، مفصولة بفواصل
API_KEYS = [
    "AIzaSyAt6GzRuv34Q7RpQtwdWQBxmVcs2XFJoZk",
    "AIzaSyDAx-PuLpgGO5TgTievI7AS2DrkZv1NMGs",
"AIzaSyCnAKo-DOEQDJ0SvxHKm515w47C68pcDdU" ,
]

# --- (المتغيرات الأخرى، لا داعي لتعديلها) ---
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_MESSAGE_LENGTH = 4096
REQUEST_TIMEOUT = 30
RATE_LIMIT_SECONDS = 3
MAX_HISTORY = 10
MAX_RETRIES = 3 # عدد مرات إعادة المحاولة للخطأ المؤقت (على نفس المفتاح)
PERSISTENCE_FILE = "bot_persistence.pkl"
# =========================================================

# [تم التعديل] التحقق من أن المستخدم قام بتعديل المتغيرات
if TELEGRAM_TOKEN == "YOUR_TELEGRAM_TOKEN_HERE":
    raise SystemExit(
        "❌ خطأ: الرجاء تعديل المتغير TELEGRAM_TOKEN في الكود"
    )
if not API_KEYS or API_KEYS[0] == "YOUR_API_KEY_1_HERE":
    raise SystemExit(
        "❌ خطأ: الرجاء تعديل المتغير API_KEYS في الكود وإضافة مفتاح واحد على الأقل"
    )

logger.info(f"Loaded {len(API_KEYS)} API keys directly from code.")

# [تم التعديل] إزالة المفتاح من الرابط الأساسي
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

# ===================== Async Client Lifecycle ==================
async def post_init_client(application: Application):
    """إنشاء httpx client وتخزين المفاتيح عند بدء تشغيل البوت"""
    application.bot_data['httpx_client'] = httpx.AsyncClient(
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT
    )
    # [جديد] تخزين قائمة المفاتيح والمؤشر في سياق البوت
    application.bot_data['api_keys'] = API_KEYS
    application.bot_data['current_key_index'] = 0
    
    logger.info(f"AsyncClient initialized and {len(API_KEYS)} API keys stored in bot_data")

async def post_shutdown_client(application: Application):
    """إغلاق httpx client بأمان عند إيقاف البوت"""
    client = application.bot_data.get('httpx_client')
    if client:
        await client.aclose()
        logger.info("AsyncClient closed")
# =========================================================

class BotError(Exception):
    """استثناء مخصص للبوت"""
    pass

# ===================== Rate Limiting (Using context) =====================
def check_rate_limit(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """[محسّن] التحقق من حد السرعة للمستخدم"""
    now = datetime.now()
    last_time = context.user_data.get("last_request_time", datetime.min)
    
    if (now - last_time).total_seconds() < RATE_LIMIT_SECONDS:
        return False
    
    context.user_data["last_request_time"] = now
    return True

def get_wait_time(context: ContextTypes.DEFAULT_TYPE) -> int:
    """[محسّن] حساب وقت الانتظار المتبقي"""
    now = datetime.now()
    last_time = context.user_data.get("last_request_time", datetime.min)
    elapsed = (now - last_time).total_seconds()
    wait_time = max(0, RATE_LIMIT_SECONDS - elapsed)
    return int(wait_time)
# =========================================================

# ===================== Performance Monitor (Using context) ===============
def add_performance_metric(context: ContextTypes.DEFAULT_TYPE, duration: float):
    """[محسّن] إضافة مقياس الأداء إلى context.bot_data"""
    if "performance_times" not in context.bot_data:
        context.bot_data["performance_times"] = []
        
    performance_list = context.bot_data["performance_times"]
    performance_list.append(duration)
    if len(performance_list) > 100:
        performance_list.pop(0)

def get_avg_response_time(context: ContextTypes.DEFAULT_TYPE) -> float:
    """[محسّن] الحصول على متوسط وقت الاستجابة من context.bot_data"""
    performance_list = context.bot_data.get("performance_times", [])
    if not performance_list:
        return 0
    return sum(performance_list) / len(performance_list)
# =========================================================

# ===================== Conversation History (Using context) ==============
def add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str):
    """إضافة رسالة إلى تاريخ المحادثة في context.user_data"""
    if "history" not in context.user_data:
        context.user_data["history"] = []
        
    history = context.user_data["history"]
    history.append({"role": role, "parts": [{"text": content}]})
    
    if len(history) > MAX_HISTORY:
        context.user_data["history"] = history[-MAX_HISTORY:]

def get_history(context: ContextTypes.DEFAULT_TYPE) -> List[dict]:
    """الحصول على تاريخ المحادثة من context.user_data"""
    return context.user_data.get("history", [])

def clear_history(context: ContextTypes.DEFAULT_TYPE):
    """مسح تاريخ المحادثة من context.user_data"""
    if "history" in context.user_data:
        del context.user_data["history"]
# =========================================================

# ===================== Text Processing ===================
def smart_split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """تقسيم ذكي للنصوص الطويلة (نسخة محسنة)"""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current = ""
    paragraphs = text.split('\n\n')
    
    for para in paragraphs:
        if len(para) > max_length:
            lines = para.split('\n')
            for line in lines:
                if len(line) > max_length:
                    if current:
                        chunks.append(current.strip())
                        current = ""
                    for i in range(0, len(line), max_length):
                        chunks.append(line[i:i + max_length])
                
                elif len(current) + len(line) + 1 <= max_length:
                    current += line + '\n'
                else:
                    if current:
                        chunks.append(current.strip())
                    current = line + '\n'
            
            if current:
                chunks.append(current.strip())
                current = ""

        elif len(current) + len(para) + 2 <= max_length:
            current += para + '\n\n'
        else:
            if current:
                chunks.append(current.strip())
            current = para + '\n\n'
    
    if current:
        chunks.append(current.strip())
    
    return chunks if chunks else [text[:max_length]]
# =========================================================

# ===================== Gemini API (Async) ========================
# [تم التعديل] تعديل الدالة بالكامل لتدوير المفاتيح
async def get_gemini_response(
    prompt: str, 
    context: ContextTypes.DEFAULT_TYPE, 
    use_history: bool = False
) -> str:
    """الحصول على رد من Gemini API مع تدوير المفاتيح عند الفشل"""
    start_time = time.time()
    
    # جلب الموارد من سياق البوت
    client = context.bot_data.get('httpx_client')
    api_keys = context.bot_data.get('api_keys', [])
    start_index = context.bot_data.get('current_key_index', 0)
    
    if not client:
        logger.error("httpx_client not found in bot_data")
        raise BotError("خدمة الاتصال غير جاهزة. حاول لاحقاً.")
    
    if not api_keys:
        logger.error("No API keys found in bot_data")
        raise BotError("❌ حدث خطأ: لا توجد مفاتيح متاحة حاليًا.") # رسالة المستخدم المطلوبة

    num_keys = len(api_keys)
    
    # تجهيز محتوى الطلب
    if use_history:
        history = get_history(context)
        contents = history + [{"role": "user", "parts": [{"text": prompt}]}]
    else:
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
    payload = {"contents": contents}

    # الحلقة الخارجية: للمرور على جميع المفاتيح المتاحة
    for i in range(num_keys):
        key_index = (start_index + i) % num_keys
        current_key = api_keys[key_index]
        url = f"{GEMINI_API_URL}?key={current_key}"
        
        logger.info(f"Request to Gemini (Key Index {key_index}): {prompt[:50]}...")

        # الحلقة الداخلية: لإعادة المحاولة على *نفس المفتاح* للأخطاء المؤقتة
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.post(url, json=payload)
                
                # --- 1. حالة النجاح ---
                if response.status_code == 200:
                    data = response.json()
                    if "candidates" in data and len(data["candidates"]) > 0:
                        candidate = data["candidates"][0]
                        if "content" in candidate and "parts" in candidate["content"]:
                            parts = candidate["content"]["parts"]
                            if len(parts) > 0 and "text" in parts[0]:
                                response_text = parts[0]["text"]
                                
                                if use_history:
                                    add_to_history(context, "user", prompt)
                                    add_to_history(context, "model", response_text)
                                
                                duration = time.time() - start_time
                                add_performance_metric(context, duration)
                                
                                # [جديد] تحديث مؤشر المفتاح للطلب القادم (Round-Robin)
                                context.bot_data['current_key_index'] = (key_index + 1) % num_keys
                                
                                logger.info(f"Response generated in {duration:.2f}s using key index {key_index}")
                                return response_text
                    
                    # إذا كان الرد 200 ولكن بدون محتوى صالح
                    logger.warning(f"Key index {key_index} returned 200 but no content. Trying next key.")
                    break # الانتقال للمفتاح التالي

                # --- 2. خطأ في المفتاح/الحصة (4xx) -> الانتقال للمفتاح التالي ---
                if response.status_code in [400, 403, 429]:
                    error_msg = f"HTTP {response.status_code}"
                    try:
                        error_data = response.json()
                        if "error" in error_data:
                            error_msg = error_data["error"].get("message", error_msg)
                    except: pass
                    logger.warning(f"Key index {key_index} failed ({error_msg}). Trying next key.")
                    break # اكسر الحلقة الداخلية (attempts) وانتقل للحلقة الخارجية (keys)

                # --- 3. خطأ مؤقت في الخادم (5xx) -> إعادة المحاولة بنفس المفتاح ---
                if response.status_code in [500, 503] and attempt < MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Error {response.status_code} on key {key_index}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue # أعد المحاولة (الحلقة الداخلية)

                # --- 4. خطأ HTTP آخر (أو آخر محاولة 5xx) -> الانتقال للمفتاح التالي ---
                logger.warning(f"HTTP {response.status_code} on key {key_index} after {attempt + 1} attempts. Trying next key.")
                break # اكسر الحلقة الداخلية (attempts) وانتقل للحلقة الخارجية (keys)

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                if attempt < MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    logger.warning(f"{type(e).__name__} on key {key_index}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue # أعد المحاولة (الحلقة الداخلية)
                
                logger.error(f"{type(e).__name__} on key {key_index} after {MAX_RETRIES} attempts. Trying next key.")
                break # اكسر الحلقة الداخلية (attempts) وانتقل للحلقة الخارجية (keys)
            
            except Exception as e:
                logger.exception(f"Unexpected error on key {key_index} (Attempt {attempt + 1}). Trying next key.")
                break # اكسر الحلقة الداخلية (attempts) وانتقل للحلقة الخارجية (keys)

    # [جديد] إذا انتهت الحلقة الخارجية دون إرجاع رد، فكل المفاتيح فشلت
    logger.error("All API keys failed for this request.")
    raise BotError("❌ حدث خطأ: لا توجد مفاتيح متاحة حاليًا.")
# =========================================================

# ===================== Telegram Handlers =================
async def send_typing_action(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """إرسال إشارة الكتابة"""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as e:
        logger.warning(f"Failed to send typing action: {e}")

async def split_and_send_message(
    message: Message, 
    text: str, 
    reply_to_message_id: Optional[int] = None
):
    """تقسيم وإرسال الرسائل الطويلة"""
    chunks = smart_split_message(text)
    
    for i, chunk in enumerate(chunks):
        try:
            prefix = f"📄 الجزء {i + 1}/{len(chunks)}\n\n" if len(chunks) > 1 else ""
            await message.reply_text(
                prefix + chunk, 
                reply_to_message_id=reply_to_message_id if i == 0 else None
            )
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Failed to send message chunk {i + 1}: {e}")
            if i == 0:
                await message.reply_text("❌ حدث خطأ في إرسال الرد")
                break

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /start"""
    user = update.effective_user
    chat_type = update.effective_chat.type
    
    if chat_type == Chat.PRIVATE:
        welcome_msg = (
            f"👋 مرحباً {user.first_name}!\n\n"
            f"🤖 أنا بوت ذكاء اصطناعي مدعوم بـ Gemini\n\n"
            f"📝 الأوامر المتاحة:\n"
            f"• /ask <سؤال> - طرح سؤال\n"
            f"• /clear - مسح ذاكرة المحادثة\n"
            f"• /stats - إحصائيات البوت\n"
            f"• /help - المساعدة\n\n"
            f"💡 أرسل رسالة مباشرة للحصول على رد!"
        )
    else:
        welcome_msg = (
            f"👋 مرحباً! أنا بوت ذكاء اصطناعي\n\n"
            f"📝 للتفاعل معي في المجموعة:\n"
            f"• اذكرني: @{context.bot.username} <سؤالك>\n"
            f"• أو استخدم: /ask <سؤالك>\n\n"
            f"💡 استخدم /help لمزيد من المعلومات"
        )
    
    await update.message.reply_text(welcome_msg)
    logger.info(f"User {user.id} ({user.username}) started bot in {chat_type}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /help"""
    help_text = (
        "🤖 دليل استخدام البوت:\n\n"
        "📌 الأوامر الأساسية:\n"
        "• /start - رسالة الترحيب\n"
        "• /help - عرض هذه المساعدة\n"
        "• /ask <سؤال> - طرح سؤال\n"
        "• /clear - مسح ذاكرة المحادثة\n"
        "• /stats - عرض الإحصائيات\n\n"
        "💬 في المحادثات الخاصة:\n"
        "أرسل سؤالك مباشرة بدون أوامر\n\n"
        "👥 في المجموعات:\n"
        f"اذكر البوت @{context.bot.username} متبوعاً بسؤالك\n\n"
        "✨ المزايا:\n"
        "• ذاكرة محادثة ذكية (يتم حفظها)\n"
        "• دعم الرسائل الطويلة\n"
        "• حماية من الإزعاج\n\n"
        "⚡️ مدعوم بـ: Google Gemini"
    )
    await update.message.reply_text(help_text)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /clear"""
    clear_history(context)
    await update.message.reply_text(
        "🗑️ تم مسح ذاكرة المحادثة بنجاح!\n"
        "يمكنك الآن بدء محادثة جديدة."
    )
    logger.info(f"User {update.effective_user.id} cleared conversation history")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج أمر /stats"""
    avg_time = get_avg_response_time(context) # استخدام context
    history_count = len(get_history(context))
    # [جديد] إظهار عدد المفاتيح
    keys_count = len(context.bot_data.get('api_keys', []))
    
    stats_text = (
        "📊 إحصائيات البوت:\n\n"
        f"⏱️ متوسط وقت الاستجابة: {avg_time:.2f}s\n"
        f"💭 رسائل في الذاكرة: {history_count}\n"
        f"🔄 الحد الأقصى للذاكرة: {MAX_HISTORY}\n"
        f"🔑 عدد مفاتيح API: {keys_count}\n"
        f"🤖 النموذج: {MODEL_NAME}\n"
    )
    
    await update.message.reply_text(stats_text)

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[محسّن] معالج أمر /ask"""
    user = update.effective_user
    text = " ".join(context.args).strip()
    
    if not text:
        await update.message.reply_text(
            "❓ الرجاء كتابة سؤالك بعد الأمر\n\n"
            "مثال: /ask ما هو تعريف الذرة؟"
        )
        return
    
    if not check_rate_limit(context):
        wait_time = get_wait_time(context)
        await update.message.reply_text(
            f"⏳ الرجاء الانتظار {wait_time} ثانية قبل إرسال سؤال آخر"
        )
        return
    
    logger.info(f"User {user.id} ({user.username}) asked: {text[:50]}...")
    
    await send_typing_action(update.effective_chat.id, context)
    
    try:
        answer = await get_gemini_response(text, context, use_history=True)
        await split_and_send_message(
            update.message, 
            f"🧠 الجواب:\n\n{answer}", 
            reply_to_message_id=update.message.message_id
        )
    except BotError as e:
        await update.message.reply_text(f"{str(e)}") # [تم التعديل] إزالة البادئة ❌
    except Exception as e:
        logger.exception("Unexpected error in ask_command")
        await update.message.reply_text("❌ حدث خطأ غير متوقع. الرجاء المحاولة لاحقاً.")

async def mention_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[محسّن] معالج المنشن في المجموعات"""
    msg = update.message
    if not msg or not msg.text:
        return
    
    bot_username = f"@{context.bot.username}"
    if bot_username not in msg.text:
        return
    
    text = msg.text.replace(bot_username, "").strip()
    if not text:
        await msg.reply_text(
            "نعم؟ 🤔\n"
            "اكتب سؤالك بعد مناداتي أو استخدم /ask"
        )
        return
    
    user = update.effective_user
    
    if not check_rate_limit(context):
        wait_time = get_wait_time(context)
        await msg.reply_text(
            f"⏳ الرجاء الانتظار {wait_time} ثانية"
        )
        return
    
    logger.info(f"User {user.id} mentioned bot: {text[:50]}...")
    
    await send_typing_action(msg.chat.id, context)

    try:
        # المجموعات لا تحتفظ بالذاكرة (use_history=False)
        answer = await get_gemini_response(text, context, use_history=False) 
        await split_and_send_message(
            msg, 
            answer, 
            reply_to_message_id=msg.message_id
        )
    except BotError as e:
        await msg.reply_text(f"{str(e)}") # [تم التعديل] إزالة البادئة ❌
    except Exception as e:
        logger.exception("Error in mention_handler")
        await msg.reply_text("❌ حدث خطأ في الخدمة. حاول لاحقاً.")

async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[محسّن] معالج الرسائل الخاصة"""
    msg = update.message
    if not msg or not msg.text:
        return
    
    text = msg.text.strip()
    if text.startswith("/"):
        return
    
    user = update.effective_user
    
    if not check_rate_limit(context):
        wait_time = get_wait_time(context)
        await msg.reply_text(
            f"⏳ الرجاء الانتظار {wait_time} ثانية قبل إرسال رسالة أخرى"
        )
        return
    
    logger.info(f"Private message from {user.id} ({user.username}): {text[:50]}...")
    
    await send_typing_action(msg.chat.id, context)
    
    try:
        answer = await get_gemini_response(text, context, use_history=True)
        await split_and_send_message(msg, answer)
    except BotError as e:
        await msg.reply_text(f"{str(e)}") # [تم التعديل] إزالة البادئة ❌
    except Exception:
        logger.exception("Error in private_message_handler")
        await msg.reply_text("❌ حدث خطأ في الخدمة. حاول لاحقاً.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأخطاء العام"""
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)
# =========================================================

# ===================== Main Function =====================
def main():
    """الدالة الرئيسية لتشغيل البوت"""
    logger.info("Starting bot initialization...")
    
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .post_init(post_init_client)      # [جديد] ربط دالة الإعداد
        .post_shutdown(post_shutdown_client) # [جديد] ربط دالة الإغلاق
        .build()
    )
    
    # إضافة المعالجات
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Entity("mention") & ~filters.COMMAND & filters.ChatType.GROUPS,
            mention_handler
        )
    )
    
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            private_message_handler
        )
    )
    
    app.add_error_handler(error_handler)
    
    logger.info("✅ Bot initialized successfully (Console logging only)")
    print("=" * 50)
    print("🤖 Bot is running...")
    print(f"📱 Model: {MODEL_NAME}")
    print(f"🔑 Keys Loaded: {len(API_KEYS)}") # [تم التعديل]
    print(f"💾 Persistence File: {PERSISTENCE_FILE}")
    print("📝 Logging: Console Only")
    print(f"⏱️  Rate Limit: {RATE_LIMIT_SECONDS}s")
    print(f"💭 Max History: {MAX_HISTORY}")
    print(f"🔄 Max Retries (per key): {MAX_RETRIES}") # [تم التعديل]
    print("=" * 50)
    print("Press Ctrl+C to stop")
    print("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        print("\n👋 Bot stopped gracefully")
    except Exception as e:
        logger.exception("Fatal error in main")
        print(f"\n❌ Fatal error: {e}")
