import ActivityKit
import SwiftUI
import WidgetKit

@main
struct SweatBoxWidgetBundle: WidgetBundle {
    var body: some Widget {
        SweatBoxLiveActivityWidget()
    }
}

struct SweatBoxLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: SaunaActivityAttributes.self) { context in
            LockScreenLiveActivityView(state: context.state)
                .activityBackgroundTint(Color(red: 0.08, green: 0.10, blue: 0.14))
                .activitySystemActionForegroundColor(.orange)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(context.state.bookingName)
                            .font(.caption.weight(.semibold))
                            .lineLimit(1)
                        Text(context.state.heatOn ? "Heating" : "Off")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }

                DynamicIslandExpandedRegion(.trailing) {
                    TemperatureStack(state: context.state)
                }

                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Label(remainingText(context.state), systemImage: "timer")
                        Spacer()
                        if let targetTempF = context.state.targetTempF {
                            Text("Target \(targetTempF)°")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            } compactLeading: {
                Image(systemName: context.state.heatOn ? "flame.fill" : "power")
                    .foregroundStyle(context.state.heatOn ? .orange : .secondary)
            } compactTrailing: {
                Text(compactText(context.state))
                    .font(.caption2.weight(.semibold))
                    .minimumScaleFactor(0.75)
            } minimal: {
                Image(systemName: context.state.heatOn ? "flame.fill" : "power")
                    .foregroundStyle(context.state.heatOn ? .orange : .secondary)
            }
            .widgetURL(URL(string: "sweatbox://open?tab=controls"))
        }
    }
}

private struct LockScreenLiveActivityView: View {
    let state: SaunaActivityAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(state.bookingName)
                        .font(.headline)
                        .lineLimit(1)
                    Text(state.heatOn ? "Sauna heating" : "Sauna off")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Image(systemName: state.heatOn ? "flame.fill" : "power")
                    .font(.title3)
                    .foregroundStyle(state.heatOn ? .orange : .secondary)
            }

            HStack(spacing: 18) {
                TemperatureStack(state: state)
                MetricView(title: "Remaining", value: remainingText(state))
                if let targetTempF = state.targetTempF {
                    MetricView(title: "Target", value: "\(targetTempF)°")
                }
            }
        }
        .padding()
        .foregroundStyle(.white)
    }
}

private struct TemperatureStack: View {
    let state: SaunaActivityAttributes.ContentState

    var body: some View {
        MetricView(
            title: "Current",
            value: state.currentTempF.map { "\($0)°" } ?? "--"
        )
    }
}

private struct MetricView: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.title3.weight(.bold))
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}

private func remainingText(_ state: SaunaActivityAttributes.ContentState) -> String {
    guard let remainingMinutes = state.remainingMinutes else { return "--" }
    return "\(max(0, remainingMinutes))m"
}

private func compactText(_ state: SaunaActivityAttributes.ContentState) -> String {
    let temp = state.currentTempF.map { "\($0)°" } ?? "--"
    return "\(temp) · \(remainingText(state))"
}
