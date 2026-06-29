# Sweat Box iOS App Todo

Plan for a native iOS wrapper that embeds the existing Sweat Box web app and adds Live Activity support for active sauna sessions.

## Design Decisions

- [ ] Full-screen `WKWebView` wrapper around the existing Sweat Box web app.
- [ ] No native controls around the webpage.
- [ ] Live Activity shows active booking name, current temperature in Fahrenheit, and time remaining.
- [ ] Dynamic Island compact view should stay glanceable, e.g. `174° · 28m`.
- [ ] Users can see Live Activities for their own sessions.
- [ ] Admins can opt into Live Activities for everyone.
- [ ] Admin visibility overrides member privacy.
- [ ] Server-side 60 second temperature polling only runs while a sauna session is active.

## Group 1: Native App Shell

- [ ] Create `ios/SweatBox/` Xcode project.
- [ ] Add SwiftUI app target.
- [ ] Add Widget Extension target for Live Activities.
- [ ] Add app entitlements for Live Activities.
- [ ] Add App Group if shared storage is needed.
- [ ] Add `AppConfig.swift` with production, test, and local web URLs.
- [ ] Add user-agent suffix, e.g. `SweatBoxNative/1.0`.
- [ ] Add full-screen `WebViewContainer.swift` using `WKWebView`.
- [ ] Restrict in-webview navigation to the configured Sweat Box host.
- [ ] Open external links in `SFSafariViewController`.
- [ ] Handle WebContent process termination by reloading the webview.
- [ ] Disable native pinch zoom so the web app remains stable.

## Group 2: Web-to-Native Bridge

- [ ] Add `WebBridge.swift`.
- [ ] Define bridge channels:
  - [ ] `startSaunaSession`
  - [ ] `updateSaunaSession`
  - [ ] `endSaunaSession`
  - [ ] `repostLiveActivityTokens`
  - [ ] `signOut`
- [ ] Define typed payloads for start/update/end sauna session events.
- [ ] Inject native device id into the webview as `window.__nativeDeviceId`.
- [ ] Dispatch Live Activity token events from native to the webpage.
- [ ] Dispatch push-to-start token events from native to the webpage.
- [ ] Add graceful no-op behavior when the app is opened in a normal browser.

## Group 3: Frontend Integration

- [ ] Add a small native bridge helper inside `static/index.html`.
- [ ] Detect the native wrapper using the user-agent marker.
- [ ] After `/api/sauna/on` succeeds, post `startSaunaSession`.
- [ ] After `/api/sauna/off` succeeds, post `endSaunaSession`.
- [ ] After status refreshes during an active session, post `updateSaunaSession` when running in native.
- [ ] Listen for native Live Activity token events and upload them to Flask.
- [ ] Listen for native push-to-start token events and upload them to Flask.
- [ ] Add a native sign-out bridge call when the web app logs out.

## Group 4: ActivityKit Model

- [ ] Add `Shared.swift` with `SaunaActivityAttributes`.
- [ ] Add `ContentState` fields:
  - [ ] `bookingId`
  - [ ] `memberId`
  - [ ] `bookingName`
  - [ ] `currentTempF`
  - [ ] `targetTempF`
  - [ ] `remainingMinutes`
  - [ ] `heatOn`
  - [ ] `active`
  - [ ] `updatedAtMillis`
- [ ] Keep Swift `ContentState` schema aligned with backend APNs payload builder.
- [ ] Add tests or fixtures for APNs payload parity if the backend payload grows.

## Group 5: Activity Manager

- [ ] Add `ActivityManager.swift`.
- [ ] Start a local Live Activity immediately when the current device starts a session.
- [ ] Update the local Live Activity when bridge updates arrive.
- [ ] End the Live Activity when the sauna is turned off or the booking completes.
- [ ] Observe per-activity push token updates.
- [ ] Observe push-to-start token updates.
- [ ] Repost cached tokens to the webview after reload or app foreground.
- [ ] Reattach to existing ActivityKit activities after app relaunch when possible.

## Group 6: Live Activity UI

- [ ] Create Widget Extension Live Activity configuration.
- [ ] Lock Screen layout:
  - [ ] Active booking name
  - [ ] Current temperature in Fahrenheit
  - [ ] Time remaining
  - [ ] Target temperature as secondary info
  - [ ] Heating/on state
- [ ] Dynamic Island expanded layout:
  - [ ] Booking name
  - [ ] Current temp
  - [ ] Remaining time
- [ ] Dynamic Island compact layout:
  - [ ] Leading: heat icon or simple Sweat Box mark
  - [ ] Trailing: `174° · 28m`
- [ ] Dynamic Island minimal layout:
  - [ ] Heat icon or simple Sweat Box mark
- [ ] Add deep link so tapping the Live Activity opens the app to Controls.
- [ ] Verify layout in light and dark appearances.
- [ ] Verify text fits for long booking/member names.

## Group 7: Backend Data Model

- [ ] Add native device table/model.
- [ ] Store native device id.
- [ ] Store owning member id.
- [ ] Store APNs device token if used for companion/silent pushes.
- [ ] Add Live Activity push-to-start token table/model.
- [ ] Add per-activity Live Activity token table/model tied to `booking_id`.
- [ ] Add token removal path when ActivityKit reports an ended activity.
- [ ] Add member/admin preference for all-session Live Activity visibility.
- [ ] Add safe startup migrations for the new tables/columns.

## Group 8: Backend Native Endpoints

- [ ] Add endpoint to upload native device APNs token.
- [ ] Add endpoint to upload Live Activity push-to-start token.
- [ ] Add endpoint to upload/remove per-activity Live Activity token.
- [ ] Add endpoint to return the normalized current live session payload.
- [ ] Add endpoint to update admin all-session Live Activity opt-in.
- [ ] Enforce visibility:
  - [ ] Session owner can receive their own session.
  - [ ] Admin can receive everyone if opted in.
  - [ ] Admin override means members cannot hide active sessions from opted-in admins.
- [ ] Ensure token endpoints require authenticated sessions.

## Group 9: Backend Live Activity Push

- [ ] Add APNs configuration env vars:
  - [ ] `APNS_KEY_ID`
  - [ ] `APNS_TEAM_ID`
  - [ ] `APNS_PRIVATE_KEY`
  - [ ] `APNS_BUNDLE_ID`
- [ ] Add APNs JWT signing helper.
- [ ] Add Live Activity push sender.
- [ ] Build APNs payload from one normalized sauna session object.
- [ ] Send `start` pushes to eligible devices when an active session begins.
- [ ] Send `update` pushes to per-activity tokens during an active session.
- [ ] Send `end` pushes when sauna turns off or booking completes.
- [ ] Remove stale tokens on APNs stale-token responses.
- [ ] Log push failures without breaking scheduler jobs.

## Group 10: Active-Session-Only Polling

- [ ] Add helper to find the active/preheating booking before contacting Harvia.
- [ ] Skip Harvia status polling when there is no active/preheating booking.
- [ ] Skip Live Activity update fanout when there is no active/preheating booking.
- [ ] During an active/preheating booking, poll Harvia at most once per scheduler tick.
- [ ] Reuse the same status payload for all eligible Live Activity recipients.
- [ ] End any lingering Live Activities when the booking completes or the sauna is detected off.
- [ ] Avoid duplicate Harvia calls between existing scheduler jobs and Live Activity updates where practical.
- [ ] Add logs that distinguish skipped polling from failed polling.

## Group 11: Scheduler Integration

- [ ] Add `push_live_activity_updates()` scheduler job.
- [ ] Run it every 60 seconds.
- [ ] Ensure the job exits quickly when no session is active.
- [ ] Coordinate with `check_and_auto_shutoff()` so completed bookings end Live Activities.
- [ ] Coordinate with manual `/api/sauna/off` so off events end Live Activities promptly.
- [ ] Make the job safe if Harvia credentials are missing in UI-only dev.

## Group 12: Deep Links

- [ ] Register custom URL scheme, e.g. `sweatbox://`.
- [ ] Add Live Activity tap URL, e.g. `sweatbox://open?tab=controls`.
- [ ] Forward deep-link actions into the webview as `native-action` events.
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
