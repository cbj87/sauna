# Sweat Box — Todo

---

## 🐛 Bug Fixes

- [x] **Booking modal time picker has no validation** — entering a past time gives a confusing backend error ("end_time must be after start_time") with no inline hint; entering an hour > 23 silently wraps to the next day; show "Start time is in the past" / "Invalid time" inline before the user submits
- [x] **Extend button has no upper limit** — +15 min can be tapped indefinitely; capped so remaining time never exceeds 30 min (e.g. 25 min left → button adds 5 min); button hidden when already at cap
- [x] **Auto-shutoff misses midnight boundary** — already handled by `past_yesterday_stranded` cleanup query in scheduler; stranded normal bookings from yesterday are completed and sauna turned off on next run
- [x] **Admin push notification shows °F label but uses °C value** — already correct; all notification paths use `c_to_f()` and show both units e.g. "194°F (90°C)"
- [x] **Pending members see no content** — by design; unapproved members see only the waiting screen
- [x] **Bell toggle has no loading/pending state** — `notifLoading` state added; `subscribePush` guarded against re-entry; toggle shows spinner and is disabled while request is in flight

---

## ✨ New Features

- [ ] **Change own PIN** — users have no way to change their PIN after signup; add a "Change PIN" option in member settings
- [ ] **PIN reset** — admin can reset any member's PIN; member gets a one-time reset flow
- [x] **"Book now" quick action** — single tap on the Controls tab that books an immediate session using member defaults; skips the full modal for the common case of "I'm about to use the sauna right now"
- [ ] **Booking modification** — allow editing start time, duration, and temp after creation, not just cancelling
- [ ] **Booking beyond 7 days** — the date picker is capped at 7 days out; allow booking further ahead (configurable window)
- [ ] **Booking history** — view past sessions; basic usage stats per member (total sessions, total hours, favourite temp)
- [ ] **Push notifications — booking approved** — notify a member when their signup is approved by admin
- [ ] **Push notifications — sauna ready** — notify the booking owner when the sauna reaches target temp (requires polling status during preheat)
- [ ] **Notification when a booking is cancelled** — notify the booking owner (and optionally admin) if a session is cancelled; currently only creation triggers a notification
- [ ] **Admin "maintenance mode"** — lock the sauna from being turned on; useful for cleaning days or travel; shows a banner to all users explaining it's unavailable
- [ ] **Invite link** — admin can share a one-time signup link instead of requiring manual approval every time
- [ ] **Recurring bookings** — book the same slot weekly (e.g. "every Tuesday at 7 PM")
- [ ] **Group bookings** — allow a booking to include multiple members in the same slot
- [ ] **Estimated ready time** — during preheat, show "Ready in ~X min" based on current temp delta and typical heat rate
- [ ] **Sauna temperature history chart** — during an active/preheating session, show a sparkline of temp over time so users can see how quickly it's heating

---

## 🔧 Improvements

- [ ] **Controls tab: active booking context** — when a session is running, display the booker's name, original end time, and a visual countdown so users know the context before extending or turning off
- [ ] **Auto-shutoff edge case** — if a booking is cancelled *after* the scheduler has already run for that slot, the sauna may stay on; re-check shutdown logic for this race condition
- [ ] **Overlap feedback** — when a booking fails due to a conflict, highlight the clashing slot on the timeline rather than just showing a toast
- [ ] **Booking confirmation step** — show a summary (date, time, temp, duration) before saving to prevent accidental mis-bookings
- [ ] **Member switcher on booking modal** — when an admin creates a booking, allow assigning it to any member, not just themselves
- [ ] **Notification tap — deep link and refresh** — tapping a preheat or session-ending notification should navigate directly to the relevant booking slot *and* immediately re-fetch booking and sauna status rather than showing stale data
- [ ] **Push re-subscribe on VAPID key rotation** — if VAPID keys are regenerated, existing subscriptions silently break; detect 410/404 from the push service, delete the stale DB record, and prompt the user to re-enable notifications
- [ ] **Friendlier Harvia error messages** — when the Harvia device is offline, the toast shows a raw exception string; map common failure modes to human-readable messages (e.g. "Sauna device is unreachable — check its WiFi")
- [x] **Control log pagination** — the admin control log loads all records with no limit; add pagination or a "load more" button before the log grows unwieldy
- [ ] **Admin pending-count badge** — currently re-polls every 2 min even when not on the Admin tab; only poll when the badge is visible or the tab is active
- [ ] **Smarter Controls unlock window** — currently unlocks 90 min before any booking; expose this as a configurable admin setting

---

## 🎨 UI

- [ ] **Show current time marker on schedule** — a red "now" line across the timeline (like Google Calendar) so users can instantly see what's happening vs upcoming
- [ ] **Visual preheat countdown on booking card** — once "Preheat" is tapped, show a live "Heating… X°C / Y°C target" progress on the booking card instead of a static "Preheating" pill
- [ ] **Booking card shows temp** — the booking slot on the timeline only shows member name + time; add the target temp (e.g. "90°C") for useful at-a-glance info
- [ ] **Better empty state for Controls when sauna is off** — add a CTA "Book a session" or "Turn on now" with default settings for zero-friction access
- [ ] **Touch target sizes** — duration buttons and the temperature slider are too small for reliable thumb taps on 375px screens; minimum 44px touch targets per Apple HIG
- [ ] **Toasts not manually dismissible** — error toasts auto-disappear after 3.5s even when the user is still reading; tap-to-dismiss would prevent messages vanishing before the user can act
- [ ] **Toast overflow on small screens** — long error messages get cut off; add `word-wrap` and `max-width` so the full message is always readable
- [ ] **Install banner close button touch target too small** — the ✕ dismiss button uses minimal padding, well below the 44px Apple HIG minimum; easy to mis-tap on small phones
- [x] **Splash screen for iOS PWA** — add `apple-touch-startup-image` meta tags so the installed iOS PWA shows the app background instead of a white flash on launch
- [ ] **Colour conflict warning** — warn or prevent two members from using the same timeline colour so the schedule stays readable
- [ ] **Member colour picker — more options** — offer a larger palette or hex input so members can pick a truly unique colour
- [ ] **Onboarding tip for new members** — after a member is approved, show a brief one-time walkthrough (3 steps: how to book, how to preheat, how to enable notifications)
- [ ] **Swipe between days** — swipe left/right on the timeline to move between the 7-day pill dates on mobile
- [ ] **Animated status transitions** — smooth the status pill change (e.g. Off → Heating) with a brief colour crossfade
- [ ] **Dark/light mode** — currently hard-coded dark; follow system preference

---

## 🔒 Security

- [ ] **Re-authenticate for destructive actions** — require PIN confirmation before deleting a member or removing admin privileges

---

## ⚡ Performance

- [ ] **Vendor CDN assets** — React, Tailwind, and Babel are loaded from unpkg.com on every page load; if the CDN is down the app is completely broken; vendor the files into `/static` for reliability and faster load
- [ ] **Sauna status polling interval** — Controls tab polls on a fixed interval regardless of session state; poll more aggressively during preheat/active sessions, less aggressively when off
- [ ] **HTTP caching headers on API responses** — GET routes return no `Cache-Control` headers; short-lived caching (e.g. 10s) would reduce server load when multiple family members are polling simultaneously
- [ ] **Gunicorn concurrency** — a slow Harvia API call (up to 5s) can tie up one of 4 threads; consider async handling or returning cached state under timeout rather than blocking

---

## 🚀 Infrastructure

- [ ] **Error boundary** — wrap the React app in an error boundary so a JS crash shows a friendly message instead of a blank screen
- [ ] **Logging / Sentry** — add structured error logging or a Sentry integration so production crashes are visible
