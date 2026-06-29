import Combine
import Foundation
import WebKit

@MainActor
final class WebBridge: NSObject, ObservableObject, WKScriptMessageHandler {
    enum Channel: String, CaseIterable {
        case startSaunaSession
        case updateSaunaSession
        case endSaunaSession
        case repostLiveActivityTokens
        case signOut
    }

    private enum StorageKey {
        static let nativeDeviceId = "nativeDeviceId"
    }

    private weak var webView: WKWebView?
    private let activityManager = ActivityManager()

    let nativeDeviceId: String

    override init() {
        if let existing = UserDefaults.standard.string(forKey: StorageKey.nativeDeviceId), !existing.isEmpty {
            nativeDeviceId = existing
        } else {
            let created = UUID().uuidString
            UserDefaults.standard.set(created, forKey: StorageKey.nativeDeviceId)
            nativeDeviceId = created
        }

        super.init()

        activityManager.onLiveActivityToken = { [weak self] payload in
            self?.dispatchLiveActivityToken(payload)
        }
        activityManager.onPushToStartToken = { [weak self] payload in
            self?.dispatchPushToStartToken(payload)
        }
        activityManager.onLiveActivityStatus = { [weak self] payload in
            self?.dispatchLiveActivityStatus(payload)
        }
    }

    func configure(_ configuration: WKWebViewConfiguration) {
        let controller = WKUserContentController()
        Channel.allCases.forEach { controller.add(self, name: $0.rawValue) }
        controller.addUserScript(nativeDeviceIdScript())
        configuration.userContentController = controller
    }

    func attach(webView: WKWebView) {
        self.webView = webView
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard let channel = Channel(rawValue: message.name) else { return }

        switch channel {
        case .startSaunaSession:
            handleStartSaunaSession(message.body)
        case .updateSaunaSession:
            handleUpdateSaunaSession(message.body)
        case .endSaunaSession:
            handleEndSaunaSession(message.body)
        case .repostLiveActivityTokens:
            repostLiveActivityTokens()
        case .signOut:
            handleSignOut()
        }
    }

    func dispatchLiveActivityToken(_ payload: LiveActivityTokenPayload) {
        dispatchNativeEvent(name: "live-activity-token", payload: payload)
    }

    func dispatchPushToStartToken(_ payload: PushToStartTokenPayload) {
        dispatchNativeEvent(name: "push-to-start-token", payload: payload)
    }

    func dispatchLiveActivityStatus(_ payload: LiveActivityStatusPayload) {
        dispatchNativeEvent(name: "live-activity-status", payload: payload)
    }

    private func handleStartSaunaSession(_ body: Any) {
        guard let payload = decode(SaunaSessionPayload.self, from: body) else { return }
        activityManager.startSession(payload, nativeDeviceId: nativeDeviceId)
    }

    private func handleUpdateSaunaSession(_ body: Any) {
        guard let payload = decode(SaunaSessionPayload.self, from: body) else { return }
        activityManager.updateSession(payload)
    }

    private func handleEndSaunaSession(_ body: Any) {
        guard let payload = decode(EndSaunaSessionPayload.self, from: body) else { return }
        activityManager.endSession(payload)
    }

    private func repostLiveActivityTokens() {
        activityManager.repostCachedTokens()
    }

    private func handleSignOut() {
        // Token cleanup is intentionally deferred until native token storage exists.
    }

    private func nativeDeviceIdScript() -> WKUserScript {
        let encodedDeviceId = Self.jsonString(nativeDeviceId)
        let debugHelpers: String
        #if DEBUG
        debugHelpers = """
        window.__sweatboxNativeTest = {
          startLiveActivity: function(overrides) {
            var payload = Object.assign({
              bookingId: 999001,
              memberId: 1,
              bookingName: "Test Session",
              currentTempF: 174,
              targetTempF: 194,
              remainingMinutes: 28,
              heatOn: true,
              active: true,
              updatedAtMillis: Date.now()
            }, overrides || {});
            window.webkit.messageHandlers.startSaunaSession.postMessage(payload);
            return { posted: true, channel: "startSaunaSession", payload: payload };
          },
          updateLiveActivity: function(overrides) {
            var payload = Object.assign({
              bookingId: 999001,
              memberId: 1,
              bookingName: "Test Session",
              currentTempF: 181,
              targetTempF: 194,
              remainingMinutes: 19,
              heatOn: true,
              active: true,
              updatedAtMillis: Date.now()
            }, overrides || {});
            window.webkit.messageHandlers.updateSaunaSession.postMessage(payload);
            return { posted: true, channel: "updateSaunaSession", payload: payload };
          },
          endLiveActivity: function(overrides) {
            var payload = Object.assign({
              bookingId: 999001,
              reason: "debug",
              updatedAtMillis: Date.now()
            }, overrides || {});
            window.webkit.messageHandlers.endSaunaSession.postMessage(payload);
            return { posted: true, channel: "endSaunaSession", payload: payload };
          }
        };
        window.addEventListener("live-activity-status", function(event) {
          console.info("[SweatBox native] Live Activity status", event.detail);
        });
        window.addEventListener("live-activity-token", function(event) {
          console.info("[SweatBox native] Live Activity token", event.detail);
        });
        window.addEventListener("push-to-start-token", function(event) {
          console.info("[SweatBox native] Push-to-start token", event.detail);
        });
        """
        #else
        debugHelpers = ""
        #endif
        let source = """
        document.documentElement.classList.add("native-wrapper");
        (function() {
          if (document.getElementById("sweatbox-native-wrapper-style")) return;
          var style = document.createElement("style");
          style.id = "sweatbox-native-wrapper-style";
          style.textContent = `
            html.native-wrapper,
            html.native-wrapper body {
              background: #111827 !important;
            }
            html.native-wrapper .pwa-top-nav {
              display: none !important;
            }
            html.native-wrapper .pwa-bottom-nav {
              display: block !important;
            }
            html.native-wrapper .pwa-header {
              padding-top: calc(1rem + env(safe-area-inset-top)) !important;
            }
            html.native-wrapper .pwa-main {
              padding-bottom: calc(5rem + env(safe-area-inset-bottom)) !important;
            }
            html.native-wrapper .pwa-fab {
              bottom: calc(4.75rem + env(safe-area-inset-bottom)) !important;
            }
          `;
          (document.head || document.documentElement).appendChild(style);
        })();
        Object.defineProperty(window, "__nativeDeviceId", {
          value: \(encodedDeviceId),
          configurable: false,
          enumerable: false,
          writable: false
        });
        window.dispatchEvent(new CustomEvent("native-device-ready", { detail: { deviceId: \(encodedDeviceId) } }));
        \(debugHelpers)
        """
        return WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true)
    }

    private func dispatchNativeEvent<T: Encodable>(name: String, payload: T) {
        guard let webView else { return }

        let eventName = Self.jsonString(name)
        let detail = Self.jsonObject(payload)
        let script = """
        window.dispatchEvent(new CustomEvent(\(eventName), { detail: \(detail) }));
        """
        webView.evaluateJavaScript(script)
    }

    private func decode<T: Decodable>(_ type: T.Type, from body: Any) -> T? {
        guard JSONSerialization.isValidJSONObject(body),
              let data = try? JSONSerialization.data(withJSONObject: body) else {
            return nil
        }
        return try? JSONDecoder().decode(type, from: data)
    }

    private static func jsonString(_ value: String) -> String {
        let data = try? JSONEncoder().encode(value)
        return data.flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
    }

    private static func jsonObject<T: Encodable>(_ value: T) -> String {
        let encoder = JSONEncoder()
        guard let data = try? encoder.encode(value),
              let string = String(data: data, encoding: .utf8) else {
            return "{}"
        }
        return string
    }
}
