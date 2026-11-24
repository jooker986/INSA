import os
import signal
import sys
import sqlite3
from datetime import datetime
import telebot
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN or not ADMIN_ID:
    raise ValueError("خطأ: تأكد من إضافة BOT_TOKEN و ADMIN_ID في GitHub Secrets")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
DB_PATH = "instapay_data.db"

MAX_SINGLE_TX = 70000.0
MAX_DAILY_LIMIT = 120000.0
MAX_MONTHLY_LIMIT = 400000.0

user_states = {}


def graceful_exit(signum, frame):
    print("Stopping bot gracefully to save database...")
    bot.stop_polling()
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


def calculate_instapay_fee(amount: float) -> float:
    raw_fee = amount * 0.001
    fee = max(0.50, min(raw_fee, 20.0))
    return round(fee, 2)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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


def get_balance() -> float:
    conn = sqlite3.connect(DB_PATH)
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
    """)
    bal = c.fetchone()[0]
    conn.close()
    return round(bal, 2)


def get_limits_usage():
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='out' AND date_day=?", (today_str,))
    daily_used = c.fetchone()[0]

    c.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='out' AND date_month=?", (month_str,))
    monthly_used = c.fetchone()[0]

    conn.close()
    return round(daily_used, 2), round(monthly_used, 2)


def add_transaction(tx_type: str, amount: float, fee: float, note: str):
    now = datetime.now()
    total = round(amount + fee, 2) if tx_type == "out" else round(amount, 2)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO transactions (type, amount, fee, total, note, date_day, date_month, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
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


def get_recent_transactions(limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, type, amount, fee, total, note, created_at
        FROM transactions ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


def delete_last_transaction():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, type, total, note FROM transactions ORDER BY id DESC LIMIT 1")
    last = c.fetchone()
    if last:
        c.execute("DELETE FROM transactions WHERE id = ?", (last[0],))
        conn.commit()
    conn.close()
    return last


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def main_menu_keyboard():
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
        types.InlineKeyboardButton("💾 نسخة احتياطية (DB)", callback_data="backup"),
        types.InlineKeyboardButton("🔄 استعلام / تحديث الرصيد", callback_data="refresh"),
    )
    return kb


def main_dashboard_text():
    bal = get_balance()
    daily_used, _ = get_limits_usage()
    daily_rem = max(0.0, MAX_DAILY_LIMIT - daily_used)
    return (
        f"💳 <b>مدير حساب InstaPay الشخصي</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>الرصيد الحالي:</b> <code>{bal:,.2f} ج.م</code>\n"
        f"📉 <b>المتبقي من حد اليوم:</b> <code>{daily_rem:,.2f} ج.م</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"اختر العملية المطلوبة:"
    )


@bot.message_handler(commands=["start", "balance"])
def handle_start(message):
    if not is_admin(message.from_user.id):
        return
    user_states.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, main_dashboard_text(), reply_markup=main_menu_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ غير مصرح لك.")
        return

    uid = call.from_user.id
    data = call.data

    if data == "refresh":
        user_states.pop(uid, None)
        try:
            bot.edit_message_text(
                main_dashboard_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_menu_keyboard()
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
            add_transaction(tx_type, state["amount"], state["fee"], "")
            user_states.pop(uid, None)
            bot.edit_message_text(
                f"✅ <b>تم تسجيل العملية بنجاح!</b>\n\n{main_dashboard_text()}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_menu_keyboard()
            )
        bot.answer_callback_query(call.id)

    elif data == "limits":
        daily_used, monthly_used = get_limits_usage()
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
        rows = get_recent_transactions(15)
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
        deleted = delete_last_transaction()
        if deleted:
            bot.answer_callback_query(call.id, f"تم حذف العملية ({deleted[2]:,.2f} ج.م) ✅", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "مفيش عمليات للحذف.", show_alert=True)
        bot.edit_message_text(
            main_dashboard_text(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu_keyboard()
        )

    elif data == "backup":
        if os.path.exists(DB_PATH):
            with open(DB_PATH, "rb") as f:
                bot.send_document(
                    call.message.chat.id,
                    f,
                    caption=f"💾 نسخة احتياطية من قاعدة البيانات\n📅 {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"
                )
            bot.answer_callback_query(call.id, "تم إرسال الملف ✅")
        else:
            bot.answer_callback_query(call.id, "لا توجد بيانات بعد.")


@bot.message_handler(func=lambda m: True)
def handle_inputs(message):
    if not is_admin(message.from_user.id):
        return

    uid = message.from_user.id
    state = user_states.get(uid)

    if not state:
        bot.send_message(message.chat.id, main_dashboard_text(), reply_markup=main_menu_keyboard())
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
                current_bal = get_balance()
                daily_used, monthly_used = get_limits_usage()

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
        add_transaction(tx_type, state["amount"], state["fee"], note)
        user_states.pop(uid, None)
        bot.send_message(
            message.chat.id,
            f"✅ <b>تم تسجيل العملية بنجاح!</b>\n\n{main_dashboard_text()}",
            reply_markup=main_menu_keyboard()
        )


if __name__ == "__main__":
    init_db()
    print("🚀 InstaPay-Manager Bot is running on GitHub Actions...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
