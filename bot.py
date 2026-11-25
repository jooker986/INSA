import os
import signal
import sys
import sqlite3
import subprocess
import threading
from datetime import datetime
import telebot
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# إعدادات قناة الاشتراك الإجباري
CHANNEL_USERNAME = "@Jookerrr98"
CHANNEL_URL = "https://t.me/Jookerrr98"

if not BOT_TOKEN:
    raise ValueError("خطأ: تأكد من إضافة BOT_TOKEN في GitHub Secrets")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
DB_PATH = "instapay_data.db"

MAX_SINGLE_TX = 70000.0
MAX_DAILY_LIMIT = 120000.0
MAX_MONTHLY_LIMIT = 400000.0

user_states = {}
git_lock = threading.Lock()
db_lock = threading.Lock()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    return conn


def sync_db_to_github(blocking=False):
    """رفع قاعدة البيانات فوراً إلى GitHub بعد أي تعديل مع منع التعارض"""
    def _push():
        with git_lock:
            try:
                subprocess.run(["git", "config", "--local", "user.email", "action@github.com"], check=False)
                subprocess.run(["git", "config", "--local", "user.name", "GitHub Action"], check=False)
                subprocess.run(["git", "add", DB_PATH], check=False)
                status = subprocess.run(["git", "status", "--porcelain", DB_PATH], capture_output=True, text=True)
                if status.stdout.strip():
                    subprocess.run(["git", "commit", "-m", "Instant DB backup"], check=False)
                    subprocess.run(["git", "pull", "--no-rebase", "-X", "ours", "origin", "HEAD"], check=False)
                    subprocess.run(["git", "push", "origin", "HEAD"], check=False)
            except Exception as e:
                print(f"Git sync error: {e}")

    if blocking:
        _push()
    else:
        threading.Thread(target=_push, daemon=False).start()


def graceful_exit(signum, frame):
    print("Stopping bot gracefully and saving final DB state...")
    bot.stop_polling()
    sync_db_to_github(blocking=True)
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def is_subscribed(user_id: int) -> bool:
    """التحقق من اشتراك المستخدم في القناة"""
    if is_admin(user_id):
        return True
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        print(f"Subscription check error: {e}")
        return False


def subscription_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📢 اشترك في القناة أولاً", url=CHANNEL_URL),
        types.InlineKeyboardButton("✅ تم الاشتراك / تحقق الآن", callback_data="check_sub")
    )
    return kb


def subscription_text():
    return (
        "⚠️ <b>عذراً، يجب الاشتراك في القناة أولاً لاستخدام البوت!</b>\n\n"
        f"🔹 القناة: {CHANNEL_USERNAME}\n"
        "اضغط على زر الاشتراك بالأسفل، ثم اضغط على <b>(✅ تم الاشتراك / تحقق الآن)</b>."
    )


def calculate_instapay_fee(amount: float) -> float:
    raw_fee = amount * 0.001
    fee = max(0.50, min(raw_fee, 20.0))
    return round(fee, 2)


def init_db():
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                joined_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                fee REAL DEFAULT 0.0,
                total REAL NOT NULL,
                note TEXT,
                date_day TEXT NOT NULL,
                date_month TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()


def register_user(user):
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %I:%M %p")
        c.execute("""
            INSERT OR IGNORE INTO users (user_id, full_name, username, joined_at)
            VALUES (?, ?, ?, ?)
        """, (user.id, user.full_name, user.username or "", now))
        conn.commit()
        conn.close()


def get_balance(user_id: int) -> float:
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT COALESCE(SUM(
                CASE 
                    WHEN type='in' THEN total 
                    WHEN type='out' THEN -total 
                    ELSE 0 
                END
            ), 0)
            FROM transactions
            WHERE user_id = ?
        """, (user_id,))
        bal = c.fetchone()[0]
        conn.close()
    return round(bal, 2)


def get_limits_usage(user_id: int):
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")

    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id = ? AND type='out' AND date_day=?
        """, (user_id, today_str))
        daily_used = c.fetchone()[0]

        c.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id = ? AND type='out' AND date_month=?
        """, (user_id, month_str))
        monthly_used = c.fetchone()[0]

        conn.close()
    return round(daily_used, 2), round(monthly_used, 2)


def add_transaction(user_id: int, tx_type: str, amount: float, fee: float, note: str):
    now = datetime.now()
    total = round(amount + fee, 2) if tx_type == "out" else round(amount, 2)
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO transactions (user_id, type, amount, fee, total, note, date_day, date_month, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            tx_type,
            amount,
            fee,
            total,
            note,
            now.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m"),
            now.strftime("%Y-%m-%d %I:%M %p")
        ))
        conn.commit()
        conn.close()
    sync_db_to_github()


def get_recent_transactions(user_id: int, limit=15):
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT id, type, amount, fee, total, note, created_at
            FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        rows = c.fetchall()
        conn.close()
    return rows


def delete_last_transaction(user_id: int):
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, type, total, note FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        last = c.fetchone()
        if last:
            c.execute("DELETE FROM transactions WHERE id = ?", (last[0],))
            conn.commit()
        conn.close()
    if last:
        sync_db_to_github()
    return last


def get_admin_stats():
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        users_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM transactions")
        tx_count = c.fetchone()[0]
        conn.close()
    return users_count, tx_count


def main_menu_keyboard(user_id: int):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ استلام / إيداع", callback_data="tx_in"),
        types.InlineKeyboardButton("💸 تحويل InstaPay (برسوم)", callback_data="tx_out_fee"),
    )
    kb.add(
        types.InlineKeyboardButton("➖ خصم / سحب (بدون رسوم)", callback_data="tx_out_nofee"),
        types.InlineKeyboardButton("📊 حدود التحويل", callback_data="limits"),
    )
    kb.add(
        types.InlineKeyboardButton("📜 سجل العمليات", callback_data="history"),
        types.InlineKeyboardButton("↩️ تراجع عن آخر عملية", callback_data="undo_confirm"),
    )
    kb.add(
        types.InlineKeyboardButton("🔄 استعلام / تحديث الرصيد", callback_data="refresh"),
    )
    if is_admin(user_id):
        kb.add(
            types.InlineKeyboardButton("👑 إحصائيات البوت", callback_data="admin_stats"),
            types.InlineKeyboardButton("💾 نسخة احتياطية (DB)", callback_data="backup"),
        )
    return kb


def main_dashboard_text(user_id: int):
    bal = get_balance(user_id)
    daily_used, _ = get_limits_usage(user_id)
    daily_rem = max(0.0, MAX_DAILY_LIMIT - daily_used)
    return (
        f"💳 <b>مدير حساب InstaPay الشخصي</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>رصيدك الحالي:</b> <code>{bal:,.2f} ج.م</code>\n"
        f"📉 <b>المتبقي من حد اليوم:</b> <code>{daily_rem:,.2f} ج.م</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"اختر العملية المطلوبة:"
    )


@bot.message_handler(commands=["start", "balance"])
def handle_start(message):
    uid = message.from_user.id
    if not is_subscribed(uid):
        bot.send_message(message.chat.id, subscription_text(), reply_markup=subscription_keyboard())
        return

    register_user(message.from_user)
    user_states.pop(uid, None)
    bot.send_message(message.chat.id, main_dashboard_text(uid), reply_markup=main_menu_keyboard(uid))


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    uid = call.from_user.id
    data = call.data

    if data == "check_sub":
        if is_subscribed(uid):
            register_user(call.from_user)
            bot.answer_callback_query(call.id, "تم التحقق من اشتراكك بنجاح! أهلاً بك ✅")
            bot.edit_message_text(
                main_dashboard_text(uid),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_menu_keyboard(uid)
            )
        else:
            bot.answer_callback_query(call.id, "❌ لم تشترك في القناة بعد! اشترك أولاً ثم اضغط تحقق.", show_alert=True)
        return

    if not is_subscribed(uid):
        bot.answer_callback_query(call.id, "⚠️ يجب الاشتراك في القناة أولاً!", show_alert=True)
        try:
            bot.edit_message_text(
                subscription_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=subscription_keyboard()
            )
        except Exception:
            pass
        return

    register_user(call.from_user)

    if data == "refresh":
        user_states.pop(uid, None)
        try:
            bot.edit_message_text(
                main_dashboard_text(uid),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_menu_keyboard(uid)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "تم تحديث الرصيد ✅")

    elif data in ("tx_in", "tx_out_fee", "tx_out_nofee"):
        titles = {
            "tx_in": "➕ استلام / إيداع رصيد",
            "tx_out_fee": "💸 تحويل InstaPay (يُحسب عليه رسوم 0.1%)",
            "tx_out_nofee": "➖ خصم / سحب مباشر (بدون رسوم)"
        }
        user_states[uid] = {"step": "amount", "mode": data}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="refresh"))
        bot.edit_message_text(
            f"<b>{titles[data]}</b>\n\nأرسل <b>المبلغ</b> الآن بالأرقام (مثال: <code>1500</code> أو <code>250.5</code>):",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)

    elif data == "skip_note":
        state = user_states.get(uid)
        if state and state.get("step") == "note":
            tx_type = "in" if state["mode"] == "tx_in" else "out"
            add_transaction(uid, tx_type, state["amount"], state["fee"], "")
            user_states.pop(uid, None)
            bot.edit_message_text(
                f"✅ <b>تم تسجيل العملية بنجاح!</b>\n\n{main_dashboard_text(uid)}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_menu_keyboard(uid)
            )
        bot.answer_callback_query(call.id)

    elif data == "limits":
        daily_used, monthly_used = get_limits_usage(uid)
        daily_rem = max(0.0, MAX_DAILY_LIMIT - daily_used)
        monthly_rem = max(0.0, MAX_MONTHLY_LIMIT - monthly_used)

        text = (
            f"📊 <b>حالة حدود التحويل (InstaPay)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>الحد الأقصى للمعاملة الواحدة:</b>\n"
            f"   <code>{MAX_SINGLE_TX:,.2f} ج.م</code>\n\n"
            f"📅 <b>الحد اليومي (120,000 ج.م):</b>\n"
            f"   • المستهلك اليوم: <code>{daily_used:,.2f} ج.م</code>\n"
            f"   • المتبقي اليوم: <code>{daily_rem:,.2f} ج.م</code>\n\n"
            f"🗓️ <b>الحد الشهري (400,000 ج.م):</b>\n"
            f"   • المستهلك الشهر: <code>{monthly_used:,.2f} ج.م</code>\n"
            f"   • المتبقي الشهر: <code>{monthly_rem:,.2f} ج.م</code>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="refresh"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif data == "history":
        rows = get_recent_transactions(uid, 15)
        if not rows:
            bot.answer_callback_query(call.id, "لا توجد عمليات مسجلة بعد.", show_alert=True)
            return

        lines = ["📜 <b>آخر 15 عملية:</b>\n"]
        for r_id, r_type, amount, fee, total, note, dt in rows:
            if r_type == "in":
                lines.append(f"🟢 <b>+{total:,.2f} ج.م</b> {f'({note})' if note else ''}\n   🕒 <i>{dt}</i>")
            else:
                fee_txt = f" [رسوم: {fee:.2f}]" if fee > 0 else ""
                lines.append(f"🔴 <b>-{total:,.2f} ج.م</b> (أساسي: {amount:,.2f}{fee_txt}) {f'({note})' if note else ''}\n   🕒 <i>{dt}</i>")

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="refresh"))
        bot.edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif data == "undo_confirm":
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ نعم، احذفها", callback_data="undo_exec"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="refresh")
        )
        bot.edit_message_text(
            "⚠️ هل أنت متأكد من حذف <b>آخر عملية</b> مسجلة وإرجاع الرصيد؟",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)

    elif data == "undo_exec":
        deleted = delete_last_transaction(uid)
        if deleted:
            bot.answer_callback_query(call.id, f"تم حذف العملية ({deleted[2]:,.2f} ج.م) ✅", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "مفيش عمليات للحذف.", show_alert=True)
        bot.edit_message_text(
            main_dashboard_text(uid),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu_keyboard(uid)
        )

    elif data == "admin_stats" and is_admin(uid):
        users_count, tx_count = get_admin_stats()
        bot.answer_callback_query(
            call.id,
            f"👑 إحصائيات البوت:\n👥 عدد المستخدمين: {users_count}\n📊 إجمالي العمليات: {tx_count}",
            show_alert=True
        )

    elif data == "backup" and is_admin(uid):
        sync_db_to_github(blocking=True)
        if os.path.exists(DB_PATH):
            with open(DB_PATH, "rb") as f:
                bot.send_document(
                    call.message.chat.id,
                    f,
                    caption=f"💾 نسخة احتياطية كاملة (وتم الرفع إلى GitHub)\n📅 {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"
                )
            bot.answer_callback_query(call.id, "تم إرسال الملف ورفعه ✅")
        else:
            bot.answer_callback_query(call.id, "لا توجد بيانات بعد.")


@bot.message_handler(func=lambda m: True)
def handle_inputs(message):
    uid = message.from_user.id
    if not is_subscribed(uid):
        bot.send_message(message.chat.id, subscription_text(), reply_markup=subscription_keyboard())
        return

    register_user(message.from_user)
    state = user_states.get(uid)

    if not state:
        bot.send_message(message.chat.id, main_dashboard_text(uid), reply_markup=main_menu_keyboard(uid))
        return

    if state["step"] == "amount":
        try:
            amount = float(message.text.strip().replace(",", ""))
            if amount <= 0:
                raise ValueError

            mode = state["mode"]
            fee = calculate_instapay_fee(amount) if mode == "tx_out_fee" else 0.0
            total = round(amount + fee, 2)

            warnings = []
            if mode in ("tx_out_fee", "tx_out_nofee"):
                current_bal = get_balance(uid)
                daily_used, monthly_used = get_limits_usage(uid)

                if total > current_bal:
                    warnings.append(f"⚠️ تنبيه: المبلغ الإجمالي ({total:,.2f}) أكبر من رصيدك المسجل ({current_bal:,.2f})!")
                if mode == "tx_out_fee" and amount > MAX_SINGLE_TX:
                    warnings.append(f"⚠️ تنبيه: المبلغ يتخطى الحد الأقصى للمعاملة الواحدة ({MAX_SINGLE_TX:,.0f} ج.م).")
                if (daily_used + amount) > MAX_DAILY_LIMIT:
                    warnings.append(f"⚠️ تنبيه: ستتخطى الحد اليومي ({MAX_DAILY_LIMIT:,.0f} ج.م).")
                if (monthly_used + amount) > MAX_MONTHLY_LIMIT:
                    warnings.append(f"⚠️ تنبيه: ستتخطى الحد الشهري ({MAX_MONTHLY_LIMIT:,.0f} ج.م).")

            state["amount"] = amount
            state["fee"] = fee
            state["step"] = "note"

            warn_block = ("\n\n" + "\n".join(warnings)) if warnings else ""
            fee_block = f"\n💸 رسوم التحويل (0.1%): <code>{fee:.2f} ج.م</code>\n📉 إجمالي الخصم: <code>{total:,.2f} ج.م</code>" if fee > 0 else ""

            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⏭️ تخطي الملاحظة وحفظ", callback_data="skip_note"))

            bot.send_message(
                message.chat.id,
                f"💵 المبلغ: <b>{amount:,.2f} ج.م</b>{fee_block}{warn_block}\n\n"
                f"اكتب <b>اسم الطرف / ملاحظة</b> (أو اضغط تخطي):",
                reply_markup=kb
            )
        except ValueError:
            bot.send_message(message.chat.id, "⚠️ من فضلك أدخل رقم صحيح أكبر من الصفر (مثال: <code>500</code>).")

    elif state["step"] == "note":
        note = "" if message.text.strip() == "-" else message.text.strip()
        tx_type = "in" if state["mode"] == "tx_in" else "out"
        add_transaction(uid, tx_type, state["amount"], state["fee"], note)
        user_states.pop(uid, None)
        bot.send_message(
            message.chat.id,
            f"✅ <b>تم تسجيل العملية بنجاح!</b>\n\n{main_dashboard_text(uid)}",
            reply_markup=main_menu_keyboard(uid)
        )


if __name__ == "__main__":
    init_db()
    sync_db_to_github(blocking=True)
    print("🚀 Multi-user InstaPay-Manager Bot with Force Subscribe is running...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
