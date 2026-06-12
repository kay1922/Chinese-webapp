#!/usr/bin/env python3
import logging
import random
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import openpyxl
import uvicorn
from fastapi import FastAPI, Cookie, Request, Response, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from gtts import gTTS
from pydantic import BaseModel

# ── Config ─────────────────────────────────────────────────────────────────────
VOCAB_FILE = Path(__file__).parent / "HSK1_Vocabulary.xlsx"
DB_FILE    = Path(__file__).parent / "data" / "bot_data.db"
AUDIO_DIR  = Path(__file__).parent / "data" / "audio"
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Vocabulary ─────────────────────────────────────────────────────────────────
def load_vocabulary():
    wb = openpyxl.load_workbook(VOCAB_FILE)
    ws = wb.active
    words = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 2:
            continue
        num, simplified, _trad, pinyin, english = row
        if num and simplified:
            words.append({"id": int(num), "simplified": simplified, "pinyin": pinyin, "english": english})
    return words

WORDS      = load_vocabulary()
WORD_BY_ID = {w["id"]: w for w in WORDS}

# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_progress (
                user_id INTEGER, word_id INTEGER,
                ease_factor REAL DEFAULT 2.5, interval INTEGER DEFAULT 1,
                next_review TEXT, correct INTEGER DEFAULT 0, wrong INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, word_id)
            );
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                total_correct INTEGER DEFAULT 0, total_wrong INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0, last_active TEXT, daily_word INTEGER DEFAULT 0
            );
        """)
        # migrate: add name column if not present
        try:
            conn.execute("ALTER TABLE user_stats ADD COLUMN name TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_name ON user_stats(name)")
        except sqlite3.DatabaseError:
            logger.warning("Could not create unique index on user_stats.name (duplicate names?)")

def db():
    return sqlite3.connect(DB_FILE)

def ensure_user(uid: int):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO user_stats (user_id, last_active) VALUES (?,?)",
                     (uid, datetime.now().isoformat()))

def get_user_stats(uid: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT total_correct,total_wrong,streak,last_active,name FROM user_stats WHERE user_id=?", (uid,)
        ).fetchone()
    if row:
        return dict(zip(["total_correct", "total_wrong", "streak", "last_active", "name"], row))
    return {"total_correct": 0, "total_wrong": 0, "streak": 0, "last_active": None, "name": None}

def update_word_progress(uid: int, word_id: int, correct: bool, hard: bool = False):
    with db() as conn:
        row = conn.execute(
            "SELECT ease_factor,interval,correct,wrong FROM user_progress WHERE user_id=? AND word_id=?",
            (uid, word_id)
        ).fetchone()
        ease, interval, c_cnt, w_cnt = row if row else (2.5, 1, 0, 0)
        if correct:
            new_interval = max(1, int(interval * (1.3 if hard else ease)))
            new_ease     = ease if hard else min(4.0, ease + 0.1)
            c_cnt += 1
        else:
            new_interval = 1
            new_ease     = max(1.3, ease - 0.2)
            w_cnt += 1
        next_review = (datetime.now() + timedelta(days=new_interval)).isoformat()
        conn.execute("""
            INSERT INTO user_progress (user_id,word_id,ease_factor,interval,next_review,correct,wrong)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id,word_id) DO UPDATE SET
                ease_factor=excluded.ease_factor, interval=excluded.interval,
                next_review=excluded.next_review, correct=excluded.correct, wrong=excluded.wrong
        """, (uid, word_id, new_ease, new_interval, next_review, c_cnt, w_cnt))

def update_user_stats(uid: int, correct: bool):
    today = datetime.now().date().isoformat()
    s     = get_user_stats(uid)
    last  = (s["last_active"] or "")[:10]
    if last == today:
        streak = max(1, s["streak"])
    elif last == (datetime.now().date() - timedelta(days=1)).isoformat():
        streak = s["streak"] + 1
    else:
        streak = 1
    col = "total_correct" if correct else "total_wrong"
    with db() as conn:
        conn.execute(f"UPDATE user_stats SET {col}={col}+1, streak=?, last_active=? WHERE user_id=?",
                     (streak, datetime.now().isoformat(), uid))

def get_next_word(uid: int) -> dict:
    now = datetime.now().isoformat()
    with db() as conn:
        due = conn.execute(
            "SELECT word_id FROM user_progress WHERE user_id=? AND next_review<=? ORDER BY next_review ASC LIMIT 1",
            (uid, now)
        ).fetchone()
        if due:
            return WORD_BY_ID[due[0]]
        seen = {r[0] for r in conn.execute("SELECT word_id FROM user_progress WHERE user_id=?", (uid,)).fetchall()}
    unseen = [w for w in WORDS if w["id"] not in seen]
    # randomise so every session feels different (not always word #1 first)
    return random.choice(unseen) if unseen else random.choice(WORDS)

def pick_match_words(uid: int, n: int) -> list:
    """Pick n words for a match round: due first, then unseen, then random.
    Pinyin must be unique within the round (他/她 are both "tā"), otherwise
    matching becomes ambiguous."""
    now = datetime.now().isoformat()
    chosen, picked, pinyins = [], set(), set()

    def try_add(w) -> None:
        py = (w["pinyin"] or "").strip().lower()
        if w["id"] in picked or py in pinyins:
            return
        chosen.append(w); picked.add(w["id"]); pinyins.add(py)

    with db() as conn:
        due = conn.execute(
            "SELECT word_id FROM user_progress WHERE user_id=? AND next_review<=? ORDER BY next_review ASC LIMIT ?",
            (uid, now, n)
        ).fetchall()
        seen = {r[0] for r in conn.execute("SELECT word_id FROM user_progress WHERE user_id=?", (uid,)).fetchall()}
    for (wid,) in due:
        if wid in WORD_BY_ID and len(chosen) < n:
            try_add(WORD_BY_ID[wid])
    if len(chosen) < n:
        unseen = [w for w in WORDS if w["id"] not in seen]
        random.shuffle(unseen)
        for w in unseen:
            if len(chosen) >= n: break
            try_add(w)
    if len(chosen) < n:
        rest = list(WORDS)
        random.shuffle(rest)
        for w in rest:
            if len(chosen) >= n: break
            try_add(w)
    random.shuffle(chosen)
    return chosen[:n]

# ── Audio ──────────────────────────────────────────────────────────────────────
def get_audio_path(word: dict) -> Path:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIO_DIR / f"{word['id']}.mp3"
    if not path.exists():
        gTTS(text=word["simplified"], lang="zh-CN").save(str(path))
    return path

# ── Session helpers ────────────────────────────────────────────────────────────
def uid_from_cookie(user_id: Optional[str]) -> Optional[int]:
    if user_id and user_id.isdigit():
        uid = int(user_id)
        with db() as conn:
            if conn.execute("SELECT 1 FROM user_stats WHERE user_id=?", (uid,)).fetchone():
                return uid
    return None

COOKIE_OPTS = dict(max_age=365 * 24 * 3600, httponly=True, samesite="lax", path="/", secure=True)

def set_uid_cookie(response: Response, uid: int):
    response.set_cookie("user_id", str(uid), **COOKIE_OPTS)

# ── App ────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Loaded %d words", len(WORDS))
    yield

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def refresh_session_cookie(request: Request, call_next):
    """Re-stamp the cookie on every response so active users never get logged out.
    Skip if the endpoint already wrote a new user_id cookie (login / logout / switch)."""
    response = await call_next(request)
    endpoint_set_cookie = any(
        b"user_id=" in value
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    )
    if not endpoint_set_cookie:
        uid_str = request.cookies.get("user_id")
        if uid_str and uid_str.isdigit():
            response.set_cookie("user_id", uid_str, **COOKIE_OPTS)
    return response

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate"}

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

# ── User management ────────────────────────────────────────────────────────────
@app.get("/api/me")
def me(user_id: Optional[str] = Cookie(default=None)):
    uid = uid_from_cookie(user_id)
    if uid is None:
        return {"user_id": None, "name": None}
    s = get_user_stats(uid)
    return {"user_id": uid, "name": s["name"]}

@app.get("/api/users")
def list_users():
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, name FROM user_stats WHERE name IS NOT NULL ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]

class CreateUserBody(BaseModel):
    name: str

@app.post("/api/users/create")
def create_user(body: CreateUserBody, response: Response):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with db() as conn:
        existing = conn.execute("SELECT user_id FROM user_stats WHERE name=?", (name,)).fetchone()
        if existing:
            uid = existing[0]
        else:
            try:
                conn.execute("INSERT INTO user_stats (name, last_active) VALUES (?,?)",
                             (name, datetime.now().isoformat()))
                uid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            except sqlite3.IntegrityError:
                uid = conn.execute("SELECT user_id FROM user_stats WHERE name=?", (name,)).fetchone()[0]
    set_uid_cookie(response, uid)
    return {"user_id": uid, "name": name}

class SelectUserBody(BaseModel):
    user_id: int

@app.post("/api/users/select")
def select_user(body: SelectUserBody, response: Response):
    with db() as conn:
        row = conn.execute("SELECT name FROM user_stats WHERE user_id=?", (body.user_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    set_uid_cookie(response, body.user_id)
    return {"user_id": body.user_id, "name": row[0]}

# ── Stats ──────────────────────────────────────────────────────────────────────
def require_uid(user_id: Optional[str]) -> int:
    uid = uid_from_cookie(user_id)
    if uid is None:
        raise HTTPException(401, "No user selected")
    return uid

@app.get("/api/stats")
def stats_api(user_id: Optional[str] = Cookie(default=None)):
    uid = require_uid(user_id)
    s   = get_user_stats(uid)
    now = datetime.now().isoformat()
    with db() as conn:
        seen    = conn.execute("SELECT COUNT(*) FROM user_progress WHERE user_id=? AND correct>=1", (uid,)).fetchone()[0]
        learned = conn.execute("SELECT COUNT(*) FROM user_progress WHERE user_id=? AND correct>=2", (uid,)).fetchone()[0]
        due     = conn.execute("SELECT COUNT(*) FROM user_progress WHERE user_id=? AND next_review<=?", (uid, now)).fetchone()[0]
    total = s["total_correct"] + s["total_wrong"]
    return {
        "seen": seen, "learned": learned, "total": len(WORDS),
        "total_correct": s["total_correct"], "total_wrong": s["total_wrong"],
        "accuracy": int(s["total_correct"] / total * 100) if total else 0,
        "streak": s["streak"], "due": due, "name": s["name"],
    }

# ── Study ──────────────────────────────────────────────────────────────────────
@app.get("/api/study/next")
def study_next(user_id: Optional[str] = Cookie(default=None)):
    uid  = require_uid(user_id)
    word = get_next_word(uid)
    with db() as conn:
        seen_count = conn.execute("SELECT COUNT(*) FROM user_progress WHERE user_id=?", (uid,)).fetchone()[0]
    return {
        "word_id": word["id"], "simplified": word["simplified"],
        "pinyin": word["pinyin"], "english": word["english"],
        "seen_count": seen_count, "total": len(WORDS),
    }

class RateBody(BaseModel):
    word_id: int
    quality: int  # 0=forgot 1=hard 2=easy

@app.post("/api/study/rate")
def study_rate(body: RateBody, user_id: Optional[str] = Cookie(default=None)):
    uid = require_uid(user_id)
    if body.word_id not in WORD_BY_ID:
        raise HTTPException(404, "Unknown word")
    update_word_progress(uid, body.word_id, body.quality > 0, hard=body.quality == 1)
    update_user_stats(uid, body.quality > 0)
    return {"ok": True}

# ── Quiz ───────────────────────────────────────────────────────────────────────
def first_english(word: dict) -> str:
    return word["english"].split(";")[0].strip()

def quiz_correct_text(word: dict, direction: str) -> str:
    return first_english(word) if direction == "zh_to_en" else word["simplified"]

@app.get("/api/quiz/next")
def quiz_next(user_id: Optional[str] = Cookie(default=None)):
    uid       = require_uid(user_id)
    word      = get_next_word(uid)
    direction = random.choice(["zh_to_en", "en_to_zh"])
    correct   = quiz_correct_text(word, direction)
    # distractors must not collide with the correct answer or each other
    # (HSK1 has duplicate glosses: 会/能 both "can", 他/她 both "tā")
    pool = [w for w in WORDS if w["id"] != word["id"]
            and first_english(w).lower() != first_english(word).lower()]
    random.shuffle(pool)
    choices, seen_texts = [], {correct.lower()}
    for w in pool:
        if direction == "zh_to_en":
            text, pinyin = first_english(w), None
        else:
            text, pinyin = w["simplified"], w["pinyin"]
        if text.lower() in seen_texts:
            continue
        seen_texts.add(text.lower())
        choices.append({"text": text, "pinyin": pinyin})
        if len(choices) == 3:
            break
    choices.append({"text": correct, "pinyin": word["pinyin"] if direction == "en_to_zh" else None})
    random.shuffle(choices)
    return {
        "word_id": word["id"], "simplified": word["simplified"],
        "pinyin": word["pinyin"], "english": word["english"],
        "direction": direction, "correct": correct, "choices": choices,
    }

class AnswerBody(BaseModel):
    word_id: int
    chosen: str
    direction: str

@app.post("/api/quiz/answer")
def quiz_answer(body: AnswerBody, user_id: Optional[str] = Cookie(default=None)):
    uid = require_uid(user_id)
    if body.word_id not in WORD_BY_ID or body.direction not in ("zh_to_en", "en_to_zh"):
        raise HTTPException(404, "Unknown word")
    correct = quiz_correct_text(WORD_BY_ID[body.word_id], body.direction)
    ok = body.chosen.strip().lower() == correct.strip().lower()
    update_word_progress(uid, body.word_id, ok)
    update_user_stats(uid, ok)
    return {"correct": ok}

# ── Match ──────────────────────────────────────────────────────────────────────
MATCH_PAIRS = 5

@app.get("/api/match/next")
def match_next(user_id: Optional[str] = Cookie(default=None)):
    uid   = require_uid(user_id)
    words = pick_match_words(uid, MATCH_PAIRS)
    return {
        "pairs": [
            {
                "word_id": w["id"], "simplified": w["simplified"],
                "pinyin": w["pinyin"], "english": first_english(w),
            }
            for w in words
        ]
    }

class MatchAttemptBody(BaseModel):
    zh_word_id: int
    py_word_id: int

@app.post("/api/match/attempt")
def match_attempt(body: MatchAttemptBody, user_id: Optional[str] = Cookie(default=None)):
    uid = require_uid(user_id)
    if body.zh_word_id not in WORD_BY_ID or body.py_word_id not in WORD_BY_ID:
        raise HTTPException(404, "Unknown word")
    ok = body.zh_word_id == body.py_word_id
    if ok:
        update_word_progress(uid, body.zh_word_id, True)
        update_user_stats(uid, True)
    else:
        for wid in (body.zh_word_id, body.py_word_id):
            update_word_progress(uid, wid, False)
            update_user_stats(uid, False)
    return {"correct": ok}

# ── Audio ──────────────────────────────────────────────────────────────────────
@app.get("/api/audio/{word_id}")
def audio(word_id: int):
    if word_id not in WORD_BY_ID:
        raise HTTPException(404)
    return FileResponse(get_audio_path(WORD_BY_ID[word_id]), media_type="audio/mpeg")

# ── Reset ──────────────────────────────────────────────────────────────────────
@app.get("/api/leaderboard")
def leaderboard(user_id: Optional[str] = Cookie(default=None)):
    uid = uid_from_cookie(user_id)
    with db() as conn:
        users = conn.execute("""
            SELECT s.user_id, s.name, s.total_correct, s.total_wrong, s.streak,
                   (SELECT COUNT(*) FROM user_progress p
                    WHERE p.user_id = s.user_id AND p.correct >= 2) AS learned
            FROM user_stats s WHERE s.name IS NOT NULL
        """).fetchall()

    rows = []
    for u_id, name, t_correct, t_wrong, streak, learned in users:
        total = t_correct + t_wrong
        rows.append({
            "name":     name,
            "learned":  learned,
            "accuracy": int(t_correct / total * 100) if total else 0,
            "streak":   streak,
            "is_me":    u_id == uid,
        })

    rows.sort(key=lambda x: (x["learned"], x["accuracy"]), reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows

@app.post("/api/reset")
def reset(user_id: Optional[str] = Cookie(default=None)):
    uid = require_uid(user_id)
    with db() as conn:
        conn.execute("DELETE FROM user_progress WHERE user_id=?", (uid,))
        conn.execute("UPDATE user_stats SET total_correct=0,total_wrong=0,streak=0 WHERE user_id=?", (uid,))
    return {"ok": True}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("user_id", path="/")
    return {"ok": True}

@app.post("/api/account/delete")
def delete_account(response: Response, user_id: Optional[str] = Cookie(default=None)):
    uid = require_uid(user_id)
    with db() as conn:
        conn.execute("DELETE FROM user_progress WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM user_stats WHERE user_id=?", (uid,))
    response.delete_cookie("user_id", path="/")
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
