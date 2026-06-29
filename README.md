# Sweat Box

A family sauna booking and real-time control system. Book sessions, preheat remotely, and control your Harvia sauna from any device — installable as a PWA with push notifications.

## Features

- **Booking system** — schedule sauna sessions with conflict detection and 15-minute cooldown buffers between bookings; midnight-spanning sessions supported
- **Real-time sauna control** — turn on/off, adjust temperature, fan, and light via the Harvia MyHarvia cloud API
- **Presets** — admins define named temperature/fan/light presets; any member can activate them
- **Preheat** — start the sauna up to 90 minutes before a session with one tap
- **Push notifications** — Web Push alerts 35 minutes before a session starts and 15 minutes before it ends
- **Multi-member access** — family members sign up and await admin approval; per-member temperature ceilings can be set
- **Admin tools** — member management, preset editing, DB browser, control log, Harvia hardware stats
- **PWA** — installable on iOS and Android; service worker for offline caching
- **Password reset** — email-based reset via Resend

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Flask 3.0+, Python |
| Database | SQLite (WAL mode), SQLAlchemy 2.0+ |
| Auth | Email/password, bcrypt, Flask sessions, CSRF tokens |
| Frontend | React 18 (CDN), Tailwind CSS (CDN), inline Babel — no build step |
| Push notifications | VAPID Web Push (pywebpush) |
| Scheduling | APScheduler (background jobs every 60s) |
| Hardware | Harvia MyHarvia GraphQL API via Cognito SRP auth |
| Email | Resend HTTP API |
| Deployment | Railway, Gunicorn (1 worker, 4 threads) |

## Project Structure

```
harvia_server.py      # Flask app — all API routes
harvia_client.py      # Harvia cloud API client (Cognito auth, GraphQL)
models.py             # SQLAlchemy models
static/
  index.html          # Entire React frontend (~2,400 lines, all inline)
  sw.js               # Service worker — offline caching + push handling
  manifest.json       # PWA manifest
generate_vapid_keys.py
.env.example
Procfile
requirements.txt
```

## Getting Started

### Prerequisites

- Python 3.10+
- A Harvia sauna with a MyHarvia account and device UUID (optional for UI-only development)

### Local setup

```bash
pip install -r requirements.txt
cp .env.example .env.local
# Edit .env.local — at minimum set APP_SECRET_KEY
python harvia_server.py
# App runs at http://localhost:5000
```

Without Harvia credentials the sauna control/status endpoints will error, but all booking, auth, and member flows work fine.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `APP_SECRET_KEY` | Yes | 32+ random characters; app refuses to start with the default |
| `APP_TIMEZONE` | No | IANA timezone (default: `Australia/Sydney`) |
| `HARVIA_USERNAME` | No | MyHarvia account email |
| `HARVIA_PASSWORD` | No | MyHarvia account password |
| `HARVIA_DEVICE_ID` | No | UUID of the Harvia device |
| `VAPID_PRIVATE_KEY` | No | Base64 VAPID private key — generate with `python generate_vapid_keys.py` |
| `VAPID_PUBLIC_KEY` | No | Base64 VAPID public key |
| `VAPID_CLAIMS_EMAIL` | No | Contact email included in VAPID JWT claims |
| `DB_PATH` | No | Path to SQLite file (default: `./sweatbox.db`) |
| `PORT` | No | HTTP port (default: `5000`) |
| `RESEND_API_KEY` | No | Resend API key — required for password reset emails |
| `EMAIL_FROM` | No | Sender address (domain must be verified in Resend) |
| `APP_URL` | No | Public URL — used in password reset links |

## Roles

| Role | Permissions |
|------|-------------|
| Admin | Full access — approve/reject members, manage presets, view control log and DB browser, set per-member temperature ceilings |
| User | Own bookings, sauna controls (subject to `max_temp` limit if set) |

The first account to sign up is automatically approved as admin. All subsequent signups require admin approval.

## API Routes

| Group | Routes |
|-------|--------|
| Auth | `POST /api/auth/signup` `/login` `/logout` `/migrate` `/forgot-password` `/reset-password`; `GET /api/auth/me` |
| Admin members | `GET/POST /api/admin/members`; `POST approve/reject`; `PUT/DELETE /api/admin/members/<id>`; `PUT /api/admin/members/<id>/set-credentials` |
| Members | `GET /api/members`; `PUT /api/members/<id>`; `POST /api/members/<id>/change-password` |
| Sauna | `GET /api/sauna/status`; `POST /on /off /extend /set /preset/<name>` |
| Presets | `GET /api/presets`; `PUT/DELETE /api/admin/presets/<name>` |
| Bookings | `GET/POST /api/bookings`; `PUT/DELETE /api/bookings/<id>`; `POST /preheat` |
| Push | `GET /api/push/vapid-key`; `POST subscribe /unsubscribe /test` |
| Admin utils | `GET /api/admin/harvia-stats /control_log /db/<table>`; `PUT/DELETE /api/admin/db/<table>/<id>` |
| Health | `GET /health` |

## Deployment

The app is deployed on [Railway](https://railway.app) with a persistent volume for the SQLite database.

```
# Procfile
gunicorn harvia_server:app --workers 1 --threads 4 --timeout 120
```

Key Railway settings:
- Set `DB_PATH=/data/sweatbox.db` (persistent volume mounted at `/data`)
- Set all required environment variables in Railway's variable editor
- First deploy: call `POST /api/auth/signup` to create the initial admin account

### Syncing the production database locally

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

Then set `DB_PATH=sweatbox_local.db` in `.env.local`.

## PWA & Push Notification Testing

Service workers and Web Push require HTTPS. For local testing, use [ngrok](https://ngrok.com):

```bash
# Start the app, note the port
python harvia_server.py

# In another terminal
ngrok http 5000
```

Open the ngrok `https://` URL in Chrome or Safari. The bell icon in the header will appear once VAPID keys are present in the environment.

To generate VAPID keys:

```bash
python generate_vapid_keys.py
```

## Background Jobs

APScheduler runs four jobs on a 60-second tick:

| Job | Description |
|-----|-------------|
| `check_and_auto_shutoff` | Advances booking states; turns the sauna off when a session ends |
| `check_preheat_reminders` | Sends push notification 35 min before session start |
| `check_session_ending` | Sends push notification 15 min before session end |
| `refresh_harvia_token` | Proactively refreshes the Cognito token every 30 min |
