# Sweat Box iOS App Todo

Plan for a native iOS wrapper that embeds the existing Sweat Box web app and adds Live Activity support for active sauna sessions.

## Known Issues / Gotchas

- **Queue Debugging on iOS 27**: In Xcode 26.5, the scheme option **Run → Diagnostics → "Queue debugging"** must be **OFF** when targeting an iOS 27 device. With it on, every app (including the stock Hello World template) crashes pre-main with `-[OS_dispatch_mach_msg _setContext:]: unrecognized selector`. The instrumentation uses libdispatch internals that iOS 27 no longer tolerates. Toggle off in Product → Scheme → Edit Scheme → Run → Diagnostics tab. **Verify this first on any new scheme before debugging anything else.**

## Handoff / Current State

**Status**: Local Live Activity flow confirmed working on device — start/update/end via the in-webview test bridge (`window.__sweatboxNativeTest.*`) and via real `/api/sauna/on` → `off` flow. Backend token capture, per-activity token storage, and an admin manual-push test endpoint all exist. **Remote pushes from the server have not been exercised end-to-end yet** — APNs env vars are not set in any environment, and there is no scheduler job that fans updates out to recorded tokens. That's the immediate next milestone.

**Where things are**:
- Xcode project: `ios/SweatBox/SweatBox.xcodeproj`
- App target source: `ios/SweatBox/SweatBox/` — SwiftUI shell, web bridge, ActivityKit manager, shared model, web view container
- Widget target source: `ios/SweatBox/SweatBoxWidget/` — Live Activity configuration (lock screen + Dynamic Island)
- Bundle ids: app `dev.cbj87.SweatBox`, widget `dev.cbj87.SweatBox.SweatBoxWidget` (capitalized — registered in Apple Developer)
- Production web URL: `https://sweatbox.cbj87.dev` (set in [AppConfig.swift](ios/SweatBox/SweatBox/AppConfig.swift))
- Apple Developer team: `RKF9VC4438`
- APNs key: created in Apple Developer (Sandbox + Production, Team Scoped). User holds the `.p8` and Key ID — these need to be exported as `APNS_*` env vars before remote pushes can fire.
- Registered App IDs: `dev.cbj87.sweatbox` + `dev.cbj87.sweatbox.widgets` (both with Push Notifications enabled)

**Backend already in place** (`harvia_server.py`):
- APNs JWT helper (`_apns_jwt`), Live Activity push sender (`_send_live_activity_apns`), env vars `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_PRIVATE_KEY`, `APNS_BUNDLE_ID`, `APNS_USE_SANDBOX`
- `_live_activity_payload_for_booking()` builds a normalized payload (booking name, current/target °F, remaining minutes, heat state)
- Token capture endpoints: `/api/native/device-token`, `/api/native/live-activity/push-to-start-token`, `/api/native/live-activity/token` (POST + DELETE)
- `/api/native/live-session` returns the current normalized payload (admin all-sessions opt-in honored)
- `/api/admin/native/live-activity/test` sends one push against the most-recent token — used for manual end-to-end verification
- Models: `NativeDevice`, `LiveActivityPushToStartToken`, `LiveActivityToken`, plus `FamilyMember.live_activity_all_sessions`

**What's missing for remote**:
- Lifecycle fan-out: no scheduler job pushes `update` events during an active session, and no hook on `/api/sauna/off` or `check_and_auto_shutoff()` pushes `end` events. Per-activity tokens just sit in the DB.
- No push-to-start fan-out when a booking transitions into active state on a device that hasn't started its own activity.
- Stale-token cleanup on APNs 410 (BadDeviceToken) is not implemented.
- APNs env vars are not set in `.env.local` or Railway.

**Previous session backstory** (so you don't repeat mistakes):
- A previous agent hand-rolled a project file (`ios/SweatBox_old/` — now deleted) which had subtle issues. Combined with the Queue Debugging trap above, this caused hours of debugging a pre-main crash that LOOKED like an iOS 27 SDK mismatch but was actually queue debugging. **Don't recreate the .xcodeproj — use the existing one. Don't waste cycles on entitlements/Info.plist tweaks if the app crashes pre-main — check Queue Debugging FIRST.**

**Immediate next step**: Wire APNs env vars locally, then add a scheduler-driven push fan-out for `update` and `end` events (Groups 9–11). Verify with the existing admin test endpoint first, then graduate to the scheduler job.

**Then proceed through groups in order.** Each group should be verified working before moving to the next. Don't pile on changes when something breaks — isolate by removing variables.

## Design Decisions

- [x] Full-screen `WKWebView` wrapper around the existing Sweat Box web app.
- [x] No native controls around the webpage.
- [ ] Live Activity shows active booking name, current temperature in Fahrenheit, and time remaining.
- [ ] Dynamic Island compact view should stay glanceable, e.g. `174° · 28m`.
- [ ] Users can see Live Activities for their own sessions.
- [ ] Admins can opt into Live Activities for everyone.
- [ ] Admin visibility overrides member privacy.
- [ ] Server-side 60 second temperature polling only runs while a sauna session is active.

## Group 1: Native App Shell

- [x] Create `ios/SweatBox/` Xcode project.
- [x] Add SwiftUI app target.
- [x] Add Widget Extension target for Live Activities.
- [x] Add app entitlements for Live Activities.
- [ ] Add App Group if shared storage is needed.
- [x] Add `AppConfig.swift` with production, test, and local web URLs.
- [x] Add user-agent suffix, e.g. `SweatBoxNative/1.0`.
- [x] Add full-screen `WebViewContainer.swift` using `WKWebView`.
- [x] Restrict in-webview navigation to the configured Sweat Box host.
- [x] Open external links in `SFSafariViewController`.
- [x] Handle WebContent process termination by reloading the webview.
- [x] Disable native pinch zoom so the web app remains stable.

## Group 2: Web-to-Native Bridge

- [x] Add `WebBridge.swift`.
- [ ] Define bridge channels:
  - [x] `startSaunaSession`
  - [x] `updateSaunaSession`
  - [x] `endSaunaSession`
  - [x] `repostLiveActivityTokens`
  - [x] `signOut`
- [x] Define typed payloads for start/update/end sauna session events.
- [x] Inject native device id into the webview as `window.__nativeDeviceId`.
- [x] Dispatch Live Activity token events from native to the webpage.
- [x] Dispatch push-to-start token events from native to the webpage.
- [x] Add graceful no-op behavior when the app is opened in a normal browser.

## Group 3: Frontend Integration

- [x] Add a small native bridge helper inside `static/index.html`.
- [x] Detect the native wrapper using the user-agent marker.
- [x] After `/api/sauna/on` succeeds, post `startSaunaSession`.
- [x] After `/api/sauna/off` succeeds, post `endSaunaSession`.
- [x] After status refreshes during an active session, post `updateSaunaSession` when running in native.
- [x] Listen for native Live Activity token events and upload them to Flask.
- [x] Listen for native push-to-start token events and upload them to Flask.
- [x] Add a native sign-out bridge call when the web app logs out.

## Group 4: ActivityKit Model

- [x] Add `Shared.swift` with `SaunaActivityAttributes`.
- [x] Add `ContentState` fields:
  - [x] `bookingId`
  - [x] `memberId`
  - [x] `bookingName`
  - [x] `currentTempF`
  - [x] `targetTempF`
  - [x] `remainingMinutes`
  - [x] `heatOn`
  - [x] `active`
  - [x] `updatedAtMillis`
- [x] Keep Swift `ContentState` schema aligned with backend APNs payload builder. (backend `_live_activity_payload_for_booking()` mirrors these keys exactly)
- [ ] Add tests or fixtures for APNs payload parity if the backend payload grows.

## Group 5: Activity Manager

- [x] Add `ActivityManager.swift`.
- [x] Start a local Live Activity immediately when the current device starts a session.
- [x] Update the local Live Activity when bridge updates arrive.
- [x] End the Live Activity when the sauna is turned off or the booking completes.
- [x] Observe per-activity push token updates.
- [x] Observe push-to-start token updates.
- [x] Repost cached tokens to the webview after reload or app foreground.
- [x] Reattach to existing ActivityKit activities after app relaunch when possible.

## Group 6: Live Activity UI

- [x] Create Widget Extension Live Activity configuration.
- [ ] Lock Screen layout:
  - [x] Active booking name
  - [x] Current temperature in Fahrenheit
  - [x] Time remaining
  - [x] Target temperature as secondary info
  - [x] Heating/on state
- [ ] Dynamic Island expanded layout:
  - [x] Booking name
  - [x] Current temp
  - [x] Remaining time
- [ ] Dynamic Island compact layout:
  - [x] Leading: heat icon or simple Sweat Box mark
  - [x] Trailing: `174° · 28m`
- [ ] Dynamic Island minimal layout:
  - [x] Heat icon or simple Sweat Box mark
- [ ] Add deep link so tapping the Live Activity opens the app to Controls.
- [ ] Verify layout in light and dark appearances.
- [ ] Verify text fits for long booking/member names.

## Group 7: Backend Data Model

- [x] Add native device table/model. (`NativeDevice` in [models.py](models.py))
- [x] Store native device id.
- [x] Store owning member id.
- [x] Store APNs device token if used for companion/silent pushes.
- [x] Add Live Activity push-to-start token table/model. (`LiveActivityPushToStartToken`)
- [x] Add per-activity Live Activity token table/model tied to `booking_id`. (`LiveActivityToken`)
- [x] Add token removal path when ActivityKit reports an ended activity. (`/api/native/live-activity/token` POST treats `state: ended|dismissed` as a delete)
- [x] Add member/admin preference for all-session Live Activity visibility. (`FamilyMember.live_activity_all_sessions`)
- [x] Add safe startup migrations for the new tables/columns.

## Group 8: Backend Native Endpoints

- [x] Add endpoint to upload native device APNs token. (`POST /api/native/device-token`)
- [x] Add endpoint to upload Live Activity push-to-start token. (`POST /api/native/live-activity/push-to-start-token`)
- [x] Add endpoint to upload/remove per-activity Live Activity token. (`POST` / `DELETE /api/native/live-activity/token`)
- [x] Add endpoint to return the normalized current live session payload. (`GET /api/native/live-session`)
- [ ] Add endpoint to update admin all-session Live Activity opt-in. (column exists; no dedicated PUT yet — currently only settable via DB browser)
- [ ] Enforce visibility on push fan-out:
  - [x] Session owner can receive their own session. (read endpoint honors this)
  - [x] Admin can receive everyone if opted in. (read endpoint honors this)
  - [ ] Admin override means members cannot hide active sessions from opted-in admins. (will need to be re-applied inside the fan-out job)
- [x] Ensure token endpoints require authenticated sessions.

## Group 9: Backend Live Activity Push

- [x] Add APNs configuration env vars:
  - [x] `APNS_KEY_ID`
  - [x] `APNS_TEAM_ID`
  - [x] `APNS_PRIVATE_KEY`
  - [x] `APNS_BUNDLE_ID`
  - [x] `APNS_USE_SANDBOX` (extra — toggle between sandbox/prod hosts)
- [x] Add APNs JWT signing helper. (`_apns_jwt()` — 45 min cache)
- [x] Add Live Activity push sender. (`_send_live_activity_apns()`)
- [x] Build APNs payload from one normalized sauna session object. (`_live_activity_payload_for_booking()`)
- [x] Admin manual test endpoint. (`POST /api/admin/native/live-activity/test`)
- [ ] Send `start` pushes to eligible devices when an active session begins. (needs push-to-start fan-out tied to `_current_live_booking` transitions)
- [x] Send `update` pushes to per-activity tokens during an active session. (`push_live_activity_updates` scheduler job, 60s interval)
- [x] Send `end` pushes when sauna turns off or booking completes. (`_fanout_live_activity_end` called from `/api/sauna/off` and `check_and_auto_shutoff()`)
- [x] Remove stale tokens on APNs stale-token responses. (`_push_live_activity_to_tokens` deletes on 400/410 with BadDeviceToken / Unregistered / ExpiredToken / DeviceTokenNotForTopic)
- [x] Log push failures without breaking scheduler jobs.

## Group 10: Active-Session-Only Polling

- [x] Add helper to find the active/preheating booking before contacting Harvia. (`_current_live_booking()`)
- [x] Skip Harvia status polling when there is no active/preheating booking. (early return in `push_live_activity_updates`)
- [x] Skip Live Activity update fanout when there is no active/preheating booking.
- [x] During an active/preheating booking, poll Harvia at most once per scheduler tick.
- [x] Reuse the same status payload for all eligible Live Activity recipients.
- [x] End any lingering Live Activities when the booking completes or the sauna is detected off.
- [ ] Avoid duplicate Harvia calls between existing scheduler jobs and Live Activity updates where practical. (`log_device_state` also calls `get_full_status` every 60s — could share cache later)
- [x] Add logs that distinguish skipped polling from failed polling.

## Group 11: Scheduler Integration

- [x] Add `push_live_activity_updates()` scheduler job.
- [x] Run it every 60 seconds.
- [x] Ensure the job exits quickly when no session is active.
- [x] Coordinate with `check_and_auto_shutoff()` so completed bookings end Live Activities.
- [x] Coordinate with manual `/api/sauna/off` so off events end Live Activities promptly.
- [x] Make the job safe if Harvia credentials are missing in UI-only dev.

## Group 12: Deep Links

- [ ] Register custom URL scheme, e.g. `sweatbox://`. (not yet in `Info.plist` / `CFBundleURLSchemes`)
- [x] Add Live Activity tap URL, e.g. `sweatbox://open?tab=controls`. (`widgetURL` set in [SweatBoxWidget.swift](ios/SweatBox/SweatBoxWidget/SweatBoxWidget.swift))
- [ ] Forward deep-link actions into the webview as `native-action` events. (need `onOpenURL` in `SweatBoxApp` + bridge dispatch)
- [ ] Update the web app to switch to the Controls tab from the native action.

## Group 13: Testing

- [ ] Test web app still works normally outside the native wrapper.
- [ ] Test local session start creates a Live Activity immediately.
- [ ] Test owner receives own Live Activity updates.
- [ ] Test admin does not receive everyone by default.
- [ ] Test opted-in admin receives everyone.
- [ ] Test normal user does not receive other members' sessions.
- [ ] Test Fahrenheit conversion matches the web app.
- [ ] Test time remaining uses booking end time, not unreliable device timer.
- [ ] Test midnight-spanning bookings.
- [ ] Test manual off ends Live Activity.
- [ ] Test auto-shutoff ends Live Activity.
- [ ] Test app relaunch while session is active.
- [ ] Test phone locked/backgrounded for several minutes.
- [ ] Test stale APNs token cleanup.

## Group 14: Release Prep

- [ ] Add app icon and launch assets.
- [ ] Add bundle id and provisioning profiles.
- [ ] Configure APNs key in Apple Developer account.
- [ ] Add Railway env vars for APNs.
- [ ] Add README notes for iOS build/run.
- [ ] Add test/prod configuration notes.
- [ ] Verify App Store privacy/data disclosures if distributing beyond personal devices.
