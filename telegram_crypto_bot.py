# -*- coding: utf-8 -*-
"""
ربات تلگرام اسکنر موقعیت معاملاتی (کریپتو + طلا/نقره + فارکس)
نسخه بهبودیافته با منوی دکمه‌ای + Keep-Alive برای Render

نحوه اجرا:
  1) pip install -r requirements.txt
  2) متغیر محیطی BOT_TOKEN را با توکنی که از @BotFather گرفتی پر کن:
       export BOT_TOKEN="توکن_خودت"
  3) python telegram_crypto_bot.py
"""

import os
import logging
import threading
import requests
from datetime import datetime, timedelta

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "متغیر محیطی BOT_TOKEN تنظیم نشده! "
        "آن را در تنظیمات سرویس (Render -> Environment) اضافه کن."
    )

CACHE: dict = {}
CACHE_TTL = timedelta(minutes=3)

FOREX_PAIRS = [
    ("EUR", "USD", "یورو / دلار آمریکا"),
    ("GBP", "USD", "پوند انگلیس / دلار آمریکا"),
    ("USD", "JPY", "دلار آمریکا / ین ژاپن"),
    ("USD", "CHF", "دلار آمریکا / فرانک سوئیس"),
    ("AUD", "USD", "دلار استرالیا / دلار آمریکا"),
    ("USD", "CAD", "دلار آمریکا / دلار کانادا"),
    ("NZD", "USD", "دلار نیوزیلند / دلار آمریکا"),
]

METALS = [
    ("XAU", "طلای جهانی (انس)", 0.008),
    ("XAG", "نقره جهانی (انس)", 0.015),
]

# ---------------------------------------------------------------------------
# Keep-Alive Web Server (برای Render Free)
# ---------------------------------------------------------------------------

flask_app = Flask("keep_alive")


@flask_app.route("/")
def home():
    return "Bot is alive!"


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# منوی اصلی (Reply Keyboard)
# ---------------------------------------------------------------------------

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("📊 اسکن کریپتو (24h)"), KeyboardButton("⚡ اسکن کریپتو (5m)")],
        [KeyboardButton("🥇 طلا"), KeyboardButton("🥈 نقره"), KeyboardButton("💱 فارکس")],
        [KeyboardButton("🔍 جستجوی ارز"), KeyboardButton("❓ راهنما")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ---------------------------------------------------------------------------
# منطق سیگنال
# ---------------------------------------------------------------------------

def analyze_signal_for_tf(current: float, high: float, low: float, tf: str) -> dict:
    if not high or not low:
        high = current
        low = current

    if tf != "24h":
        full_range = (high - low) or (current * 0.02)
        factor = {"4h": 0.45, "1h": 0.22, "5m": 0.06}.get(tf, 0.06)
        half_range = (full_range * factor) / 2
        high = current + half_range
        low = current - half_range

    if high == low:
        return {"signal": "wait", "pos": 50, "high": high, "low": low}

    pos = ((current - low) / (high - low)) * 100
    sig = "buy" if pos <= 35 else ("sell" if pos >= 72 else "wait")
    return {"signal": sig, "pos": round(pos), "high": high, "low": low}


def compute_trade_levels(r: dict) -> dict:
    high, low = r["high"], r["low"]
    rng = high - low
    levels = {"direction": r["signal"], "pos": r["pos"], "high": high, "low": low}
    if r["signal"] == "buy":
        levels.update(
            entry=low + rng * 0.20,
            sl=low * 0.983,
            tp1=low + rng * 0.50,
            tp2=low + rng * 0.75,
        )
    elif r["signal"] == "sell":
        levels.update(
            entry=high - rng * 0.08,
            sl=high * 1.017,
            tp1=high - rng * 0.40,
            tp2=high - rng * 0.65,
        )
    return levels


def fmt_price(p: float) -> str:
    if not p:
        return "$0"
    if p >= 100:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    return f"${p:.7f}"


def signal_label(sig: str) -> str:
    return {"buy": "🟢 BUY", "sell": "🔴 SELL", "wait": "🟡 انتظار"}.get(sig, "🟡 انتظار")


# ---------------------------------------------------------------------------
# دریافت داده از API های رایگان
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "TradingBot/2.0"})


def _get(url: str, timeout: int = 12) -> requests.Response:
    res = SESSION.get(url, timeout=timeout)
    res.raise_for_status()
    return res


def fetch_crypto_list(limit: int = 20) -> list:
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={limit}&page=1&sparkline=false"
    )
    data = _get(url).json()
    out = []
    for c in data:
        price = c.get("current_price") or 0
        out.append({
            "id": c["id"],
            "pair": c["symbol"].upper() + "/USDT",
            "name": c["name"],
            "current_price": price,
            "high_24h": c.get("high_24h") or price,
            "low_24h": c.get("low_24h") or price,
            "change_24h": c.get("price_change_percentage_24h") or 0,
            "rank": c.get("market_cap_rank"),
            "type": "crypto",
        })
    return out


def fetch_crypto_by_id(coin_id: str) -> dict | None:
    url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={coin_id}"
    data = _get(url).json()
    if not data:
        return None
    c = data[0]
    price = c.get("current_price") or 0
    return {
        "id": c["id"],
        "pair": c["symbol"].upper() + "/USDT",
        "name": c["name"],
        "current_price": price,
        "high_24h": c.get("high_24h") or price,
        "low_24h": c.get("low_24h") or price,
        "change_24h": c.get("price_change_percentage_24h") or 0,
        "rank": c.get("market_cap_rank"),
        "type": "crypto",
    }


def fetch_metal(sym: str, name: str, range_pct: float) -> dict:
    data = _get(f"https://api.gold-api.com/price/{sym}").json()
    price = float(data["price"])
    return {
        "id": f"metal-{sym.lower()}",
        "pair": f"{sym}/USD",
        "name": name,
        "current_price": price,
        "high_24h": price * (1 + range_pct),
        "low_24h": price * (1 - range_pct),
        "change_24h": 0,
        "rank": None,
        "type": "metal",
    }


def fetch_forex_pair(base: str, quote: str, name: str) -> dict:
    rate = _get(f"https://api.frankfurter.app/latest?from={base}&to={quote}").json()["rates"][quote]
    chg = 0.0
    try:
        yday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        res2 = SESSION.get(
            f"https://api.frankfurter.app/{yday}..?from={base}&to={quote}", timeout=10
        )
        if res2.ok:
            rates = res2.json().get("rates", {})
            dates = sorted(rates.keys())
            if dates:
                first = rates[dates[0]].get(quote)
                if first:
                    chg = ((rate - first) / first) * 100
    except Exception:
        pass

    return {
        "id": f"fx-{base.lower()}{quote.lower()}",
        "pair": f"{base}/{quote}",
        "name": name,
        "current_price": rate,
        "high_24h": rate * 1.004,
        "low_24h": rate * 0.996,
        "change_24h": chg,
        "rank": None,
        "type": "forex",
    }


# ---------------------------------------------------------------------------
# کش
# ---------------------------------------------------------------------------

def cache_put(item: dict):
    CACHE[item["id"]] = {"data": item, "ts": datetime.utcnow()}


def cache_get(item_id: str) -> dict | None:
    entry = CACHE.get(item_id)
    if not entry:
        return None
    if datetime.utcnow() - entry["ts"] > CACHE_TTL:
        return None
    return entry["data"]


def refetch(item_id: str) -> dict | None:
    try:
        if item_id.startswith("metal-"):
            sym = item_id.split("-")[1].upper()
            for s, name, pct in METALS:
                if s == sym:
                    item = fetch_metal(s, name, pct)
                    cache_put(item)
                    return item
        elif item_id.startswith("fx-"):
            rest = item_id[3:]
            for base, quote, name in FOREX_PAIRS:
                if rest == (base + quote).lower():
                    item = fetch_forex_pair(base, quote, name)
                    cache_put(item)
                    return item
        else:
            item = fetch_crypto_by_id(item_id)
            if item:
                cache_put(item)
            return item
    except Exception as e:
        log.warning(f"refetch failed for {item_id}: {e}")
    return None


# ---------------------------------------------------------------------------
# قالب‌بندی پیام‌ها
# ---------------------------------------------------------------------------

def render_quick_line(item: dict, tf: str = "24h") -> str:
    r = analyze_signal_for_tf(item["current_price"], item["high_24h"], item["low_24h"], tf)
    chg = item.get("change_24h", 0)
    chg_str = f"{'+' if chg >= 0 else ''}{chg:.2f}%"
    rank = f"#{item['rank']}  " if item.get("rank") else ""
    return (
        f"{signal_label(r['signal'])}  {rank}{item['pair']}\n"
        f"   💰 {fmt_price(item['current_price'])}  |  📈 {chg_str}  |  📍 موقعیت: {r['pos']}%"
    )


def render_full_analysis(item: dict, tf_label: str, tf: str) -> str:
    r = analyze_signal_for_tf(item["current_price"], item["high_24h"], item["low_24h"], tf)
    t = compute_trade_levels(r)

    lines = [f"📊 *{item['pair']}* — {item['name']}"]
    if item.get("rank"):
        lines[0] += f" (رتبه #{item['rank']})"
    lines.append(f"⏱ تایم‌فریم: *{tf_label}*")
    lines.append(f"💰 قیمت فعلی: *{fmt_price(item['current_price'])}*")
    if item.get("change_24h"):
        chg = item["change_24h"]
        emoji = "📈" if chg >= 0 else "📉"
        lines.append(f"{emoji} تغییر 24h: {'+' if chg>=0 else ''}{chg:.2f}%")
    lines.append(f"\n🚦 سیگنال: *{signal_label(r['signal'])}*")
    lines.append(f"📍 موقعیت در رنج: {r['pos']}%")
    lines.append(f"⬆️ سقف رنج: {fmt_price(t['high'])}")
    lines.append(f"⬇️ کف رنج: {fmt_price(t['low'])}")

    if t["direction"] == "wait":
        lines.append("\n🟡 *میانه رنج — معامله توصیه نمی‌شود*")
    else:
        dir_emoji = "🟢" if t["direction"] == "buy" else "🔴"
        lines.append(f"\n{dir_emoji} *سطوح معاملاتی:*")
        lines.append(f"🎯 نقطه ورود: *{fmt_price(t['entry'])}*")
        lines.append(f"🛑 حد ضرر (SL): {fmt_price(t['sl'])}")
        lines.append(f"✅ تارگت ۱ (TP1): {fmt_price(t['tp1'])}")
        lines.append(f"✅ تارگت ۲ (TP2): {fmt_price(t['tp2'])}")

    lines.append("\n⚠️ _صرفاً آموزشی — مسئولیت معامله با خود شماست._")
    return "\n".join(lines)


def m5_keyboard(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ تحلیل تایم ۵ دقیقه", callback_data=f"m5|{item_id}")],
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"refresh|24h|{item_id}")],
    ])


def refresh_keyboard(item_id: str, tf: str) -> InlineKeyboardMarkup:
    other_tf = "5m" if tf == "24h" else "24h"
    other_label = "⏱ تایم ۵ دقیقه" if other_tf == "5m" else "📅 تایم روزانه (24h)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"refresh|{tf}|{item_id}")],
        [InlineKeyboardButton(other_label, callback_data=f"refresh|{other_tf}|{item_id}")],
    ])


# ---------------------------------------------------------------------------
# هندلرهای دستورات
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "کاربر"
    text = (
        f"👋 سلام *{name}* عزیز!\n\n"
        "📡 *ربات اسکنر موقعیت معاملاتی*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "از دکمه‌های پایین برای استفاده استفاده کن:\n\n"
        "📊 *اسکن کریپتو (24h)* — اسکن ارزهای برتر با تایم‌فریم روزانه\n"
        "⚡ *اسکن کریپتو (5m)* — اسکن با تایم‌فریم ۵ دقیقه\n"
        "🥇 *طلا* — تحلیل XAU/USD\n"
        "🥈 *نقره* — تحلیل XAG/USD\n"
        "💱 *فارکس* — تحلیل جفت‌ارزهای اصلی\n"
        "🔍 *جستجوی ارز* — جستجو با نام یا نماد (مثلاً BTC، ethereum)\n\n"
        "⚠️ _این ابزار صرفاً آموزشی است — مسئولیت معامله با خود شماست._"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_MENU)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 *راهنمای ربات*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔹 /scan [تعداد] — اسکن کریپتو 24h (پیش‌فرض: ۲۰، حداکثر: ۵۰)\n"
        "🔹 /scan5 [تعداد] — اسکن کریپتو 5m\n"
        "🔹 /coin BTC — تحلیل یک ارز خاص\n"
        "🔹 /gold — تحلیل طلا\n"
        "🔹 /silver — تحلیل نقره\n"
        "🔹 /forex — تحلیل فارکس\n\n"
        "💡 *چطور سیگنال بخوانیم؟*\n"
        "موقعیت رنج زیر ۳۵٪ → BUY 🟢\n"
        "موقعیت رنج بالای ۷۲٪ → SELL 🔴\n"
        "بین ۳۵ تا ۷۲٪ → انتظار 🟡\n\n"
        "⚠️ _صرفاً آموزشی — مسئولیت معامله با خود شماست._"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_MENU)


async def _do_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, tf: str, tf_label: str, limit: int = 20):
    msg = await update.message.reply_text(f"⏳ در حال دریافت داده {limit} ارز برتر از بازار...")

    try:
        coins = fetch_crypto_list(limit)
    except Exception as e:
        log.error(f"fetch_crypto_list error: {e}")
        await msg.edit_text(
            "❌ خطا در اتصال به بازار.\n"
            "اگر در ایران هستی VPN را روشن کن و دوباره امتحان کن.",
            reply_markup=MAIN_MENU,
        )
        return

    for c in coins:
        cache_put(c)

    buy, sell, wait = [], [], []
    for c in coins:
        r = analyze_signal_for_tf(c["current_price"], c["high_24h"], c["low_24h"], tf)
        c["_sig"] = r["signal"]
        {"buy": buy, "sell": sell, "wait": wait}[r["signal"]].append(c)

    text = (
        f"📡 *نتیجه اسکن* [{tf_label}] — {len(coins)} ارز برتر\n"
        f"🟢 BUY: {len(buy)}  |  🔴 SELL: {len(sell)}  |  🟡 انتظار: {len(wait)}\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    buttons = []
    if buy:
        text += "\n\n*🟢 سیگنال‌های BUY:*\n"
        for c in buy[:10]:
            text += render_quick_line(c, tf) + "\n"
            buttons.append([InlineKeyboardButton(
                f"📊 {c['pair']} — تحلیل ۵ دقیقه", callback_data=f"m5|{c['id']}"
            )])

    if sell:
        text += "\n*🔴 سیگنال‌های SELL:*\n"
        for c in sell[:10]:
            text += render_quick_line(c, tf) + "\n"
            buttons.append([InlineKeyboardButton(
                f"📊 {c['pair']} — تحلیل ۵ دقیقه", callback_data=f"m5|{c['id']}"
            )])

    if not buy and not sell:
        text += "\n\n💤 هیچ سیگنال BUY/SELL واضحی پیدا نشد.\nهمه ارزها در میانه رنج هستند."

    await msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    limit = 20
    if context.args:
        try:
            limit = max(5, min(50, int(context.args[0])))
        except ValueError:
            pass
    await _do_scan(update, context, "24h", "24 ساعته", limit)


async def cmd_scan5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    limit = 20
    if context.args:
        try:
            limit = max(5, min(50, int(context.args[0])))
        except ValueError:
            pass
    await _do_scan(update, context, "5m", "5 دقیقه", limit)


async def cmd_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔍 نام یا نماد ارز را بنویس:\n\n"
            "مثال: `/coin bitcoin`\n"
            "مثال: `/coin BTC`\n"
            "مثال: `/coin ethereum`",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args).strip().lower()
    msg = await update.message.reply_text(f"⏳ در حال جستجوی «{query}»...")

    try:
        item = fetch_crypto_by_id(query)
        if not item:
            # جستجو با سیمبول از بین ۳۰۰ ارز برتر
            items = fetch_crypto_list(300)
            item = next(
                (c for c in items if c["pair"].lower().startswith(query + "/") or c["id"] == query),
                None
            )
        if not item:
            await msg.edit_text(
                f"❌ ارزی با نام «{query}» پیدا نشد.\n\n"
                "💡 آیدی دقیق کوین‌گکو را امتحان کن:\n"
                "مثال: bitcoin، ethereum، solana، cardano"
            )
            return
    except Exception as e:
        log.error(f"cmd_coin error: {e}")
        await msg.edit_text("❌ خطا در اتصال به بازار — VPN را روشن کن و دوباره تلاش کن.")
        return

    cache_put(item)
    text = render_full_analysis(item, "روزانه (24h)", "24h")
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=m5_keyboard(item["id"]))


async def cmd_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ در حال دریافت قیمت طلا...")
    try:
        item = fetch_metal("XAU", "طلای جهانی (انس)", 0.008)
    except Exception as e:
        log.error(f"cmd_gold error: {e}")
        await msg.edit_text("❌ خطا در دریافت قیمت طلا — اتصال/VPN را بررسی کن.")
        return
    cache_put(item)
    text = render_full_analysis(item, "روزانه (تخمینی)", "24h")
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=m5_keyboard(item["id"]))


async def cmd_silver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ در حال دریافت قیمت نقره...")
    try:
        item = fetch_metal("XAG", "نقره جهانی (انس)", 0.015)
    except Exception as e:
        log.error(f"cmd_silver error: {e}")
        await msg.edit_text("❌ خطا در دریافت قیمت نقره — اتصال/VPN را بررسی کن.")
        return
    cache_put(item)
    text = render_full_analysis(item, "روزانه (تخمینی)", "24h")
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=m5_keyboard(item["id"]))


async def cmd_forex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ در حال دریافت نرخ‌های فارکس...")

    text = "💱 *تحلیل جفت‌ارزهای فارکس* [روزانه — تخمینی]\n━━━━━━━━━━━━━━━━━━\n\n"
    buttons = []
    success_count = 0

    for base, quote, name in FOREX_PAIRS:
        try:
            item = fetch_forex_pair(base, quote, name)
            cache_put(item)
            success_count += 1
            text += render_quick_line(item, "24h") + "\n\n"
            buttons.append([InlineKeyboardButton(
                f"📊 {item['pair']} — تحلیل کامل", callback_data=f"m5|{item['id']}"
            )])
        except Exception as e:
            log.warning(f"forex fetch failed for {base}/{quote}: {e}")
            text += f"⚠️ {base}/{quote} — خطا در دریافت داده\n\n"

    if success_count == 0:
        await msg.edit_text(
            "❌ خطا در اتصال به frankfurter.app\n"
            "VPN را روشن کن و دوباره تلاش کن."
        )
        return

    text += "⚠️ _صرفاً آموزشی — مسئولیت معامله با خود شماست._"
    await msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------------------------------------------------------------------------
# هندلر پیام‌های متنی (منو)
# ---------------------------------------------------------------------------

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "📊 اسکن کریپتو (24h)":
        await _do_scan(update, context, "24h", "24 ساعته", 20)
    elif text == "⚡ اسکن کریپتو (5m)":
        await _do_scan(update, context, "5m", "5 دقیقه", 20)
    elif text == "🥇 طلا":
        await cmd_gold(update, context)
    elif text == "🥈 نقره":
        await cmd_silver(update, context)
    elif text == "💱 فارکس":
        await cmd_forex(update, context)
    elif text == "❓ راهنما":
        await cmd_help(update, context)
    elif text == "🔍 جستجوی ارز":
        await update.message.reply_text(
            "🔍 نام یا نماد ارز را بنویس:\n\n"
            "مثال: `/coin bitcoin`\n"
            "مثال: `/coin ETH`\n"
            "مثال: `/coin solana`",
            parse_mode="Markdown",
        )
    else:
        # اگر کاربر مستقیم نام یک ارز نوشت، جستجو کن
        if len(text) >= 2 and text.replace("/", "").isalnum():
            context.args = [text]
            await cmd_coin(update, context)


# ---------------------------------------------------------------------------
# هندلر دکمه‌های Inline
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    action = parts[0]

    if action == "m5":
        item_id = parts[1]
        item = cache_get(item_id) or refetch(item_id)
        if not item:
            await query.message.reply_text(
                "❌ داده منقضی شده — دوباره /scan یا /coin را اجرا کن."
            )
            return
        text = render_full_analysis(item, "5 دقیقه", "5m")
        await query.message.reply_text(
            text, parse_mode="Markdown", reply_markup=refresh_keyboard(item_id, "5m")
        )

    elif action == "refresh":
        tf, item_id = parts[1], parts[2]
        item = refetch(item_id) or cache_get(item_id)
        if not item:
            await query.answer("❌ داده در دسترس نیست.", show_alert=True)
            return
        tf_label = "5 دقیقه" if tf == "5m" else "روزانه (24h)"
        text = render_full_analysis(item, tf_label, tf)
        try:
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=refresh_keyboard(item_id, tf)
            )
        except Exception:
            await query.message.reply_text(
                text, parse_mode="Markdown", reply_markup=refresh_keyboard(item_id, tf)
            )


# ---------------------------------------------------------------------------
# راه‌اندازی
# ---------------------------------------------------------------------------

def main():
    keep_alive()  # شروع وب‌سرور کوچک برای Render

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("scan5", cmd_scan5))
    app.add_handler(CommandHandler("coin", cmd_coin))
    app.add_handler(CommandHandler("gold", cmd_gold))
    app.add_handler(CommandHandler("silver", cmd_silver))
    app.add_handler(CommandHandler("forex", cmd_forex))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    log.info("✅ ربات شروع به کار کرد...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
