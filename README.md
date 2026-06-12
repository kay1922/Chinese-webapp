# Chinese Web App — HSK1 Vocabulary Trainer

A mobile-first web app for learning the 150 HSK1 Chinese words: spaced-repetition flashcards, quizzes, a pinyin-matching game and handwriting practice. Built for family use — pick your name, no passwords.

## Features

- **Study** — SRS flashcards (SM-2): character + pinyin + audio, reveal the meaning, rate yourself (Forgot / Hard / Easy)
- **Quiz** — 4-choice questions in both directions (汉字 → English and English → 汉字)
- **Match** — pair 5 characters with their pinyin (translation shown as a hint); correct match plays the pronunciation
- **Write** — draw the character on a canvas from the English + pinyin prompt, then compare and self-rate
- **Audio** — pronunciation for every word, generated with gTTS on first request and cached
- **Stats & leaderboard** — progress bars, accuracy, daily streak, friendly competition between users
- Multiple named users on one device, switchable in two taps

## Tech stack

- **Backend**: Python, FastAPI + uvicorn, SQLite (no ORM)
- **Frontend**: single `index.html`, vanilla JS, no build step
- **Audio**: gTTS (Google Text-to-Speech), cached as MP3
- **Deploy**: Docker Compose

## Quick start

### Docker (production)

```bash
docker compose up -d --build
```

The app listens on `127.0.0.1:8080` (put a reverse proxy with HTTPS in front of it — the session cookie is `Secure`). SQLite data and the audio cache live in `./data/` on the host and survive rebuilds.

### Local development

```bash
pip install -r requirements.txt
python3 web.py            # http://localhost:8080
```

## How the SRS works

Each answer updates the word's SM-2 state per user:

- correct (easy): interval × ease factor, ease nudges up
- correct (hard): interval × 1.3, ease unchanged
- wrong: interval resets to 1 day, ease decreases

The next word to show is picked as: overdue reviews → random unseen word → random fallback.

## Project layout

```
web.py                 # FastAPI app: vocabulary, SQLite, SRS, REST API
static/index.html      # the whole frontend (SPA, vanilla JS)
HSK1_Vocabulary.xlsx   # word list: simplified, pinyin, English
data/                  # SQLite DB + audio cache (created at runtime, not in git)
bot.py                 # legacy Telegram bot (not deployed; needs BOT_TOKEN env)
```

API details are documented in [CLAUDE.md](CLAUDE.md).
