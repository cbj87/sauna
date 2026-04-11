# Sweat Box — CLAUDE.md

## Project Overview
**Sweat Box** is a family sauna booking and real-time control system. Flask backend + React SPA frontend (no build step — inline Babel). Integrates with Harvia MyHarvia cloud API for hardware control. Deployed on Railway with SQLite.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Flask 3.0+, Python |
| Database | SQLite (WAL mode), SQLAlchemy 2.0+ |
| Auth | 4-digit PIN, bcrypt, Flask sessions, CSRF tokens |
| Frontend | React 18 (CDN), Tailwind CSS (CDN), inline Babel |
| Push | VAPID Web Push (pywebpush) |
| Scheduling | APScheduler (60s jobs) |
| Hardware | Harvia MyHarvia GraphQL API via Cognito SRP auth |
| Deployment | Railway, Gunicorn (1 worker, 4 threads) |

---

## Key Files

| File | Purpose |
|------|---------|
| `harvia_server.py` | Main Flask server — all 39 API routes |
| `harvia_client.py` | Harvia cloud API client (Cognito auth, GraphQL) |
| `models.py` | SQLAlchemy models: FamilyMember, Booking, Preset, PushSubscription, ControlLog |
| `static/index.html` | Entire React frontend (2,400+ lines, all inline) |
| `static/sw.js` | Service worker — offline caching + push notification handling |
| `static/manifest.json` | PWA manifest |
| `.env.example` | Required environment variables template |

---

## Running Locally

```bash
pip install -r requirements.txt
cp .env.example .env.local
# Fill in .env.local (APP_SECRET_KEY required; Harvia creds optional for UI-only dev)
python harvia_server.py
# App runs at http://localhost:5000
```

For UI-only development without Harvia credentials, the sauna status endpoints will error but the booking/auth/member flows work fine.

---

## Environment Variables

```
APP_SECRET_KEY=<32+ random chars>      # Required; refuses to start with default
APP_TIMEZONE=Australia/Sydney          # IANA tz; all scheduler/booking logic uses this
HARVIA_USERNAME=<myharvia-email>
HARVIA_PASSWORD=<myharvia-password>
HARVIA_DEVICE_ID=<uuid>
VAPID_PRIVATE_KEY=<base64>             # Generate: python generate_vapid_keys.py
VAPID_PUBLIC_KEY=<base64>
VAPID_CLAIMS_EMAIL=<email>
DB_PATH=/data/sweatbox.db              # Railway volume path; defaults to ./sweatbox.db
PORT=5000
```

---

## Architecture & Conventions

### Backend
- All routes in `harvia_server.py`; organized by feature group (auth, admin, sauna, bookings, push)
- `err(msg, code)` helper returns `{error: msg}` JSON with HTTP status
- `app_now()` returns current datetime in `APP_TIMEZONE` — always use this, never `datetime.now()`
- DB sessions: `SessionLocal()` per request, always close in `finally`
- Thread safety: `_booking_lock` (booking creation), `_login_lock` (rate limiting), `_stats_lock` (Harvia stats)
- DB migrations: `_migrate_db()` runs safe `ALTER TABLE` statements on startup

### Frontend
- Single `index.html` — all React components inline, no build process
- `api(path, options)` wrapper handles fetch, CSRF, timeouts (10s), 401 session expiry
- Temperature: always stored in °C; frontend has °C/°F toggle for display only
- Dates: stored as YYYY-MM-DD strings; times as HH:MM strings
- Toast notifications: `showToast(msg, type)` — stacked, auto-dismiss after 3.5s
- `localDate()` — gets today's date in user's local timezone

### Database
- Temperature always in °C
- `Booking.end_time < Booking.start_time` signals a midnight-spanning booking
- `notification_prefs` stored as JSON text in FamilyMember
- `ControlLog.notes` stores JSON-encoded extras (light, fan, steamEn)

### Roles
- **Admin**: full access — member approval, preset management, DB browser, control log
- **User**: own bookings + sauna controls (subject to `max_temp` limit if set by admin)
- First signup is auto-approved as admin; subsequent signups require admin approval

### Background Jobs (APScheduler, every 60s)
1. `check_and_auto_shutoff()` — advance booking states, turn off sauna when session ends
2. `check_preheat_reminders()` — push notification 35 min before session start
3. `check_session_ending()` — push notification 15 min before session end
4. `refresh_harvia_token()` — proactive Cognito token refresh (every 30 min)

---

## API Route Summary

| Group | Routes |
|-------|--------|
| Auth | POST /api/auth/signup, /login, /logout; GET /api/auth/me |
| Admin members | GET/POST /api/admin/members; POST approve/reject; PUT/DELETE /api/admin/members/<id> |
| Members | GET /api/members; PUT /api/members/<id> |
| Sauna | GET /api/sauna/status; POST /on, /off, /extend, /set, /preset/<name> |
| Presets | GET /api/presets; PUT/DELETE /api/admin/presets/<name> |
| Bookings | GET/POST /api/bookings; PUT/DELETE /api/bookings/<id>; POST /preheat |
| Push | GET vapid-key; POST subscribe/unsubscribe/test |
| Admin utils | GET harvia-stats, control_log, db/<table>; PUT/DELETE db/<table>/<id> |

---

## Booking Rules
- 15-minute cooldown buffer enforced between bookings (server-side)
- `_booking_lock` prevents race conditions on concurrent creation
- Preheat window: 90 minutes before session start
- Midnight-spanning: allowed when `end_time < start_time` (e.g. 23:00–01:00)
- Member `max_temp` ceiling enforced at both booking creation and sauna on/set

---

## Local Dev — PWA & Push Notification Testing

Port 5000 is blocked on this Mac by macOS Control Center (AirPlay). The app auto-assigns a free port when started via the Claude preview server (`.claude/launch.json`). Check the assigned port in the preview start output.

### Full PWA testing with ngrok (HTTPS required for push notifications)

1. **Start the local server** via Claude preview — note the assigned port (e.g. 53092)
2. **Start ngrok tunnel:**
   ```bash
   ngrok http <port>
   ```
3. **Open the ngrok HTTPS URL** in Chrome or Safari — click "Visit Site" past the ngrok warning page
4. Log in and the bell icon will appear in the header for notification testing

### Why ngrok?
- Service workers and Web Push require HTTPS (browsers exempt `localhost` for SW but not always for push)
- ngrok gives a real `https://` URL, enabling the full PWA install prompt + push subscription flow
- ngrok is installed via Homebrew: `brew install ngrok/ngrok/ngrok`

### VAPID keys for local push testing
VAPID keys are in `.env.local` (pulled from Railway). The `.claude/launch.json` exports them as env vars so the preview server picks them up. If the bell icon is missing, the server doesn't have VAPID keys in its environment — restart the preview server after any env changes.

### Syncing Railway DB locally
```bash
railway ssh "python3 -c \"
import sqlite3, base64, sys
src = sqlite3.connect('/data/sweatbox.db')
dst = sqlite3.connect('/tmp/sweatbox_backup.db')
src.backup(dst)
src.close(); dst.close()
with open('/tmp/sweatbox_backup.db','rb') as f:
    sys.stdout.buffer.write(base64.b64encode(f.read()))
\"" 2>/dev/null | base64 -d > sweatbox_local.db
```
`DB_PATH` in `.env.local` points to `sweatbox_local.db`. The `models.py` fallback only applies when `DB_PATH` is not set — if it is set, it's always used.

---

## Deployment (Railway)
- `Procfile`: `gunicorn harvia_server:app --workers 1 --threads 4 --timeout 120`
- Railway volume at `/data`; set `DB_PATH=/data/sweatbox.db`
- First deploy: POST /api/auth/signup to create admin account
- Health check: `GET /health` → `{ok: true}`
