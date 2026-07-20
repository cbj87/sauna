import ActivityKit
import Foundation

@MainActor
final class ActivityManager {
    var onLiveActivityToken: ((LiveActivityTokenPayload) -> Void)?
    var onPushToStartToken: ((PushToStartTokenPayload) -> Void)?
    var onLiveActivityStatus: ((LiveActivityStatusPayload) -> Void)?

    private var currentActivity: Activity<SaunaActivityAttributes>?
    private var lastState: SaunaActivityAttributes.ContentState?
    private var tokenTasks: [String: Task<Void, Never>] = [:]
    private var pushToStartTask: Task<Void, Never>?
    private var activityUpdatesTask: Task<Void, Never>?
    private var cachedLiveActivityToken: LiveActivityTokenPayload?
    private var cachedPushToStartToken: PushToStartTokenPayload?

    init() {
        reattachToExistingActivity()
        observeActivityUpdates()
        observePushToStartTokens()
    }

    deinit {
        tokenTasks.values.forEach { $0.cancel() }
        pushToStartTask?.cancel()
        activityUpdatesTask?.cancel()
    }

    func startSession(_ payload: SaunaSessionPayload, nativeDeviceId: String) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            sendStatus(
                status: "disabled",
                bookingId: payload.bookingId,
                message: "Live Activities are disabled for this device or app."
            )
            return
        }

        Task {
            await endAllActivities(dismissalPolicy: .immediate)

            do {
                let attributes = SaunaActivityAttributes(
                    nativeDeviceId: nativeDeviceId,
                    createdAtMillis: Int64(Date().timeIntervalSince1970 * 1000)
                )
                let state = payload.contentState
                let activity = try requestActivity(attributes: attributes, state: state, bookingId: payload.bookingId)
                currentActivity = activity
                lastState = state
                observePushTokenUpdates(for: activity)
                sendStatus(status: "started", activityId: activity.id, bookingId: state.bookingId, message: nil)
            } catch {
                sendStatus(status: "failed", bookingId: payload.bookingId, message: "\(error)")
            }
        }
    }

    private func requestActivity(
        attributes: SaunaActivityAttributes,
        state: SaunaActivityAttributes.ContentState,
        bookingId: Int?
    ) throws -> Activity<SaunaActivityAttributes> {
        do {
            return try Activity.request(
                attributes: attributes,
                content: ActivityContent(state: state, staleDate: nil),
                pushType: .token
            )
        } catch {
            sendStatus(
                status: "push-token-failed",
                bookingId: bookingId,
                message: "Starting without a push token after push-token request failed: \(error)"
            )
            return try Activity.request(
                attributes: attributes,
                content: ActivityContent(state: state, staleDate: nil),
                pushType: nil
            )
        }
    }

    func updateSession(_ payload: SaunaSessionPayload) {
        guard let activity = activity(for: payload.bookingId) else { return }

        Task {
            let state = payload.contentState
            await activity.update(ActivityContent(state: state, staleDate: nil))
            currentActivity = activity
            lastState = state
            sendStatus(status: "updated", activityId: activity.id, bookingId: state.bookingId, message: nil)
        }
    }

    func endSession(_ payload: EndSaunaSessionPayload) {
        guard let activity = activity(for: payload.bookingId) else { return }

        Task {
            let finalState = endedState(for: payload)
            await activity.end(
                ActivityContent(state: finalState, staleDate: nil),
                dismissalPolicy: .immediate
            )
            clearActivity(activity)
            // One sauna → at most one Live Activity. Sweep any strays (e.g. a
            // remote-started duplicate) so an "off" always clears the Lock Screen.
            await endAllActivities(dismissalPolicy: .immediate)
            sendStatus(status: "ended", activityId: activity.id, bookingId: payload.bookingId, message: payload.reason)
        }
    }

    func repostCachedTokens() {
        if let cachedLiveActivityToken {
            onLiveActivityToken?(cachedLiveActivityToken)
        }
        if let cachedPushToStartToken {
            onPushToStartToken?(cachedPushToStartToken)
        }
    }

    private func reattachToExistingActivity() {
        guard let activity = Activity<SaunaActivityAttributes>.activities.first else { return }
        currentActivity = activity
        lastState = activity.content.state
        observePushTokenUpdates(for: activity)
    }

    private func activity(for bookingId: Int?) -> Activity<SaunaActivityAttributes>? {
        // Prefer a bookingId match when one is available — handles the rare case
        // of multiple concurrent activities.
        if let bookingId,
           let matching = Activity<SaunaActivityAttributes>.activities.first(where: { $0.content.state.bookingId == bookingId }) {
            currentActivity = matching
            lastState = matching.content.state
            observePushTokenUpdates(for: matching)
            return matching
        }

        // Fall back to the current activity regardless of bookingId. We start at
        // most one Sauna activity at a time, and start/end calls can disagree on
        // bookingId (e.g. handleBookNow starts with null because the booking was
        // just created and its id wasn't captured) — strict matching would
        // strand the activity, so prefer "the one we know about" over silence.
        if let currentActivity {
            return currentActivity
        }

        // Last resort: adopt any running Sauna activity (post-relaunch case).
        if let first = Activity<SaunaActivityAttributes>.activities.first {
            currentActivity = first
            lastState = first.content.state
            observePushTokenUpdates(for: first)
            return first
        }

        return nil
    }

    /// End every activity of our type, optionally keeping one. There is one
    /// sauna, so anything beyond a single activity is a stray — e.g. the
    /// duplicate a push-to-start creates next to a broken one.
    private func endAllActivities(except keepId: String? = nil, dismissalPolicy: ActivityUIDismissalPolicy) async {
        for activity in Activity<SaunaActivityAttributes>.activities where activity.id != keepId {
            let finalState = endedState(for: nil, basedOn: activity.content.state)
            await activity.end(ActivityContent(state: finalState, staleDate: nil), dismissalPolicy: dismissalPolicy)
            clearActivity(activity)
        }
    }

    private func endedState(
        for payload: EndSaunaSessionPayload?,
        basedOn state: SaunaActivityAttributes.ContentState? = nil
    ) -> SaunaActivityAttributes.ContentState {
        let state = state ?? lastState
        return SaunaActivityAttributes.ContentState(
            bookingId: payload?.bookingId ?? state?.bookingId,
            memberId: state?.memberId,
            bookingName: state?.bookingName ?? "Sauna Session",
            currentTempF: state?.currentTempF,
            targetTempF: state?.targetTempF,
            remainingMinutes: 0,
            endsAtMillis: nil,
            heatOn: false,
            active: false,
            updatedAtMillis: payload?.updatedAtMillis ?? Int64(Date().timeIntervalSince1970 * 1000)
        )
    }

    private func clearActivity(_ activity: Activity<SaunaActivityAttributes>) {
        tokenTasks[activity.id]?.cancel()
        tokenTasks.removeValue(forKey: activity.id)
        if currentActivity?.id == activity.id {
            currentActivity = nil
            lastState = nil
            cachedLiveActivityToken = nil
        }
    }

    private func observePushTokenUpdates(for activity: Activity<SaunaActivityAttributes>) {
        guard tokenTasks[activity.id] == nil else { return }

        tokenTasks[activity.id] = Task { [weak self] in
            for await tokenData in activity.pushTokenUpdates {
                let payload = LiveActivityTokenPayload(
                    bookingId: activity.content.state.bookingId,
                    token: tokenData.hexString,
                    state: activity.activityState.description,
                    environment: APNsEnvironment.current,
                    updatedAtMillis: Int64(Date().timeIntervalSince1970 * 1000)
                )
                await MainActor.run {
                    self?.cachedLiveActivityToken = payload
                    self?.onLiveActivityToken?(payload)
                }
            }
        }
    }

    private func observeActivityUpdates() {
        activityUpdatesTask = Task { [weak self] in
            for await activity in Activity<SaunaActivityAttributes>.activityUpdates {
                await MainActor.run {
                    self?.currentActivity = activity
                    self?.lastState = activity.content.state
                    self?.observePushTokenUpdates(for: activity)
                    self?.sendStatus(status: "adopted", activityId: activity.id, bookingId: activity.content.state.bookingId, message: nil)
                }
                // A push-to-start creates a brand-new activity and can't end
                // the old one itself — enforce the one-activity invariant on
                // every adoption.
                await self?.endAllActivities(except: activity.id, dismissalPolicy: .immediate)
            }
        }
    }

    private func observePushToStartTokens() {
        guard #available(iOS 17.2, *) else { return }

        if let tokenData = Activity<SaunaActivityAttributes>.pushToStartToken {
            emitPushToStartToken(tokenData)
        }

        pushToStartTask = Task { [weak self] in
            for await tokenData in Activity<SaunaActivityAttributes>.pushToStartTokenUpdates {
                await MainActor.run { self?.emitPushToStartToken(tokenData) }
            }
        }
    }

    private func emitPushToStartToken(_ tokenData: Data) {
        let payload = PushToStartTokenPayload(
            token: tokenData.hexString,
            environment: APNsEnvironment.current,
            updatedAtMillis: Int64(Date().timeIntervalSince1970 * 1000)
        )
        cachedPushToStartToken = payload
        onPushToStartToken?(payload)
    }

    private func sendStatus(status: String, activityId: String? = nil, bookingId: Int?, message: String?) {
        onLiveActivityStatus?(
            LiveActivityStatusPayload(
                status: status,
                activityId: activityId,
                bookingId: bookingId,
                message: message,
                updatedAtMillis: Int64(Date().timeIntervalSince1970 * 1000)
            )
        )
    }
}

private extension Data {
    var hexString: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

private extension ActivityState {
    var description: String {
        switch self {
        case .active:
            return "active"
        case .dismissed:
            return "dismissed"
        case .ended:
            return "ended"
        case .pending:
            return "pending"
        case .stale:
            return "stale"
        @unknown default:
            return "unknown"
        }
    }
}
