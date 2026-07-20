# Sweat Box — Code Review & Improvement Plan

Full review of `harvia_server.py`, `harvia_client.py`, `models.py`, `static/index.html`, and `static/sw.js` (2026-07-18).

Each item is ranked by **effectiveness** — how much real-world benefit fixing it delivers:
**🔴 High** (users hit this / safety-relevant), **🟡 Medium** (real but situational), **🟢 Low** (polish / hardening).

---

## 🐛 Bugs

### B1. 🔴 `/api/sauna/on` outlives the frontend's 10-second timeout
`HarviaClient.turn_on()` stages `onTime` and polls up to **45 s** for the device to commit it (`harvia_client.py:249-255`), so the HTTP response for `/api/sauna/on` routinely takes 20–45 s. The frontend `api()` wrapper aborts at **10 s** (`index.html:275`), so the user sees *"Request timed out — check your connection"* while the sauna actually turns on — and every follow-up step on the client (Live Activity start via `NativeBridge.startSaunaSession`, success toast, refresh) is skipped. Preheat (`/api/bookings/<id>/preheat`), `/api/sauna/extend`, and `/api/sauna/set` (off→sleep→on cycle) have the same problem, extend being worst (off + 1.5 s + full turn_on).
**Fix:** respond immediately after the state-change is accepted and finish the commit-wait + activate in a background thread (status polling already shows progress), or at minimum raise the client timeout for control endpoints. This is the single most user-visible defect in the app.
**Status: ✅ Fixed 2026-07-18** — `turn_on` split into `turn_on_stage()` (sync, fast) + `turn_on_activate()` (background via `_activate_sauna_async`); applies to on/preheat/extend/set. A generation counter cancels pending activations on any turn-off so a stale activation can't re-ignite the sauna.

### B2. 🔴 `max_temp` limit is not enforced at booking creation/edit
`create_booking` (`harvia_server.py:3271-3276`) and `edit_booking` (`:3426-3430`) clamp to 40–110 °C but never check `member.max_temp`. A limited member can book any temperature, then **Preheat** applies `booking.target_temp` directly (`:3511-3514`) with no limit check — fully bypassing the admin-set ceiling. CLAUDE.md explicitly claims this is enforced ("Member max_temp ceiling enforced at both booking creation and sauna on/set"), so this is doc-contradicting drift.
Related frontend bug: `BookingModal` derives the slider cap from `members[0]` (the first approved member), not the logged-in user (`index.html:1317`, `1337-1339`), so the wrong person's limit (or none) is displayed; the server ignores the submitted `member_id` anyway.

### B3. 🔴 Overlap/cooldown check breaks for bookings near or across midnight
`create_booking` and `edit_booking` compare **times only, on a single date** (`harvia_server.py:3292-3308`, `:3433-3445`):
- `cooldown_start`/`cooldown_end` are computed with `.time()`, so a 00:05 start minus 15 min wraps to 23:50 and the comparison inverts.
- A midnight-spanning booking (`end < start`) fails `start_time < cooldown_end AND end_time > cooldown_start` in ways that let genuinely overlapping bookings through, and the spillover portion after midnight is never checked against the next day's bookings at all.

`extend_booking_time` already does this correctly with real datetimes across ±1 day (`booking_bounds`, `:2492-2536`) — reuse that logic for create/edit.

### B4. 🔴 Midnight-spanning bookings never go "active" and never get the 15-min reminder
- `check_and_auto_shutoff`'s `newly_active` query requires `end_time > start_time`, and the midnight branch only looks at `date == yesterday` (`harvia_server.py:1142-1165`). So a 23:00–01:00 booking stays `scheduled` for its entire pre-midnight portion — the UI shows "Later", and `_current_live_booking` (Live Activities, `/native/live-session`) ignores it because it filters on `active/preheating`.
- `check_session_ending` computes `end_dt = combine(booking.date, end_time)` (`:1267`) which for a spanning booking is already in the past → the reminder never fires; after midnight the booking isn't in the `date == today` query either.

### B5. 🟡 "Invite" and "RSVP" notification toggles silently do nothing
`update_notification_prefs` only persists keys in `("session_ending", "sauna_control", "signup", "booking", "approval")` (`harvia_server.py:2802`), but the Notifications panel offers `invite` and `rsvp` toggles (`index.html:3592`, `:3600`) and the send paths honour those keys (`pref_key="invite"` / `"rsvp"`). Toggling them updates local state, the server drops the key, and the toggle reverts on next load — users cannot opt out of invite/RSVP pushes. Add the two keys to the allowlist. (Inverse papercut: `approval` is in the allowlist but the approval push never passes a `pref_key`.)

### B6. 🟡 Session-ending reminders are dead on APNs-only deployments
`check_session_ending` bails out when `VAPID_PRIVATE_KEY` is unset (`harvia_server.py:1248`), but delivery goes through `_notify_member` → APNs first. If a deployment configures APNs without Web Push, the 15-minute reminder job (a time-sensitive safety-ish alert) never runs. Drop the guard or check "either transport configured".

### B7. 🟡 "Book & start" double-creates bookings and inflates stats
`HomePanel.handlePrimary` (non-admin) and `Controls.handleBookNow` first POST `/api/bookings`, then POST `/api/sauna/on` — which calls `_auto_create_booking`, which **completes the booking just created and creates a second tracking booking** (`harvia_server.py:2291-2316`). The user ends up with two bookings for one session (one instantly "completed"), and `member_stats` counts both (`total_hours` doubles). Either have the client skip the explicit booking when it's about to turn the sauna on, or make `_auto_create_booking` adopt an existing just-started booking instead of completing it.

### B8. 🟡 `apply_preset` reintroduces the stale-`onTime` latching bug
`turn_on()` was rewritten to stage `onTime` while off, wait for commit, then activate — because activating in the same call latches a stale `onTime` ("session capped at 60 min", `harvia_client.py:231-241`). But `apply_preset` sends `{targetTemp, onTime, maxOnTime, active: 1}` in **one** `set_state` call (`harvia_server.py:2706-2712`). Unless `maxOnTime` genuinely changes device behaviour (the client says it's not writable on this unit), presets are exposed to the exact duration bug the two-phase flow exists to prevent. Route presets through `turn_on()`.

### B9. 🟡 `/api/sauna/extend` may extend the wrong booking
It grabs `.first()` of *any* booking today with status in `(active, preheating, scheduled)` with no ordering or time filter (`harvia_server.py:2431-2438`) — with a live session at 7 am and a scheduled one at 9 pm it can select either, and permission checks then run against the wrong booking. It also misses a midnight-spanning session started yesterday. Use `_current_live_booking()` here.

### B10. 🟡 Rate limiting keyed on a spoofable header
`_get_client_ip` takes the **first** entry of `X-Forwarded-For` (`harvia_server.py:127`), which the client controls. Behind Cloudflare/Railway an attacker can rotate fake XFF values to bypass the login lockout and guest-RSVP limits entirely. Use `CF-Connecting-IP` (Cloudflare) or the right-most untrusted-hop XFF entry. Related: `_login_attempts` / `_guest_rsvp_attempts` prune per-IP lists but never delete idle keys — unbounded (slow) memory growth.

### B11. 🟢 No rate limit on `/api/auth/forgot-password`
Anyone can trigger unlimited reset emails to a known address (annoyance + Resend quota burn). Reuse the login limiter.

### B12. 🟢 Home screen loses the active session after midnight
`todayBookingsFor` / `activeBookingForNow` / `remainingForStatus` only look at `b.date === todayIso` (`index.html:410-443`), so a 23:00–01:00 session disappears from Home/Controls at 00:00 while the sauna is still on.

### B13. 🟢 `edit_booking` accepts dates/times in the past
Creating guards against past times client-side only; the server lets you edit a scheduled booking into the past, where the scheduler immediately completes it.

### B14. 🟢 Session cookie flags not set
`SESSION_COOKIE_SECURE` and `SESSION_COOKIE_SAMESITE` are left at defaults. With 30-day permanent sessions, set `Secure` + `SameSite=Lax` explicitly in production.

### B15. 🟢 Toast IDs can collide
`useToast` uses `Date.now()` as the id (`index.html:302`); two toasts in the same millisecond dismiss together. Use a counter.

---

## 🎨 UI/UX

### U1. 🔴 Sauna-start feedback (companion to B1)
Even with B1 fixed server-side, "Turn on" should feel instant: optimistic "Starting…" state, then poll cached status to confirm. Today the happy path is a spinner for many seconds followed (often) by a spurious error toast while the heater is actually running — the worst possible message for a hardware-control app.
**Status: ✅ Fixed 2026-07-18** — hero shows a pulsing "Sauna is starting…", the primary button becomes "Turn off" (cancels the pending activation), and the app polls live status (~10/25/40/55/75s) until `active=1`, with an info toast if it never confirms.

### U2. 🟡 °C/°F preference isn't honoured everywhere
The `TempUnitContext` exists, but `BookingModal` (slider, labels, confirm screen), `SlotModal` ("Temp: 194°F (90°C)"), and the admin max-temp / preset editors hardcode °F. A °C-preferring member books in Fahrenheit. Thread `fmtC`/`fmtF` through these components.

### U3. 🟡 Public share link is created just by opening a booking
`SlotModal` POSTs `/api/bookings/<id>/share` on mount for anyone who can control the booking (`index.html:1552-1558`), minting a permanent unguessable-but-public URL even if the user never taps Share. Generate the token on first Share tap instead (keep the pre-fetch trick by fetching on tap-down or showing a two-step share).

### U4. 🟡 `window.confirm`/`alert` mixed with the app's own UI
Member delete, preset delete, admin toggles, and DB-browser errors use native `confirm()`/`alert()` (`index.html:1754`, `1781`, `3184`, `2874`…), which look jarring in the iOS PWA/native wrapper and can't be styled. The app already has a nice inline confirm pattern (cancel-booking flow in `SlotModal`) and toasts — standardise on those.

### U5. 🟡 Nested interactive elements on session cards
The Preheat/Join affordances are `<span onClick>` **inside** a `<button>` (`index.html:489-501`, `1078-1085`) — invalid HTML, not keyboard-focusable, and screen readers announce one merged button. Restructure as sibling buttons in a grid row.

### U6. 🟢 Service-worker update lag
`staleWhileRevalidate` serves the old `index.html` until the *second* visit after a deploy (a pain point you've already hit in local testing). Listen for the new SW's `updatefound`/`controllerchange` and show a "New version — tap to refresh" toast.

### U7. 🟢 History stats mislead for young accounts
"Month / week" divides by a fixed 30/7 even if the account is 4 days old (`index.html:992`); "Avg / week" uses first-session date, so the two disagree. Use `min(30, daysSinceFirst)` as the denominator.

### U8. 🟢 Retire the PIN migration flow when done
Login screen still offers "Migrating from PIN?"; once every member has credentials the screen, `PinPad`, `/api/auth/migrate`, and `reset-pin` admin route are dead weight and extra attack surface.

### U9. 🟢 Error message quality in `api()`
All failures collapse to `e.message`; distinguish offline ("You're offline") from timeout from server error, since the offline booking queue already exists and the copy matters on flaky sauna-adjacent Wi-Fi.

---

## ⚡ Performance

### P1. 🔴 Push/APNs fan-out runs synchronously inside request handlers
`_notify_admins`, `_notify_member`, and `_fanout_live_activity_start` do serial HTTPS calls to Apple/push services **before the HTTP response returns** — e.g. `create_booking` notifies every admin device inline (`harvia_server.py:3336`), `sauna_on` does Live Activity fan-out + admin alerts (`:2367-2373`), RSVP notifies the host. Each push is a network round trip; a few registered devices add seconds to interactive requests, on a 4-thread server. Move notification dispatch to a background thread/queue (`_send_email` already does exactly this — copy the pattern).

### P2. 🟡 N+1 queries in `/api/bookings/history`
`booking_history` loads up to 500 bookings **without** `selectinload(participants, invites)` (`harvia_server.py:3843-3859`), then `_booking_to_dict_for` lazy-loads both collections per row → up to ~1,000 extra queries per request. `list_bookings` already uses `selectinload` — add the same options here. This endpoint is called on every app load, tab focus, and pull-to-refresh.

### P3. 🟡 Cold-start request storm and duplicate fetches
On mount the app fires `loadStatus()` (live Harvia round trip — the slowest possible call) plus `loadBookings()` **twice** (once from the `/api/config` effect at `index.html:3741-3748`, once from the main mount effect at `:3772-3783`), along with members/presets/history/pending. Use `?cached=1` for the first status paint (a ≤60 s-old value is fine for the hero number) and drop the duplicate bookings load.

### P4. 🟡 In-browser Babel + Tailwind-CDN JIT on every cold load
4,400 lines of JSX are transpiled by `@babel/standalone` on the phone, and Tailwind's CDN script recompiles utilities at runtime — likely 1–3 s of main-thread work per cold start on mobile. A minimal prebuild (esbuild + tailwind CLI emitting two static files, still no framework churn) would cut startup dramatically. This trades away the stated "no build step" simplicity, so it's a deliberate call — but it's the biggest single perf lever in the frontend.

### P5. 🟢 Missing SQLite indexes
Nearly every hot query filters `bookings.date` + `status` (scheduler runs 4 of them every 60 s), and the control log paginates on `created_at DESC`. Add `ix_bookings_date_status` and `ix_control_log_created_at` in `_migrate_db()`. Cheap, and keeps the 60 s jobs O(log n) as history grows.

### P6. 🟢 Blocking sleeps tie up worker threads
`turn_on`'s 45 s poll, `_safe_turn_off`'s retries, and extend/set's off→sleep→on each occupy one of the 4 gunicorn threads. Two concurrent control actions plus the scheduler can starve unrelated requests. Largely resolved by fixing B1 (async control flow); until then consider bumping threads.

### P7. 🟢 `_compute_heating_rate` scans 30 days of telemetry while holding the lock
The 10-min cache rebuild loads every `DeviceStateLog` row for 30 days and blocks concurrent heat-estimate requests on `_heat_rate_lock` (`harvia_server.py:2118-2172`). Fine today; if telemetry density grows, compute outside the lock and swap the cache atomically, or pre-filter columns with `with_entities`.

---

## 🔧 Other improvements

### O1. 🔴 No automated tests
Zero test files in the repo, while the trickiest logic — cooldown/overlap math, midnight-spanning transitions, scheduler state machine, max-temp enforcement — is exactly what regresses silently (B2–B4 prove it). A small pytest suite using Flask's test client + an in-memory SQLite, focused on booking rules and the scheduler tick, would pay for itself immediately. Highest-leverage non-user-facing change on this list.

### O2. 🟡 Unauthenticated endpoints leak household presence
`/api/sauna/status` (live "is someone in the sauna / is anyone home" signal) and `/api/members` (family names + colors) are public by design for the login screen. Anyone who learns the domain can poll them. Since login is email/password now (the PIN-picker rationale is gone), consider requiring auth for both, or at least stripping `/api/members` to what migration truly needs and rate-limiting status.

### O3. 🟡 Split `harvia_server.py` (4,086 lines) into blueprints
Auth, admin, sauna control, bookings/RSVP, push/native, and scheduler jobs are all one file. Flask blueprints (`auth.py`, `bookings.py`, `sauna.py`, `push.py`, `admin.py`, `jobs.py`) would make review and testing tractable without changing behaviour. Same story for `index.html` (4,421 lines) if P4 ever lands.

### O4. 🟡 SQLite backup story
The Railway volume is the only copy of the database. A daily scheduler job doing `sqlite3 .backup` to a timestamped file (+ optional upload to object storage) is ~20 lines and turns "volume corruption" from disaster into inconvenience.

### O5. 🟢 Scheduler duplication hazard if gunicorn workers > 1
`_startup()` runs at import in every worker; the Procfile's `--workers 1` is the only thing preventing duplicate auto-shutoff/notification jobs. Add a guard (env flag or file lock) or a loud comment in the Procfile so a future scale-up doesn't double-fire pushes.

### O6. 🟢 Mixed timestamp bases in the DB
`created_at` columns use `datetime.utcnow` while booking/reset logic uses naive local `app_now()`. It happens to be consistent per-column today (the frontend appends `'Z'` for control-log times), but it's a trap — standardise new columns on one base and document it in CLAUDE.md.

### O7. 🟢 Fix CLAUDE.md drift
Update the booking-rules section once B2 is fixed (or corrected to match reality), and document that `invite`/`rsvp` prefs exist (B5). Stale project docs actively mislead future agent/code sessions.

### O8. 🟢 Minor code hygiene
- `_send_push` re-imports `pywebpush` per call; `signup`/`migrate`/`admin_set_credentials` re-import `re` per request — hoist to module scope, and extract the duplicated email-regex/uniqueness check into one helper.
- `_live_activity_push_to_start_recipients` and `_live_activity_recipient_tokens` duplicate the eligible-member computation — share it.
- The stale-token detection for Live Activity pushes does substring matching on the body (`"BadDeviceToken" in body`) while alert pushes parse JSON properly (`_apns_token_is_stale`) — unify on the parser.
- `log_device_state` reads `_last_device_active` without `_device_state_lock` (writes take it) — harmless today with one scheduler thread, but inconsistent.

---

## Suggested attack order

1. **B1 + U1** — async sauna-control flow (biggest daily-use win)
2. **B2, B3, B4** — booking correctness + safety-limit enforcement (share the `booking_bounds` datetime logic)
3. **B5, B6** — notification prefs & reminder transport (small diffs, immediate effect)
4. **P1, P2, P3** — snappier requests and app loads
5. **O1** — lock in the above with tests before further refactors
6. Everything else opportunistically.
