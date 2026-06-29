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
        let heatOn: Bool
        let active: Bool
        let updatedAtMillis: Int64
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
            heatOn: heatOn ?? false,
            active: active ?? false,
            updatedAtMillis: updatedAtMillis ?? Self.nowMillis
        )
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
    let updatedAtMillis: Int64
}

struct PushToStartTokenPayload: Codable {
    let token: String
    let updatedAtMillis: Int64
}

struct LiveActivityStatusPayload: Codable {
    let status: String
    let activityId: String?
    let bookingId: Int?
    let message: String?
    let updatedAtMillis: Int64
}
