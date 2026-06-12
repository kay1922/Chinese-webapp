# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Deployment (production)

The app runs in Docker on this server (localhost). Claude Code has direct access.

```bash
# Deploy / rebuild after changes
docker compose up -d --build

# Logs
docker logs chineesebot

# Stop
docker compose down
```

SQLite database is stored in `./data/bot_data.db` on the host (mounted into the container at `/app/data/`), so it survives image rebuilds. The `data/` directory is excluded from `.dockerignore`.

Audio cache lives in `./data/audio/{word_id}.mp3` — generated on first request via gTTS, cached permanently.

## Running locally (dev)

```bash
pip install -r requirements.txt --break-system-packages
python3 web.py        # web interface on :8080
```

The legacy Telegram bot (`bot.py`) is not part of the Docker image. To run it locally:
`pip install "python-telegram-bot[job-queue]"` and set the `BOT_TOKEN` env variable (never hardcode it).

## Architecture

The primary interface is a mobile web app served by **`web.py`** (FastAPI + uvicorn on port 8080).
The legacy Telegram bot (`bot.py`) still exists but is not run by the container.

### web.py structure (top-to-bottom)

1. **Vocabulary loading** — `load_vocabulary()` reads `HSK1_Vocabulary.xlsx` at startup into `WORDS` list and `WORD_BY_ID` dict. Each word: `{id, simplified, pinyin, english}`.

2. **Database layer** — Two SQLite tables:
   - `user_progress` — per-user, per-word SRS state (`ease_factor`, `interval`, `next_review`, `correct`, `wrong`)
   - `user_stats` — per-user totals, streak, `name` (TEXT, nullable — named users only)

3. **SRS logic** — `update_word_progress(uid, word_id, correct, hard=False)` implements SM-2:
   - Easy correct: interval × ease_factor, ease nudges up
   - Hard correct: interval × 1.3, ease unchanged
   - Wrong: interval reset to 1, ease decreases
   - `get_next_word()` priority: overdue → random unseen → random fallback

4. **User system** — Named user profiles stored in `user_stats.name`. No auth, just a numeric user_id in an HTTP-only cookie. Users are created/selected via the user picker UI.

5. **REST API endpoints:**
   - `GET  /api/me` — current user (returns `{user_id, name}`, null if no cookie)
   - `GET  /api/users` — list named users
   - `POST /api/users/create` — create named user `{name}`, sets cookie
   - `POST /api/users/select` — switch to existing user `{user_id}`, sets cookie
   - `GET  /api/stats` — progress stats for current user
   - `GET  /api/study/next` — next word for study mode
   - `POST /api/study/rate` — rate a study card `{word_id, quality}` (0=forgot, 1=hard, 2=easy)
   - `GET  /api/quiz/next` — next quiz question with shuffled, deduplicated choices
   - `POST /api/quiz/answer` — check answer `{word_id, chosen, direction}` (correctness computed server-side)
   - `GET  /api/match/next` — 5 pairs for the matching game (pinyin unique within a round)
   - `POST /api/match/attempt` — record a pairing attempt `{zh_word_id, py_word_id}`; wrong attempt penalises both words
   - `GET  /api/audio/{word_id}` — serve/generate pronunciation MP3
   - `GET  /api/leaderboard` — all named users ranked by learned/accuracy
   - `POST /api/reset` — delete current user's progress
   - `POST /api/logout` — clear cookie
   - `POST /api/account/delete` — delete user and all progress

   Endpoints are plain `def` (not `async`) on purpose: they do blocking sqlite/gTTS work, FastAPI runs them in a threadpool.

6. **Cache-busting** — `index.html` is served with `Cache-Control: no-store` so browsers always get the latest JS.

### static/index.html

Single-page app (vanilla JS, no build step). Screens: `users` (picker) → `home` → `study` / `quiz` / `match` / `write` / `stats`.

- **Study mode**: show character + pinyin + audio → reveal English → rate (Forgot/Hard/Easy)
- **Quiz mode**: 4-choice question; `en_to_zh` choices show character + pinyin below
- **Match mode**: 5 hanzi tiles (left) vs 5 pinyin+translation tiles (right); correct match plays audio; every attempt is POSTed to `/api/match/attempt`
- **Write mode**: prompt with English+pinyin → draw on canvas → reveal → self-rate (uses `/api/study/*`)
- Audio plays inline via HTML5 `<audio>`, no page scrolling needed
- Tab bar hidden on user picker screen; shown after login
- User-provided strings (names) must be rendered with `textContent`, never `innerHTML` (XSS)
