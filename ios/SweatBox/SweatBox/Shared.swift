import ActivityKit
import Foundation

struct SaunaActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        let bookingId: Int?
        let memberId: Int?
        let bookingName: String
        let currentTempF: Int?
        let targetTempF: Int?
        let remainingMinutes: Int?
        /// Epoch millis when the session ends. The widget recomputes its minute
        /// readout from this on every redraw, so remaining time stays right
        /// between pushes even though it no longer ticks on its own.
        /// Optional so pushes from older server builds still decode.
        let endsAtMillis: Int64?
        let heatOn: Bool
        let active: Bool
        let updatedAtMillis: Int64

        var endDate: Date? {
            endsAtMillis.map { Date(timeIntervalSince1970: TimeInterval($0) / 1000) }
        }
    }

    let nativeDeviceId: String
    let createdAtMillis: Int64
}

struct SaunaSessionPayload: Codable {
    let bookingId: Int?
    let memberId: Int?
    let bookingName: String?
    let currentTempF: Int?
    let targetTempF: Int?
    let remainingMinutes: Int?
    let endsAtMillis: Int64?
    let heatOn: Bool?
    let active: Bool?
    let updatedAtMillis: Int64?

    var contentState: SaunaActivityAttributes.ContentState {
        SaunaActivityAttributes.ContentState(
            bookingId: bookingId,
            memberId: memberId,
            bookingName: bookingName?.isEmpty == false ? bookingName! : "Sauna Session",
            currentTempF: currentTempF,
            targetTempF: targetTempF,
            remainingMinutes: remainingMinutes,
            endsAtMillis: endsAtMillis ?? Self.fallbackEndsAtMillis(remainingMinutes),
            heatOn: heatOn ?? false,
            active: active ?? false,
            updatedAtMillis: updatedAtMillis ?? Self.nowMillis
        )
    }

    /// Older web payloads only carry remainingMinutes — derive an end date so
    /// the countdown still works.
    private static func fallbackEndsAtMillis(_ remainingMinutes: Int?) -> Int64? {
        remainingMinutes.map { nowMillis + Int64($0) * 60_000 }
    }

    private static var nowMillis: Int64 {
        Int64(Date().timeIntervalSince1970 * 1000)
    }
}

struct EndSaunaSessionPayload: Codable {
    let bookingId: Int?
    let reason: String?
    let updatedAtMillis: Int64?
}

struct LiveActivityTokenPayload: Codable {
    let bookingId: Int?
    let token: String
    let state: String?
    /// Build env ("sandbox" | "production") rides along on every token upload
    /// so the server's per-device APNs host can never go stale — a wrong host
    /// means BadDeviceToken and the server dropping the token (frozen LA).
    let environment: String
    let updatedAtMillis: Int64
}

struct PushToStartTokenPayload: Codable {
    let token: String
    let environment: String
    let updatedAtMillis: Int64
}

struct LiveActivityStatusPayload: Codable {
    let status: String
    let activityId: String?
    let bookingId: Int?
    let message: String?
    let updatedAtMillis: Int64
}

struct RemoteNotificationTokenPayload: Codable {
    let token: String
    /// "sandbox" for dev/Xcode builds, "production" for TestFlight/App Store.
    /// APNs sandbox and production run parallel infrastructures; a token is
    /// only valid against the environment that issued it. The server needs
    /// this to pick the right APNs host per-device.
    let environment: String
    let updatedAtMillis: Int64
}

enum APNsEnvironment {
    static var current: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }
}
