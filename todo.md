# Sweat Box — Todo

---

## 🐛 Bug Fixes

- [ ] **No confirmation before cancelling a booking** — tapping "Cancel" immediately fires the delete; a mis-tap kills a session with no undo; show a confirm dialog first
- [ ] **No confirmation before turning the sauna OFF** — if someone else is mid-session and a second user taps Off, it cuts them off silently; require a confirm step ("Are you sure? [Name]'s session ends at HH:MM")
- [ ] **Controls tab doesn't show whose booking is active** — the live status panel shows temp/time but not which member's session is running; add the member name + booking end time so users know before they act
- [ ] **Extend button has no upper limit** — +15 min can be tapped indefinitely, extending a session for hours past the original booking; cap at e.g. 2 hours beyond the original end time or require admin for additional extends
- [ ] **Admin push notification shows °F label but uses °C value** — "Sauna is on — [Name] started session [Temp]°F" sends the raw Celsius value from the DB with an °F unit label; convert properly or use °C consistently in notifications
- [ ] **Pending members see no content** — approved users can view the schedule read-only even before booking; pending members see only a waiting screen; let them at least view the schedule so they know what they're joining
- [ ] **Booking modal allows booking in the past** — time picker has no floor at current time; backend rejects it but the error message ("end_time must be after start_time" or overlap error) gives no hint; show "Start time is in the past" inline
- [x] **Add member endpoint is wrong** — `MembersPanel` calls `POST /api/members` but that route doesn't exist; it should call `POST /api/admin/members`
- [x] **Controls default duration doesn't reflect saved prefs on first login** — `useState` only seeds once; if members haven't loaded yet it falls back to the stale auth-session value from login time
- [x] **Temperature conversion round-trip loses precision** — `f_to_c()` returns `int`, `c_to_f()` returns `float`; values can drift ±1–2° through repeated conversions
- [x] **Login crashes with 500 if member_id is not an integer** — `int(member_id)` in the login route has no `ValueError` catch; should return a clean 400 instead
- [x] **Preheat window allows triggering 5 min after session start** — `minutes_until < -5` guard is likely unintentional; should be `< 0`
- [x] **Status display missing target temp** — Controls live-readings panel shows current temp and remaining time but not what temp the sauna is set to heat *to*
- [x] **Status pill goes stale on non-Controls tabs** — status is no longer polled globally so the header pill stays frozen unless you visit the Controls tab
- [x] **Midnight-spanning bookings silently fail** — if end time is after midnight (e.g. 23:00–01:00 next day), the server rejects it with "end_time must be after start_time" and the user gets no helpful message; the UI should either block it or support next-day end times
- [ ] **Booking end time allows impossible values** — the time picker doesn't validate `hour < 24`, so entering "25:00" wraps to 01:00 on the same date, creating a slot that ends before it starts and breaks the timeline renderer
- [ ] **Past times are selectable in booking modal** — nothing prevents booking 06:00 AM on a day where it's already 7 PM; the backend rejects it but the error message gives no hint about the real cause
- [ ] **Bell toggle has no loading/pending state** — during `subscribePush()` the button stays interactive; rapid taps can fire duplicate subscription requests; disable the button and show a subtle spinner while the request is in flight
- [ ] **Auto-shutoff misses midnight boundary** — the scheduler checks `booking.end_time <= current_time` with the same date; if a booking ends at 23:59 the scheduler sees it as already-past on the next day's run but the sauna was never turned off overnight
- [x] **Concurrent booking race condition** — two family members submitting bookings for the same slot simultaneously can both pass the overlap check before either commits; add a DB-level unique constraint or serialised check

---

## ✨ New Features

- [ ] **"Book now" quick action** — single tap on Controls tab that books an immediate session using member defaults; skips the full modal for the common case of "I'm about to use the sauna right now"
- [ ] **Estimated ready time** — during preheat, show "Ready in ~X min" based on current temp delta and typical heat rate; helps users plan when to head to the sauna
- [ ] **Sauna temperature history chart** — during an active/preheating session, show a simple sparkline of temp over time so users can see how quickly it's heating
- [ ] **Notification when a booking is cancelled by someone else** — admin (and perhaps the booking owner) should be notified if a session is cancelled; currently only creation/update triggers a notification
- [ ] **Admin "maintenance mode"** — lock the sauna from being turned on; useful for cleaning days, maintenance, or travel; shows a banner to all users explaining it's unavailable
- [ ] **Change own PIN** — users have no way to change their PIN after signup; add a "Change PIN" option in their member settings
- [ ] **PIN reset** — admin can reset any member's PIN; member gets a one-time reset flow
- [x] **Push notifications — preheat reminders** — VAPID-based Web Push; notifies booking owner `on_time + 5 min` before session start; 🔔 bell toggle in header; `push_subscriptions` table; `check_preheat_reminders` scheduler job every 60s
- [ ] **Push notifications — booking approved** — notify a member when their signup is approved by admin
- [ ] **Push notifications — sauna ready** — notify when the sauna has reached target temp (requires polling status during preheat)
- [ ] **Recurring bookings** — book the same slot weekly (e.g. "every Tuesday at 7 PM")
- [ ] **Booking history** — view past sessions; basic usage stats per member (total sessions, total hours, favourite temp)
- [ ] **Group bookings** — allow a booking to include multiple members in the same slot
- [ ] **Invite link** — admin can share a one-time signup link instead of requiring manual approval every time
- [ ] **Booking beyond 7 days** — the date picker is capped at 7 days out; allow booking further ahead (configurable window)

---

## 🔧 Improved Features

- [ ] **Controls tab: show active booking context** — when a session is running, display the booker's name, original end time, and a visual countdown so users know the context before extending or turning off
- [ ] **Notification tap opens to correct booking** — when user taps a preheat/session-ending notification, navigate directly to that booking's slot in the schedule (not just the home tab); requires passing bookingId through the push payload click action
- [ ] **Push re-subscribe prompt on VAPID key rotation** — if server VAPID keys are regenerated, existing subscriptions silently break; detect the 410/404 from the push service, delete the stale DB record, and show an in-app banner prompting the user to re-enable notifications
- [ ] **Control log pagination** — the admin control log loads all records with no limit; add pagination or a "load more" button before the log grows unwieldy
- [ ] **Booking modification** — allow editing start time, duration, and temp after creation, not just cancelling
- [ ] **Overlap feedback** — when a booking fails due to a conflict, highlight the clashing slot on the timeline rather than just showing a toast
- [ ] **Smarter Controls lock** — currently unlocks 90 min before any booking; consider exposing this window as a configurable admin setting
- [ ] **Member switcher on booking modal** — when an admin creates a booking, they should be able to assign it to any member, not just themselves
- [ ] **Auto-shutoff edge case** — if a booking is cancelled *after* the auto-shutoff scheduler has already run for that slot, the sauna may stay on; re-check shutdown logic
- [ ] **Admin pending-count badge** — currently re-polls every 2 min even when not on the Admin tab; only poll when the badge is visible or tab is active
- [ ] **Booking confirmation step** — show a summary (date, time, temp, duration) before saving, to prevent accidental mis-bookings
- [ ] **Notification tap refreshes app state** — when a user taps a preheat notification and the app opens, it should immediately re-fetch booking and sauna status rather than showing stale data
- [x] **API request timeouts** — `api()` has no timeout; on slow mobile connections it hangs indefinitely with no feedback; add a 10s timeout and a "taking longer than expected…" indicator
- [ ] **Friendlier Harvia error messages** — when the Harvia device is offline, the toast shows a raw exception string; map common failure modes to human-readable messages ("Sauna device is unreachable — check its WiFi")
- [x] **Pull-to-refresh on Today tab** — standard mobile gesture; currently only a tiny ↻ button is available

---

## 🎨 UI Improvements

- [ ] **Visual preheat countdown on booking card** — once "Preheat" is tapped, show a live "Heating… X°C / Y°C target" progress on the booking card instead of just a static "Preheating" pill
- [ ] **Show current time marker on schedule** — a red "now" line across the timeline (like Google Calendar) so users can instantly see what's happening vs upcoming
- [ ] **Booking card shows temp** — the booking slot on the timeline only shows member name + time; showing the target temp (e.g. "90°C") gives useful at-a-glance info, especially for the Controls tab context
- [ ] **Member colour picker — more options or hex input** — the colour picker is limited; offer a larger palette or a hex input so members can pick a truly unique colour
- [ ] **Better empty state for Controls when sauna is off** — currently just shows "Off" status; add a CTA "Book a session" or "Turn on now" with default settings for zero-friction access
- [ ] **Onboarding tip for new members after approval** — after a member is approved, show a brief one-time walkthrough (3 steps max: how to book, how to preheat, how to enable notifications)
- [x] **Mobile timeline height** — inner canvas is 44px/hr (scales with window size); outer container is `min(520px, 68vh)` with `overflow-y: auto` so it scrolls on small phones without cutting off content
- [x] **Timeline hours are dynamic** — window computed from actual bookings (min start − 1hr to max end + 1hr, clamped to 5am–midnight); falls back to 7am–10pm when no bookings; hour grid and click-to-book math all update accordingly
- [x] **Existing bookings shown in BookingModal** — "Already booked" section lists all bookings for the selected date with member colour dot, name, and time range; only shown when there are bookings to display
- [x] **Empty state for schedule** — Timeline shows 🛖 "No bookings yet / Tap anywhere to add one" only after loading completes; not shown during initial fetch (avoids flash of empty content)
- [x] **Loading skeletons** — `bookingsLoading` state added to MainApp; `loadBookings` sets it true/false around the fetch; Timeline receives `loading` prop and shows "Loading schedule…" pulse instead of empty state; booking list below shows two skeleton rows while loading
- [ ] **Colour conflict warning** — warn or prevent two members from using the same timeline colour so the schedule stays readable
- [ ] **Dark/light mode** — currently hard-coded dark; could follow system preference
- [ ] **Swipe between days** — swipe left/right on the timeline to move between the 7-day pill dates on mobile
- [ ] **Animated status transitions** — smooth the status pill change (e.g. Off → Heating) with a brief colour crossfade
- [x] **iOS safe areas** — add `viewport-fit=cover` to the viewport meta tag and `env(safe-area-inset-*)` padding to the sticky header, tab nav, and FAB button so they don't overlap the Dynamic Island / home indicator on iPhone 14+
- [ ] **Touch target sizes** — duration buttons and the temperature slider are too small for reliable thumb taps on 375px screens; minimum 44px touch targets per Apple HIG
- [ ] **Toast overflow on small screens** — long error messages (e.g. temperature limit exceeded) get cut off; add `word-wrap` and `max-width` so the full message is always readable
- [ ] **Toasts not manually dismissible** — error toasts auto-disappear after 3.5 s even when the user is still reading; tap-to-dismiss would prevent messages vanishing before the user can act on them
- [ ] **Install banner close button touch target too small** — the `✕` dismiss button uses minimal padding, well below the 44 px Apple HIG minimum; easy to mis-tap on small phones
- [ ] **Splash screen for iOS PWA** — add `apple-touch-startup-image` meta tags so installed iOS PWA shows the app background instead of a white flash on launch
- [x] **Offline state indicator** — when the device loses network, show a banner ("You're offline — data may be stale") rather than silently failing API calls

---

## 📱 PWA / Offline

- [x] **PWA manifest** — `manifest.json` with name, icons, theme colour, `display: standalone`
- [x] **Service worker** — registered in `<head>`; handles push events and notification clicks
- [x] **Push notifications** — VAPID Web Push for preheat reminders; bell icon in header
- [x] **Offline caching** — cache-first for static assets (shell, icons, CDN libs) via SW install precache; network-first with cache fallback for all GET API calls; stale-while-revalidate for app shell; non-GET mutations pass through unmodified; old caches pruned on SW activate
- [x] **PNG icons for PWA** — 192×192 and 512×512 orange PNG icons generated and added to manifest; `apple-touch-icon` updated to PNG; SVG kept as fallback entry
- [x] **Session expiry UX** — 401 interceptor in `api()` fires a module-level callback; `App` wires it up to set `sessionExpired` state and redirect to login; `LoginScreen` shows an amber "Your session expired — please log in again" banner when the flag is set
- [x] **Background sync for failed bookings** — offline booking POSTs are saved to `localStorage`; `window.online` event and SW `FLUSH_BOOKING_QUEUE` message both trigger a flush that retries and toasts on success; SW Background Sync API registered where supported (Chrome/Android); amber offline banner shown in header when network is lost

---

## 🔒 Security

- [x] **Rate-limit login attempts** — 4-digit PINs have only 10,000 combinations; a simple per-IP attempt counter would stop brute force
- [x] **Harden default secret key** — warn loudly (or refuse to start) if `APP_SECRET_KEY` is still the default `dev-secret-change-me`
- [x] **CSRF protection** — Flask sessions are in use but no CSRF token is checked on mutating POST/DELETE routes
- [ ] **Re-authenticate for destructive actions** — require PIN confirmation before deleting a member or removing admin privileges

---

## ⚡ Performance

- [ ] **Vendor CDN assets into /static** — React, Tailwind, and Babel are loaded from unpkg.com CDN on every page load; if CDN is down, the app is completely broken; vendor the files for reliability and faster load (no external round-trips)
- [ ] **Sauna status polling interval is fixed** — Controls tab polls status on a fixed interval regardless of whether a session is active; poll more aggressively during preheat/active sessions, less aggressively when off
- [ ] **No HTTP caching headers on API responses** — GET /api/bookings, /api/sauna/status, etc. return no `Cache-Control` headers; proper short-lived caching (e.g. 10s) would reduce server load with multiple family members checking status simultaneously
- [ ] **Gunicorn single worker limits concurrency** — with 4 threads and SQLite WAL, concurrent requests work but a slow Harvia API call (up to 5s) can tie up a thread; consider async handling or a timeout that returns cached state rather than blocking

---

## 🚀 Other / Infrastructure

- [x] **Railway volume for DB persistence** — add a `/data` volume in Railway and set `DB_PATH=/data/sweatbox.db` in env vars so members and bookings survive redeploys (see `.env.example`)
- [x] **Favicon** — sauna emoji favicon in browser tab
- [x] **Health check endpoint** — `GET /health` returning `{ok: true}` for Railway uptime monitoring
- [x] **Timezone handling** — all `datetime.now()` calls use server-local time with no timezone info; if the Railway server is UTC and the family is in a different zone, booking times and scheduler jobs fire at wrong local times; fixed via `APP_TIMEZONE` env var + `app_now()` helper using `zoneinfo.ZoneInfo`
- [ ] **Error boundary** — wrap the React app in an error boundary so a JS crash shows a friendly message instead of a blank screen
- [ ] **Logging / Sentry** — add structured error logging or a Sentry integration so production crashes are visible
- [ ] **CDN fallback** — React and Tailwind are loaded from `unpkg.com`; if that CDN is unavailable the app is completely broken; vendor the files into `/static` or add a local fallback
- [ ] **Push subscription silently breaks on VAPID key rotation** — if the server's VAPID keys are ever regenerated, existing push subscriptions become invalid with no re-subscribe prompt; detect the 410/404 from the push service and guide the user to re-enable notifications
