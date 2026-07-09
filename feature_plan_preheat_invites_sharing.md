# Plan: Preheat Telemetry, Session Invites, and Shareable RSVP Links

## Context

Sweat Box currently supports single-member bookings and live sauna control, but:
1. We poll the Harvia API every 60s for Live Activities yet throw the temperature trend away — so we can't predict how long preheating takes.
2. Bookings have joiners (`BookingParticipant`) but no way to *invite* members and collect RSVPs.
3. There's no way to include someone outside the app — no public link, no guest RSVP.

**Decisions locked in with the user:**
- Outdoor temp from **Open-Meteo** (free, keyless), lat/long via env vars.
- Prediction scope: **collect data now + simple °C/min heating-rate estimate** in the UI (not a full regression model).
- RSVP options: **Yes / No / Maybe**; push notifications both ways (invite → invitee, response → host).
- Sharing: **native share sheet / copy link** only — no server-side SMS, no phone numbers stored.

**Step 0 of implementation:** commit this plan into the repo as `feature_plan_preheat_invites_sharing.md` (root-level, matching `todo.md` / `ios_todo.md` convention) so the work is tracked.

## Key codebase anchors
- `log_device_state()` (harvia_server.py:1253) already polls `harvia.get_full_status()` every 60s (`temperature`, `targetTemp`, `active`, `heatOn`, `remainingTime`) but only prints to stdout. No timeseries table exists.
- `BookingParticipant` (models.py:142) = members who **joined**; confers control rights (`_can_control_booking` harvia_server.py:649) and Live Activity eligibility (:716, :738). Invites must be a **separate table**; only a Yes RSVP creates a participant row.
- `join_booking` (harvia_server.py:3212) — extract its insert-if-missing logic into `_add_participant()` shared helper.
- Notifications: `_notify_member(member_id, payload, pref_key)` (:566) → `_dispatch_alert_to_member` (:477), APNs preferred, Web Push fallback. Unknown pref keys default to enabled — no migration needed for new keys.
- Serialization: `_booking_to_dict_for(booking, viewer)` (:622).
- Public-page pattern: `?reset_token=` handling in App() (static/index.html:3958) — copy for the guest RSVP page and `?booking=` deep link.
- CSRF: `_CSRF_EXEMPT` by endpoint name (harvia_server.py:990).
- Migrations: `_migrate_db()` (models.py:277) try/except ALTER TABLE list; **new tables need nothing** (`create_all`). Partial unique index pattern at models.py:298.
- SPA catch-all `serve_spa` (harvia_server.py:3420) already serves index.html for `/rsvp/<token>` — no new page route needed.

---

## F1 — Preheat telemetry + estimate

### F1a. Data collection (ship first so data accumulates)
- **New model `DeviceStateLog`** (models.py, after `ControlLog`): `ts` (DateTime, indexed, **`app_now()` naive local** — must match booking/segment math, not utcnow), `temperature`, `target_temp`, `active`, `heat_on`, `remaining_time`, `outdoor_temp` (Float, nullable).
- **Outdoor temp helper** `_get_outdoor_temp()` in harvia_server.py near the status cache (~:1230): module-level cache + lock, 15-min TTL; `GET https://api.open-meteo.com/v1/forecast?latitude=&longitude=&current=temperature_2m` with 5s timeout; any failure → return stale/None, never raise.
- **Env vars**: `WEATHER_LAT`, `WEATHER_LON` (skip fetch if unset), `DEVICE_LOG_RETENTION_DAYS` (default 365). Add to `.env.example` and CLAUDE.md.
- **Hook `log_device_state()`** (:1253): insert a row when `active == 1`, plus one trailing row on the 1→0 transition (cleanly terminates heating segments). Own `SessionLocal()` in try/finally, wrapped so a DB hiccup never breaks the tick.
- **Retention job** `cleanup_device_state_log()` registered with the other jobs (~:1329), interval 24h: delete rows older than retention.

### F1b. Estimate + UI
- **`_compute_heating_rate(db, outdoor_temp) -> dict | None`**: last 30 days of rows → split segments on >180s gaps or `active` drop → keep monotonic rise while `temperature < target - 3` (rate collapses near target) → keep segments ≥5 min and ΔT ≥5°C → rate = ΔT/min. Bucket by outdoor temp (<5, 5–15, >15°C); use caller's bucket if ≥3 segments, else all-data median; None if <3 total. Cache result 10 min.
- **`GET /api/sauna/heat-estimate`** (require_auth): params `target` (°C) or `target_f`; current temp from `_get_cached_status()`. Returns `{minutes_to_target, rate_c_per_min, current_temp, target_temp, outdoor_temp, samples}`; `minutes_to_target: null` when insufficient data. Apply ~1.1× safety factor.
- **Frontend**: SlotModal (index.html:1470) — when preheat is available, fetch estimate and show "Ready in ~25 min at 194°F" under the preheat button; hide when null. When a booking is `preheating`, show "~N min to target" on the today/status card (TodaySessionList :445).

---

## F2 — Member invites + RSVP

### Model
- **New `BookingInvite`** (models.py, after BookingParticipant): booking_id (FK CASCADE), member_id (FK CASCADE), invited_by (FK SET NULL), status `invited|yes|no|maybe`, invited_at, responded_at, unique(booking_id, member_id). Add `invites` relationship on `Booking` (cascade delete-orphan).

### Server (harvia_server.py)
- Extract **`_add_participant(db, booking, member_id)`** from `join_booking` (:3212); use in both join and RSVP paths. In `join_booking`, also mark any invite row `yes` to keep paths consistent.
- **`POST /api/bookings/<id>/invites`** — owner or admin. Body `{member_ids: [...]}`. Skip owner/existing participants/existing invites. Notify each invitee via `_notify_member(..., pref_key="invite")` with payload `{title: "🔥 Sauna invite", body: "<host> invited you — <day> <start>–<end>", url: "/?booking=<id>", tag: "invite-<id>-<mid>", bookingId}`. Factor a `_create_invites(db, booking, inviter, member_ids)` helper shared with create_booking.
- **`POST /api/bookings/<id>/rsvp`** — any approved member; body `{response: "yes"|"no"|"maybe"}`. Create invite row implicitly if missing (`invited_by=None`) — this also gives signed-in members opening an F3 share link a working flow. `yes` → `_add_participant`; `no`/`maybe` after prior `yes` → remove participant row **only while booking status is `scheduled`** (don't revoke control mid-session). Set responded_at via `app_now()`. Notify owner (`pref_key="rsvp"`). Reject cancelled/completed via `err()`.
- **`create_booking`** (:2928): accept optional `invite_member_ids`; after commit call `_create_invites`.
- **`_booking_to_dict_for`** (:622): add `invites: [{member_id, member_name, member_color, status, responded_at}]` and `my_invite_status`. Add `selectinload(Booking.invites)` in `list_bookings` (:2892) to avoid N+1.
- **Notification prefs**: new keys `invite` and `rsvp` (rsvp reused for F3 guest responses); add toggles to the notification settings UI (index.html ~:3940) — no migration needed.

### Frontend (static/index.html)
- **BookingModal** (:1299): "Invite others" chip row (members minus booking member) on the form screen; names echoed on the confirm screen; send `invite_member_ids` in the save payload.
- **SlotModal** (:1470): invite list with status glyphs (⏳/✓/✗/?) next to participant count (:1494); owner gets "+ Invite" member picker → POST /invites; invitee (`my_invite_status` set) gets Yes/Maybe/No button row → POST /rsvp (Yes replaces the Join button for invitees).
- **Deep link `/?booking=<id>`**: parse in App()'s initial effect (:3958) alongside reset_token; open SlotModal for that booking once bookings load. sw.js already opens `data.url` on Web Push tap; iOS APNs tap deep-linking lands in Phase 5.

---

## F3 — Public share link + guest RSVP

### Models / migration
- `Booking.share_token` (String, nullable) — `_migrate_db()` entries: `ALTER TABLE bookings ADD COLUMN share_token TEXT` + `CREATE UNIQUE INDEX IF NOT EXISTS ix_booking_share_token ON bookings (share_token) WHERE share_token IS NOT NULL`.
- **New `GuestRsvp`**: booking_id (FK CASCADE), name (cap ~40 chars), status `yes|no|maybe`, guest_secret (unique — lets a guest change their answer), created_at, updated_at.

### Server
- **`POST /api/bookings/<id>/share`** — auth; owner/admin/participant (`_can_control_booking`). Idempotently set `share_token = secrets.token_urlsafe(16)`; return `{url: f"{APP_URL}/rsvp/{token}", token}` (`APP_URL` env exists, :84).
- **`GET /api/rsvp/<token>`** — public. 404 on unknown token. Expose only: booking id, date, start/end, host first name + color, status, going_count, guest list (name+status), confirmed participant first names. Include `viewer` (via `current_member(db)` :931) with `my_invite_status` when a session cookie exists so the frontend branches to the member flow. Past/cancelled sessions return with that status (page says "this session has passed") rather than 404.
- **`POST /api/rsvp/<token>`** — public; body `{name, response, guest_secret?}`. Matching guest_secret → update; else create with fresh secret; return `{ok, guest_secret, status}`. Guards: name length cap, ~20 guests/booking cap, reject past/cancelled, light per-IP rate limit (reuse the `_check_rate_limit` pattern :118). If the request is authenticated, delegate to the F2 member RSVP flow instead (no ghost guests). Notify host (`pref_key="rsvp"`).
- **CSRF**: add the public POST endpoint name to `_CSRF_EXEMPT` (:990) — the unguessable path token is the auth. GET needs nothing.

### Frontend
- **Share button in SlotModal**: pre-fetch `POST /share` when the modal opens for an eligible viewer (keeps `navigator.share` inside the tap's gesture context), then `navigator.share({title, text, url})` with `navigator.clipboard.writeText(url)` + toast fallback.
- **GuestRsvpScreen**: in App() before the auth check, `location.pathname.startsWith('/rsvp/')` → new `authState = 'rsvp'` → component fetches `GET /api/rsvp/<token>`; if `viewer` present show member Yes/Maybe/No (POST /api/bookings/<id>/rsvp — grab CSRF via /api/auth/me first, same as app boot :3973) plus "Open in app" link to `/?booking=<id>`; else session card + name input + Yes/Maybe/No + "Create an account" (navigate to `/?signup=1`; teach App() to honor `?signup=1` → signup state). Persist `guest_secret` in localStorage keyed by token so returning guests can edit their response.

### iOS Universal Links (Phase 5 — needs a TestFlight/App Store cycle)
- **Server**: explicit route `GET /.well-known/apple-app-site-association` (Flask prefers the specific rule over the catch-all), `Content-Type: application/json`, body `{"applinks": {"apps": [], "details": [{"appID": "<APPLE_TEAM_ID>.dev.cbj87.SweatBox", "paths": ["/rsvp/*"]}]}}`. New env var `APPLE_TEAM_ID`. Restrict to `/rsvp/*` so normal web links don't hijack into the app.
- **iOS** (ios/SweatBox/SweatBox/): add Associated Domains entitlement `applinks:sweatbox.cbj87.dev`; handle `.onContinueUserActivity(NSUserActivityTypeBrowsingWeb)` + existing `.onOpenURL` in SweatBoxApp.swift → publish a `pendingURL` into WebViewContainer (load path exists at WebViewContainer.swift:85; `decidePolicyFor` :104 already allows same-host loads).
- **Notification-tap deep link** (benefits F2 too): AppDelegate (SweatBoxApp.swift:29) only implements `willPresent` — add `didReceive`, read `userInfo["url"]` (already attached by `_send_apns_alert` :512), route through the same `pendingURL`.
- Gotcha: Apple's CDN caches AASA up to ~24h at install; test with developer mode. Must be HTTPS, no redirect — confirm Cloudflare proxy serves it cleanly.

---

## Implementation order (each phase independently shippable)
1. **F1a telemetry collection** — DeviceStateLog, Open-Meteo helper, log hook, retention job, env vars. Zero UI; ship early so data accumulates while the rest is built.
2. **F1b estimate + UI** — `_compute_heating_rate`, heat-estimate endpoint, SlotModal + preheating card display.
3. **F2 member invites** — BookingInvite, `_add_participant` extraction, /invites + /rsvp endpoints, serialization, BookingModal/SlotModal UI, `?booking=` deep link, pref toggles.
4. **F3 web** — share_token migration, GuestRsvp, /share + public /api/rsvp GET/POST, CSRF exemption, GuestRsvpScreen, share button. Fully useful before the app update ships.
5. **F3 iOS Universal Links** — AASA route + APPLE_TEAM_ID, entitlement, URL/notification-tap plumbing in the Swift app.

Also update `.env.example` and CLAUDE.md (env vars, new routes, new models, new pref keys) as part of each phase.

## Risks / notes
- **Estimate is deliberately crude** (linear rate with tail cutoff + 1.1× factor). If it disappoints, upgrade to a T1→T2 minutes lookup — the raw table already supports it.
- **Timestamp convention**: `DeviceStateLog.ts` must use `app_now()` (local naive), not the `utcnow` default other models use, or segment math and preheat windows misalign.
- **Public POST abuse**: unguessable token + name-length cap + guest cap + per-IP rate limit; guest names go into push payloads as plain text (no HTML context, low injection risk).
- **AASA propagation** is delayed by Apple's CDN cache; Universal Links activate ~a day after deploy for fresh installs.
- **N+1**: use `selectinload` for participants/invites in `list_bookings` once serialized.

## Verification
- **F1a**: start the preview server with `WEATHER_LAT/LON` set in `.claude/launch.json` env; trigger sauna on (or mock `get_full_status`), confirm `device_state_log` rows appear each minute with outdoor_temp populated; confirm no rows when idle; kill Open-Meteo access and confirm the job still runs (outdoor_temp null).
- **F1b**: seed a few synthetic heating segments in the local DB, hit `/api/sauna/heat-estimate?target=90`, verify minutes/samples; check SlotModal display in the browser preview.
- **F2**: with the synced local DB (two members), create a booking with invites as member A → check invite push payload in logs, RSVP yes as member B → confirm BookingParticipant row created + host notified + `can_control` true for B; RSVP no → participant removed (only while scheduled). Test `?booking=<id>` deep link opens SlotModal.
- **F3 web**: share a booking, open `/rsvp/<token>` in an incognito window — RSVP as guest, confirm GuestRsvp row + host push; change answer via stored guest_secret; verify authenticated member on the same URL gets the member flow; confirm POST is CSRF-exempt but other endpoints still enforce CSRF. Use ngrok for the full share-sheet test on a phone (per CLAUDE.md local dev flow).
- **F3 iOS**: after deploy, `curl -i https://sweatbox.cbj87.dev/.well-known/apple-app-site-association` (200, JSON, no redirect); dev-mode Universal Link test from Messages on a device with the Xcode build; tap an invite push and confirm the app opens the booking.
