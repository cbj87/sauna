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
                .activityBackgroundTint(.black)
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
                    VStack(alignment: .trailing, spacing: 3) {
                        RemainingView(state: context.state, alignment: .trailing)
                            .font(.system(size: 19, weight: .heavy, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(heatGradient)
                            .lineLimit(1)
                            // Room to shrink rather than truncate — an hour-plus
                            // countdown is 7 glyphs, not 5.
                            .minimumScaleFactor(0.55)
                            .allowsTightening(true)
                        Text("Remaining")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }

                DynamicIslandExpandedRegion(.bottom) {
                    VStack(spacing: 8) {
                        HStack {
                            MetricView(title: "Current", value: temperatureText(context.state.currentTempF), alignment: .leading)
                            Spacer()
                            if let targetTempF = context.state.targetTempF {
                                MetricView(title: "Target", value: "\(targetTempF)°", alignment: .trailing)
                            }
                        }
                        ProgressBar(progress: heatingProgress(context.state))
                            .frame(height: 5)
                    }
                    .font(.caption)
                }
            } compactLeading: {
                Image(systemName: context.state.heatOn ? "flame.fill" : "power")
                    .foregroundStyle(context.state.heatOn ? heatGradient : LinearGradient(colors: [.secondary], startPoint: .top, endPoint: .bottom))
            } compactTrailing: {
                Text(compactText(context.state))
                    .font(.caption2.weight(.semibold))
                    .minimumScaleFactor(0.75)
            } minimal: {
                Image(systemName: context.state.heatOn ? "flame.fill" : "power")
                    .foregroundStyle(context.state.heatOn ? heatGradient : LinearGradient(colors: [.secondary], startPoint: .top, endPoint: .bottom))
            }
            .widgetURL(URL(string: "sweatbox://open?tab=controls"))
        }
    }
}

private struct LockScreenLiveActivityView: View {
    let state: SaunaActivityAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center) {
                Text(state.bookingName)
                    .font(.system(size: 15, weight: .bold, design: .rounded))
                    .foregroundStyle(.white.opacity(0.94))
                    .lineLimit(1)
                    .minimumScaleFactor(0.72)
                    .allowsTightening(true)

                Spacer()

                Image(systemName: state.heatOn ? "flame.fill" : "power")
                    .font(.system(size: 18, weight: .bold))
                    .foregroundStyle(state.heatOn ? heatGradient : LinearGradient(colors: [.white.opacity(0.5)], startPoint: .top, endPoint: .bottom))
                    .shadow(color: state.heatOn ? ember.opacity(0.7) : .clear, radius: 10)
            }

            // The countdown is sized to its content and takes layout priority,
            // so it claims the width it needs before the two temp columns split
            // the remainder. An equal three-way split truncates the moment the
            // timer rolls past an hour and needs "1:12:34" instead of "12:34".
            // Note the timer column deliberately has no maxWidth: .infinity —
            // greedy *and* high-priority would starve the temps entirely.
            HStack(alignment: .center, spacing: 8) {
                MetricView(title: "Current", value: temperatureText(state.currentTempF), alignment: .center)
                    .frame(maxWidth: .infinity)

                DividerLine()

                VStack(spacing: 1) {
                    RemainingView(state: state, alignment: .center)
                        .font(.system(size: 34, weight: .heavy, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(heatGradient)
                        .lineLimit(1)
                        .minimumScaleFactor(0.5)
                        .allowsTightening(true)
                        .shadow(color: ember.opacity(0.38), radius: 10)
                    Text("Remaining")
                        .font(.system(size: 12, weight: .semibold, design: .rounded))
                        .foregroundStyle(.white.opacity(0.58))
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                        .allowsTightening(true)
                }
                .layoutPriority(1)

                DividerLine()

                MetricView(title: "Target", value: temperatureText(state.targetTempF), alignment: .center)
                    .frame(maxWidth: .infinity)
            }

            ProgressBar(progress: heatingProgress(state))
                .frame(height: 5)
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 10)
        .foregroundStyle(.white)
        .containerBackground(for: .widget) {
            ZStack {
                LinearGradient(
                    colors: [
                        Color(red: 0.12, green: 0.12, blue: 0.12),
                        Color(red: 0.03, green: 0.04, blue: 0.05)
                    ],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
                RadialGradient(
                    colors: [ember.opacity(0.13), .clear],
                    center: .center,
                    startRadius: 6,
                    endRadius: 180
                )
                .blendMode(.screen)
                RadialGradient(
                    colors: [
                        Color(red: 1.0, green: 0.65, blue: 0.25).opacity(0.12),
                        .clear
                    ],
                    center: .topTrailing,
                    startRadius: 4,
                    endRadius: 130
                )
                .blendMode(.screen)
            }
        }
        .background {
            ZStack {
                RadialGradient(
                    colors: [ember.opacity(0.08), .clear],
                    center: .center,
                    startRadius: 6,
                    endRadius: 150
                )
                .blendMode(.screen)
            }
        }
    }
}

/// Remaining-time readout. When the state carries an end date, this renders a
/// system countdown timer that ticks by itself — the Live Activity stays
/// correct even when no update push arrives. Falls back to the static
/// remainingMinutes for old payloads or ended sessions.
private struct RemainingView: View {
    let state: SaunaActivityAttributes.ContentState
    let alignment: TextAlignment

    var body: some View {
        if let end = state.endDate, state.active, end > Date() {
            Text(timerInterval: Date.now...end, countsDown: true)
                .monospacedDigit()
                .multilineTextAlignment(alignment)
        } else {
            Text(remainingText(state))
        }
    }
}

private struct MetricView: View {
    let title: String
    let value: String
    let alignment: HorizontalAlignment

    var body: some View {
        VStack(alignment: alignment, spacing: 1) {
            Text(value)
                .font(.system(size: 32, weight: .bold, design: .rounded))
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.5)
                .allowsTightening(true)
            Text(title)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(.white.opacity(0.48))
                .lineLimit(1)
        }
    }
}

private struct DividerLine: View {
    var body: some View {
        Rectangle()
            .fill(.white.opacity(0.18))
            .frame(width: 1, height: 46)
    }
}

private struct ProgressBar: View {
    let progress: Double

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(.white.opacity(0.12))
                Capsule()
                    .fill(heatGradient)
                    .frame(width: max(proxy.size.height, proxy.size.width * progress))
                    .shadow(color: ember.opacity(0.8), radius: 10)
            }
        }
    }
}

private let ember = Color(red: 1.0, green: 0.32, blue: 0.02)
private let heatGradient = LinearGradient(
    colors: [
        Color(red: 1.0, green: 0.33, blue: 0.06),
        Color(red: 1.0, green: 0.67, blue: 0.12)
    ],
    startPoint: .leading,
    endPoint: .trailing
)

private func temperatureText(_ temp: Int?) -> String {
    temp.map { "\($0)°" } ?? "--"
}

private func heatingProgress(_ state: SaunaActivityAttributes.ContentState) -> Double {
    guard
        let currentTempF = state.currentTempF,
        let targetTempF = state.targetTempF,
        targetTempF > 0
    else {
        return state.heatOn ? 0.12 : 0
    }

    return min(1, max(0.04, Double(currentTempF) / Double(targetTempF)))
}

/// Static fallback for ended sessions and pre-endDate payloads. Splits out
/// hours so a long session doesn't read as an unhelpful "112m".
private func remainingText(_ state: SaunaActivityAttributes.ContentState) -> String {
    guard let remainingMinutes = state.remainingMinutes else { return "--" }
    let mins = max(0, remainingMinutes)
    if mins < 60 { return "\(mins)m" }
    let (h, m) = (mins / 60, mins % 60)
    return m == 0 ? "\(h)h" : "\(h)h \(m)m"
}

private func compactText(_ state: SaunaActivityAttributes.ContentState) -> String {
    let temp = temperatureText(state.currentTempF)
    return "\(temp) · \(remainingText(state))"
}
