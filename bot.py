#!/usr/bin/env python3
"""
HSK1 Chinese vocabulary practice bot.
Features: multiple choice quiz, spaced repetition, both directions, progress tracking, daily word.
"""

import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import openpyxl
from gtts import gTTS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
VOCAB_FILE = Path(__file__).parent / "HSK1_Vocabulary.xlsx"
DB_FILE    = Path(__file__).parent / "data" / "bot_data.db"
AUDIO_DIR  = Path(__file__).parent / "data" / "audio"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Load vocabulary ───────────────────────────────────────────────────────────
def load_vocabulary():
    wb = openpyxl.load_workbook(VOCAB_FILE)
    ws = wb.active
    words = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 2:          # skip title + header rows
            continue
        num, simplified, _traditional, pinyin, english = row
        if num and simplified:
            words.append({
                "id":         int(num),
                "simplified": simplified,
                "pinyin":     pinyin,
                "english":    english,
            })
    return words

WORDS      = load_vocabulary()
WORD_BY_ID = {w["id"]: w for w in WORDS}

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS user_progress (
            user_id     INTEGER,
            word_id     INTEGER,
            ease_factor REAL    DEFAULT 2.5,
            interval    INTEGER DEFAULT 1,
            next_review TEXT,
            correct     INTEGER DEFAULT 0,
            wrong       INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, word_id)
        );
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id       INTEGER PRIMARY KEY,
            total_correct INTEGER DEFAULT 0,
            total_wrong   INTEGER DEFAULT 0,
            streak        INTEGER DEFAULT 0,
            last_active   TEXT,
            daily_word    INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS quiz_sessions (
            user_id          INTEGER PRIMARY KEY,
            current_word_id  INTEGER,
            direction        TEXT,
            correct_answer   TEXT,
            session_correct  INTEGER DEFAULT 0,
            session_wrong    INTEGER DEFAULT 0,
            questions_asked  INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()

def db():
    return sqlite3.connect(DB_FILE)

def ensure_user(user_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_stats (user_id, last_active) VALUES (?,?)",
            (user_id, datetime.now().isoformat()),
        )

def get_user_stats(user_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT total_correct, total_wrong, streak, last_active, daily_word "
            "FROM user_stats WHERE user_id=?", (user_id,)
        ).fetchone()
    if row:
        return dict(zip(["total_correct","total_wrong","streak","last_active","daily_word"], row))
    return {"total_correct":0,"total_wrong":0,"streak":0,"last_active":None,"daily_word":0}

# ── Audio ─────────────────────────────────────────────────────────────────────
def get_audio_path(word: dict) -> Path:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIO_DIR / f"{word['id']}.mp3"
    if not path.exists():
        gTTS(text=word["simplified"], lang="zh-CN").save(str(path))
    return path

# ── SRS helpers ───────────────────────────────────────────────────────────────
def update_word_progress(user_id: int, word_id: int, correct: bool, hard: bool = False):
    with db() as conn:
        row = conn.execute(
            "SELECT ease_factor, interval, correct, wrong FROM user_progress "
            "WHERE user_id=? AND word_id=?", (user_id, word_id)
        ).fetchone()

    ease, interval, c_cnt, w_cnt = row if row else (2.5, 1, 0, 0)

    if correct:
        if hard:
            new_interval = max(1, int(interval * 1.3))
            new_ease     = ease
        else:
            new_interval = max(1, int(interval * ease))
            new_ease     = min(4.0, ease + 0.1)
        c_cnt += 1
    else:
        new_interval = 1
        new_ease     = max(1.3, ease - 0.2)
        w_cnt += 1

    next_review = (datetime.now() + timedelta(days=new_interval)).isoformat()

    with db() as conn:
        conn.execute("""
            INSERT INTO user_progress (user_id,word_id,ease_factor,interval,next_review,correct,wrong)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id,word_id) DO UPDATE SET
                ease_factor=excluded.ease_factor,
                interval=excluded.interval,
                next_review=excluded.next_review,
                correct=excluded.correct,
                wrong=excluded.wrong
        """, (user_id, word_id, new_ease, new_interval, next_review, c_cnt, w_cnt))

def update_user_stats(user_id: int, correct: bool):
    today = datetime.now().date().isoformat()
    stats = get_user_stats(user_id)
    last  = (stats["last_active"] or "")[:10]

    if last == today:
        streak = max(1, stats["streak"])
    elif last == (datetime.now().date() - timedelta(days=1)).isoformat():
        streak = stats["streak"] + 1
    else:
        streak = 1

    col = "total_correct" if correct else "total_wrong"
    with db() as conn:
        conn.execute(
            f"UPDATE user_stats SET {col}={col}+1, streak=?, last_active=? WHERE user_id=?",
            (streak, datetime.now().isoformat(), user_id),
        )

def get_next_word(user_id: int) -> dict:
    now = datetime.now().isoformat()
    with db() as conn:
        due = conn.execute(
            "SELECT word_id FROM user_progress WHERE user_id=? AND next_review<=? "
            "ORDER BY next_review ASC LIMIT 1", (user_id, now)
        ).fetchone()
        if due:
            return WORD_BY_ID[due[0]]

        seen = {r[0] for r in conn.execute(
            "SELECT word_id FROM user_progress WHERE user_id=?", (user_id,)
        ).fetchall()}

    new_words = [w for w in WORDS if w["id"] not in seen]
    if new_words:
        return new_words[0]           # always start from word #1
    return random.choice(WORDS)       # all seen → pick random

# ── Quiz helpers ──────────────────────────────────────────────────────────────
def make_question(word: dict, direction: str):
    """Returns (question_text, correct_answer, [4 shuffled choices])."""
    if direction == "zh_to_en":
        question = (
            f"🀄 *{word['simplified']}*\n"
            f"🔊 _{word['pinyin']}_\n\n"
            f"What does this mean in English?"
        )
        correct      = word["english"].split(";")[0].strip()
        wrong_pool   = [w["english"].split(";")[0].strip() for w in WORDS if w["id"] != word["id"]]
    else:
        english_hint = word["english"].split(";")[0].strip()
        question = (
            f"🔤 *{english_hint}*\n\n"
            f"Which Chinese character is correct?"
        )
        correct    = word["simplified"]
        wrong_pool = [w["simplified"] for w in WORDS if w["id"] != word["id"]]

    choices = random.sample(wrong_pool, 3) + [correct]
    random.shuffle(choices)
    return question, correct, choices

def save_session(user_id, word_id, direction, correct_answer,
                 s_correct=0, s_wrong=0, q_asked=1):
    with db() as conn:
        conn.execute("""
            INSERT INTO quiz_sessions
              (user_id,current_word_id,direction,correct_answer,
               session_correct,session_wrong,questions_asked)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                current_word_id=excluded.current_word_id,
                direction=excluded.direction,
                correct_answer=excluded.correct_answer,
                session_correct=excluded.session_correct,
                session_wrong=excluded.session_wrong,
                questions_asked=excluded.questions_asked
        """, (user_id, word_id, direction, correct_answer, s_correct, s_wrong, q_asked))

def get_session(user_id) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT current_word_id,direction,correct_answer,"
            "session_correct,session_wrong,questions_asked "
            "FROM quiz_sessions WHERE user_id=?", (user_id,)
        ).fetchone()
    if row:
        return dict(zip(
            ["word_id","direction","correct_answer","session_correct","session_wrong","questions_asked"],
            row
        ))
    return None

# ── Keyboard builders ─────────────────────────────────────────────────────────
LABELS = ["A", "B", "C", "D"]

def question_keyboard(choices: list[str], word_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{LABELS[i]}. {c}", callback_data=f"ans:{c}")]
            for i, c in enumerate(choices)]
    rows.append([
        InlineKeyboardButton("🔊",          callback_data=f"pronounce:{word_id}"),
        InlineKeyboardButton("⏭ Skip",     callback_data="skip"),
        InlineKeyboardButton("🏁 End Quiz", callback_data="end_quiz"),
    ])
    return InlineKeyboardMarkup(rows)

def after_answer_keyboard(s_correct, s_wrong, q_asked) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➡️ Next", callback_data=f"next:{s_correct}:{s_wrong}:{q_asked}"),
        InlineKeyboardButton("🏁 End",  callback_data="end_quiz"),
    ]])

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📚 Study (learn words)", callback_data="study_start")],
    [InlineKeyboardButton("🎯 Quiz (test yourself)", callback_data="start_quiz")],
    [InlineKeyboardButton("📊 My Stats",        callback_data="stats"),
     InlineKeyboardButton("📅 Daily Word",      callback_data="toggle_daily")],
    [InlineKeyboardButton("🔄 Reset Progress",  callback_data="reset_confirm")],
])

# ── Core: send a question ─────────────────────────────────────────────────────
async def push_question(target, user_id: int,
                        s_correct=0, s_wrong=0, q_asked=0):
    word      = get_next_word(user_id)
    direction = random.choice(["zh_to_en", "en_to_zh"])
    text, correct, choices = make_question(word, direction)

    save_session(user_id, word["id"], direction, correct,
                 s_correct, s_wrong, q_asked + 1)

    kb = question_keyboard(choices, word["id"])
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await target.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

# ── Study mode helpers ────────────────────────────────────────────────────────
def study_front_keyboard(word_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👁  Reveal meaning", callback_data=f"flip:{word_id}")],
        [InlineKeyboardButton("🔊 Pronounce",       callback_data=f"pronounce:{word_id}"),
         InlineKeyboardButton("⏭ Skip",            callback_data=f"study_skip:{word_id}"),
         InlineKeyboardButton("🏁 End",             callback_data="end_study")],
    ])

def study_back_keyboard(word_id: int, sc: int, sw: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("😰 Forgot", callback_data=f"rate:{word_id}:0:{sc}:{sw}"),
         InlineKeyboardButton("😐 Hard",   callback_data=f"rate:{word_id}:1:{sc}:{sw}"),
         InlineKeyboardButton("😊 Easy",   callback_data=f"rate:{word_id}:2:{sc}:{sw}")],
        [InlineKeyboardButton("🔊 Pronounce", callback_data=f"pronounce:{word_id}"),
         InlineKeyboardButton("🏁 End",       callback_data="end_study")],
    ])

async def push_study_card(target, user_id: int, sc: int = 0, sw: int = 0):
    word = get_next_word(user_id)
    save_session(user_id, word["id"], "study", word["simplified"], sc, sw, sc + sw)

    text = (
        f"📚 *Study Card*\n\n"
        f"🀄 *{word['simplified']}*\n"
        f"🔊 _{word['pinyin']}_\n\n"
        f"What does this mean?"
    )
    kb = study_front_keyboard(word["id"])
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await target.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "🇨🇳 *HSK1 Chinese Vocabulary Bot*\n\n"
        "📚 *Study* — learn words with flashcards (recommended first!)\n"
        "🎯 *Quiz* — multiple-choice test on what you've studied\n\n"
        "Both use spaced repetition so harder words come back sooner.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )

async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await push_question(update, update.effective_user.id)

async def cmd_study(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await push_study_card(update, update.effective_user.id)

async def cb_start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ensure_user(query.from_user.id)
    await push_question(query, query.from_user.id)

async def cb_study_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ensure_user(query.from_user.id)
    await push_study_card(query, query.from_user.id)

async def cb_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    word_id = int(query.data.split(":")[1])
    word    = WORD_BY_ID[word_id]

    session = get_session(query.from_user.id)
    sc = session["session_correct"] if session else 0
    sw = session["session_wrong"]   if session else 0

    text = (
        f"📚 *Study Card*\n\n"
        f"🀄 *{word['simplified']}*\n"
        f"🔊 _{word['pinyin']}_\n"
        f"🇬🇧 {word['english']}\n\n"
        f"Did you know this word?"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=study_back_keyboard(word_id, sc, sw),
    )

async def cb_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, word_id_s, quality_s, sc_s, sw_s = query.data.split(":")
    word_id = int(word_id_s)
    quality = int(quality_s)  # 0=forgot, 1=hard, 2=easy
    sc      = int(sc_s)
    sw      = int(sw_s)

    correct = quality > 0
    hard    = quality == 1
    update_word_progress(user_id, word_id, correct, hard=hard)
    update_user_stats(user_id, correct)

    if correct:
        sc += 1
    else:
        sw += 1

    await push_study_card(query, user_id, sc, sw)

async def cb_study_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Skipped")
    session = get_session(query.from_user.id)
    sc = session["session_correct"] if session else 0
    sw = session["session_wrong"]   if session else 0
    await push_study_card(query, query.from_user.id, sc, sw)

async def cb_pronounce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Sending audio…")
    word_id = int(query.data.split(":")[1])
    word    = WORD_BY_ID[word_id]
    path    = get_audio_path(word)
    with open(path, "rb") as f:
        await context.bot.send_voice(
            chat_id=query.message.chat_id,
            voice=f,
            caption=f"🔊 *{word['simplified']}* — {word['pinyin']}",
            parse_mode="Markdown",
        )

async def cb_end_study(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = get_session(user_id)

    sc = session["session_correct"] if session else 0
    sw = session["session_wrong"]   if session else 0
    total = sc + sw

    with db() as conn:
        learned = conn.execute(
            "SELECT COUNT(*) FROM user_progress WHERE user_id=? AND correct>=2", (user_id,)
        ).fetchone()[0]

    pct = int(sc / total * 100) if total else 0
    await query.edit_message_text(
        f"📚 *Study session ended!*\n\n"
        f"Cards reviewed: {total}\n"
        f"😊 Easy / knew: {sc} ({pct}%)\n"
        f"😰 Forgot: {sw}\n\n"
        f"📖 Words learned so far: {learned}/{len(WORDS)}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 Keep Studying", callback_data="study_start")],
            [InlineKeyboardButton("🎯 Quiz Yourself",  callback_data="start_quiz")],
            [InlineKeyboardButton("🏠 Menu",           callback_data="menu")],
        ]),
    )

async def cb_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    session = get_session(user_id)
    if not session:
        await query.edit_message_text("No active quiz. Use /start.")
        return

    chosen  = query.data[4:]          # strip "ans:"
    correct = session["correct_answer"]
    word    = WORD_BY_ID[session["word_id"]]
    ok      = chosen.strip().lower() == correct.strip().lower()

    update_word_progress(user_id, session["word_id"], ok)
    update_user_stats(user_id, ok)

    sc = session["session_correct"] + (1 if ok else 0)
    sw = session["session_wrong"]   + (0 if ok else 1)
    qa = session["questions_asked"]

    # Persist updated counters so End Quiz reads correct values
    save_session(user_id, session["word_id"], session["direction"],
                 session["correct_answer"], sc, sw, qa)

    if ok:
        verdict = "✅ *Correct!*"
    else:
        verdict = f"❌ *Wrong!*  The answer was: *{correct}*"

    word_card = (
        f"\n\n📖 *{word['simplified']}*\n"
        f"🔊 _{word['pinyin']}_\n"
        f"🇬🇧 {word['english']}"
    )
    score = f"\n\n📊 Session: ✅ {sc}  ❌ {sw}"

    await query.edit_message_text(
        verdict + word_card + score,
        parse_mode="Markdown",
        reply_markup=after_answer_keyboard(sc, sw, qa),
    )

async def cb_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, sc, sw, qa = query.data.split(":")
    await push_question(query, query.from_user.id, int(sc), int(sw), int(qa))

async def cb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Skipped")
    session = get_session(query.from_user.id)
    sc = session["session_correct"] if session else 0
    sw = session["session_wrong"]   if session else 0
    qa = session["questions_asked"] if session else 0
    await push_question(query, query.from_user.id, sc, sw, qa)

async def cb_end_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    session = get_session(user_id)
    stats   = get_user_stats(user_id)

    with db() as conn:
        learned = conn.execute(
            "SELECT COUNT(*) FROM user_progress WHERE user_id=? AND correct>=2", (user_id,)
        ).fetchone()[0]

    if session:
        total = session["session_correct"] + session["session_wrong"]
        pct   = int(session["session_correct"] / total * 100) if total else 0
        sess_block = (
            f"*This session:*\n"
            f"  Questions: {total}\n"
            f"  ✅ {session['session_correct']} correct ({pct}%)\n"
            f"  ❌ {session['session_wrong']} wrong\n\n"
        )
    else:
        sess_block = ""

    total_ans = stats["total_correct"] + stats["total_wrong"]
    accuracy  = int(stats["total_correct"] / total_ans * 100) if total_ans else 0

    overall = (
        f"*Overall:*\n"
        f"  📚 Learned: {learned}/{len(WORDS)} words\n"
        f"  🎯 Accuracy: {accuracy}%\n"
        f"  🔥 Streak: {stats['streak']} days"
    )

    await query.edit_message_text(
        f"🏁 *Quiz ended!*\n\n{sess_block}{overall}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 New Quiz",    callback_data="start_quiz")],
            [InlineKeyboardButton("📊 Full Stats",  callback_data="stats")],
            [InlineKeyboardButton("🏠 Menu",        callback_data="menu")],
        ]),
    )

def build_stats_text(user_id: int) -> str:
    ensure_user(user_id)
    stats = get_user_stats(user_id)
    with db() as conn:
        seen = conn.execute(
            "SELECT COUNT(*) FROM user_progress WHERE user_id=? AND correct>=1", (user_id,)
        ).fetchone()[0]
        learned = conn.execute(
            "SELECT COUNT(*) FROM user_progress WHERE user_id=? AND correct>=2", (user_id,)
        ).fetchone()[0]
        hard = conn.execute(
            "SELECT COUNT(*) FROM user_progress WHERE user_id=? AND wrong>correct", (user_id,)
        ).fetchone()[0]
    total = stats["total_correct"] + stats["total_wrong"]
    acc   = int(stats["total_correct"] / total * 100) if total else 0
    return (
        f"📊 *Your Statistics*\n\n"
        f"📚 Vocabulary:\n"
        f"  Seen:    {seen}/{len(WORDS)}\n"
        f"  Learned: {learned}/{len(WORDS)}\n"
        f"  Hard:    {hard} words\n\n"
        f"🎯 Performance:\n"
        f"  Total answers: {total}\n"
        f"  Correct: {stats['total_correct']} ({acc}%)\n"
        f"  Wrong:   {stats['total_wrong']}\n\n"
        f"🔥 Streak: {stats['streak']} days"
    )

STATS_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📚 Study",      callback_data="study_start")],
    [InlineKeyboardButton("🎯 Start Quiz", callback_data="start_quiz")],
    [InlineKeyboardButton("🏠 Menu",       callback_data="menu")],
])

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        build_stats_text(update.effective_user.id),
        parse_mode="Markdown",
        reply_markup=STATS_MENU,
    )

async def cb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        build_stats_text(query.from_user.id),
        parse_mode="Markdown",
        reply_markup=STATS_MENU,
    )

async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🇨🇳 *HSK1 Vocabulary Bot* — Main Menu",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )

async def cb_toggle_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    stats   = get_user_stats(user_id)
    new_val = 1 - stats["daily_word"]
    with db() as conn:
        conn.execute("UPDATE user_stats SET daily_word=? WHERE user_id=?", (new_val, user_id))
    status = "enabled ✅" if new_val else "disabled ❌"
    note   = "You will receive one word every day at 9:00 AM." if new_val else ""
    await query.edit_message_text(
        f"📅 Daily Word {status}\n\n{note}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Menu", callback_data="menu")
        ]]),
    )

async def cb_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚠️ *Reset Progress*\n\nThis deletes all progress, stats, and streaks. Sure?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Yes, reset everything", callback_data="reset_do")],
            [InlineKeyboardButton("❌ Cancel",                callback_data="menu")],
        ]),
    )

async def cb_reset_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    with db() as conn:
        conn.execute("DELETE FROM user_progress  WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM quiz_sessions  WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE user_stats SET total_correct=0,total_wrong=0,streak=0 WHERE user_id=?",
            (user_id,),
        )
    await query.edit_message_text(
        "✅ Progress reset! Starting fresh.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎯 Start Quiz", callback_data="start_quiz")
        ]]),
    )

# ── Daily word job ─────────────────────────────────────────────────────────────
async def daily_word_job(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        users = [r[0] for r in conn.execute(
            "SELECT user_id FROM user_stats WHERE daily_word=1"
        ).fetchall()]

    word = random.choice(WORDS)
    text = (
        f"📅 *Daily Word*\n\n"
        f"🀄 *{word['simplified']}*\n"
        f"🔊 _{word['pinyin']}_\n"
        f"🇬🇧 {word['english']}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎯 Practice Now", callback_data="start_quiz")
    ]])
    for uid in users:
        try:
            await context.bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.warning("Could not send daily word to %s: %s", uid, e)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set")
    init_db()
    logger.info("Loaded %d words from %s", len(WORDS), VOCAB_FILE)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("quiz",  cmd_quiz))
    app.add_handler(CommandHandler("study", cmd_study))
    app.add_handler(CommandHandler("stats", cmd_stats))

    app.add_handler(CallbackQueryHandler(cb_study_start,    pattern="^study_start$"))
    app.add_handler(CallbackQueryHandler(cb_flip,           pattern="^flip:"))
    app.add_handler(CallbackQueryHandler(cb_rate,           pattern="^rate:"))
    app.add_handler(CallbackQueryHandler(cb_study_skip,     pattern="^study_skip:"))
    app.add_handler(CallbackQueryHandler(cb_end_study,      pattern="^end_study$"))
    app.add_handler(CallbackQueryHandler(cb_pronounce,      pattern="^pronounce:"))
    app.add_handler(CallbackQueryHandler(cb_start_quiz,     pattern="^start_quiz$"))
    app.add_handler(CallbackQueryHandler(cb_answer,         pattern="^ans:"))
    app.add_handler(CallbackQueryHandler(cb_next,           pattern="^next:"))
    app.add_handler(CallbackQueryHandler(cb_skip,           pattern="^skip$"))
    app.add_handler(CallbackQueryHandler(cb_end_quiz,       pattern="^end_quiz$"))
    app.add_handler(CallbackQueryHandler(cb_stats,          pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(cb_menu,           pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(cb_toggle_daily,   pattern="^toggle_daily$"))
    app.add_handler(CallbackQueryHandler(cb_reset_confirm,  pattern="^reset_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_reset_do,       pattern="^reset_do$"))

    # Daily word at 09:00 every day
    from datetime import time as dtime
    app.job_queue.run_daily(daily_word_job, time=dtime(9, 0))

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
