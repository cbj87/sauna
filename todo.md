# Sweat Box — Todo

---

## 🐛 Bug Fixes

- [x] **Add member endpoint is wrong** — `MembersPanel` calls `POST /api/members` but that route doesn't exist; it should call `POST /api/admin/members`
- [x] **Controls default duration doesn't reflect saved prefs on first login** — `useState` only seeds once; if members haven't loaded yet it falls back to the stale auth-session value from login time
- [x] **Temperature conversion round-trip loses precision** — `f_to_c()` returns `int`, `c_to_f()` returns `float`; values can drift ±1–2° through repeated conversions
- [x] **Login crashes with 500 if member_id is not an integer** — `int(member_id)` in the login route has no `ValueError` catch; should return a clean 400 instead
- [x] **Preheat window allows triggering 5 min after session start** — `minutes_until < -5` guard is likely unintentional; should be `< 0`
- [x] **Status display missing target temp** — Controls live-readings panel shows current temp and remaining time but not what temp the sauna is set to heat *to*
- [x] **Status pill goes stale on non-Controls tabs** — status is no longer polled globally so the header pill stays frozen unless you visit the Controls tab

---

## ✨ New Features

- [ ] **PIN reset** — admin can reset any member's PIN; member gets a one-time reset flow
- [ ] **Push / browser notifications** — notify members when their booking is approved, when preheat starts, or when the sauna is ready
- [ ] **Sauna presets** — expose the built-in presets (Quick / Standard / Long / Hot / Steam) as quick-select buttons on the Controls tab; they already exist in `harvia_server.py` but are never called from the UI
- [ ] **Recurring bookings** — book the same slot weekly (e.g. "every Tuesday at 7 PM")
- [ ] **Booking history** — view past sessions; basic usage stats per member (total sessions, total hours, favourite temp)
- [ ] **Group bookings** — allow a booking to include multiple members in the same slot
- [ ] **Invite link** — admin can share a one-time signup link instead of requiring manual approval every time

---

## 🔧 Improved Features

- [ ] **Booking modification** — allow editing start time, duration, and temp after creation, not just cancelling
- [ ] **Overlap feedback** — when a booking fails due to a conflict, highlight the clashing slot on the timeline rather than just showing a toast
- [ ] **Smarter Controls lock** — currently unlocks 90 min before any booking; consider exposing this window as a configurable admin setting
- [ ] **Member switcher on booking modal** — when an admin creates a booking, they should be able to assign it to any member, not just themselves
- [ ] **Auto-shutoff edge case** — if a booking is cancelled *after* the auto-shutoff scheduler has already run for that slot, the sauna may stay on; re-check shutdown logic
- [ ] **Admin pending-count badge** — currently re-polls every 2 min even when not on the Admin tab; only poll when the badge is visible or tab is active
- [ ] **Booking confirmation step** — show a summary (date, time, temp, duration) before saving, to prevent accidental mis-bookings

---

## 🎨 UI Improvements

- [ ] **Mobile timeline height** — fixed 480px height is cramped on small phones; make it responsive or scrollable
- [ ] **Timeline hours are hard-coded to 6 AM–11 PM** — make the window configurable, or at minimum dynamic (collapse to first/last booking ± 1 hour)
- [ ] **Show existing bookings inside BookingModal** — a mini timeline or list of today's bookings so users can see what's already taken before picking a time
- [ ] **Empty state for schedule** — "No bookings today — tap to add one" message instead of just a blank timeline
- [ ] **Loading skeletons** — replace blank/pulse states with skeleton cards so the layout doesn't jump when data loads
- [ ] **Colour conflict warning** — warn or prevent two members from using the same timeline colour so the schedule stays readable
- [ ] **Dark/light mode** — currently hard-coded dark; could follow system preference
- [ ] **Swipe between days** — swipe left/right on the timeline to move between the 7-day pill dates on mobile
- [ ] **Controls tab target temp in live readings** — add a "target" tile next to "current" so you can see where the sauna is heading
- [ ] **Animated status transitions** — smooth the status pill change (e.g. Off → Heating) with a brief colour crossfade

---

## 🔒 Security

- [x] **Rate-limit login attempts** — 4-digit PINs have only 10,000 combinations; a simple per-IP attempt counter would stop brute force
- [ ] **CSRF protection** — Flask sessions are in use but no CSRF token is checked on mutating POST/DELETE routes
- [ ] **Re-authenticate for destructive actions** — require PIN confirmation before deleting a member or removing admin privileges
- [x] **Harden default secret key** — warn loudly (or refuse to start) if `APP_SECRET_KEY` is still the default `dev-secret-change-me`

---

## 🚀 Other / Infrastructure

- [x] **Railway volume for DB persistence** — add a `/data` volume in Railway and set `DB_PATH=/data/sweatbox.db` in env vars so members and bookings survive redeploys (see `.env.example`)
- [ ] **Favicon** — add a small sauna emoji favicon so the browser tab looks good
- [ ] **PWA manifest** — add a `manifest.json` and service worker so the app can be "Add to Home Screen" on iOS/Android
- [ ] **Error boundary** — wrap the React app in an error boundary so a JS crash shows a friendly message instead of a blank screen
- [ ] **Logging / Sentry** — add structured error logging or a Sentry integration so production crashes are visible
- [ ] **Health check endpoint** — `GET /health` returning `{ok: true}` for Railway uptime monitoring
