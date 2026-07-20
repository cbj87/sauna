//
//  SweatBoxApp.swift
//  SweatBox
//
//  Created by Cameron Jones on 6/29/26.
//

import Combine
import SwiftUI
import UIKit
import UserNotifications

@main
struct SweatBoxApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

/// Handles APNs device-token registration for regular alert notifications
/// (Live Activities use their own per-activity token via ActivityKit).
///
/// The device token is stashed in `RemoteNotificationTokenBroker` — WebBridge
/// reads it from there on init and posts it into the WebView, which uploads
/// it to /api/native/device-token using the authenticated session cookie.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound, .badge]
        ) { granted, error in
            if let error {
                NSLog("[SweatBox] Notification authorization failed: \(error)")
            }
            guard granted else { return }
            DispatchQueue.main.async {
                UIApplication.shared.registerForRemoteNotifications()
            }
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        RemoteNotificationTokenBroker.shared.updateToken(hex)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        NSLog("[SweatBox] APNs registration failed: \(error)")
    }

    // Present alerts even when the app is in the foreground — the LA covers the
    // in-session UI, so an alert popping up means something the user should see now.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound, .list])
    }

    // Tapping a notification deep-links into the web app. The server attaches a
    // `url` ride-along (e.g. "/?booking=12") to every alert payload.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if let urlString = response.notification.request.content.userInfo["url"] as? String,
           let resolved = URL(string: urlString, relativeTo: AppConfig.webAppURL)?.absoluteURL,
           resolved.host == AppConfig.webAppHost {
            DeepLinkBroker.shared.open(resolved)
        }
        completionHandler()
    }
}

/// Shared holder for a pending in-app navigation target. Universal Links,
/// the sweatbox:// scheme, and notification taps all funnel through here;
/// ContentView observes it and tells the WebView to load the URL.
final class DeepLinkBroker: ObservableObject {
    static let shared = DeepLinkBroker()

    @Published var pendingURL: URL?

    private init() {}

    func open(_ url: URL) {
        DispatchQueue.main.async {
            self.pendingURL = url
        }
    }

    /// Map a sweatbox:// URL onto the web app, e.g. sweatbox://rsvp/abc → https://…/rsvp/abc.
    /// "open" is the generic "bring the app to the web root" host (the Live
    /// Activity tap target) — it maps to "/", not to a "/open" path, which the
    /// server doesn't serve as a page.
    static func webURL(fromCustomScheme url: URL) -> URL? {
        guard url.scheme == AppConfig.urlScheme else { return nil }
        guard var comps = URLComponents(url: AppConfig.webAppURL, resolvingAgainstBaseURL: false) else { return nil }
        let hostSegment: String
        if let host = url.host, host != "open" {
            hostSegment = "/\(host)"
        } else {
            hostSegment = ""
        }
        let path = hostSegment + url.path
        comps.path = path.isEmpty ? "/" : path
        comps.query = url.query
        return comps.url
    }
}

/// Shared holder for the APNs device token. Publishes to observers (WebBridge)
/// whenever a new token arrives.
@MainActor
final class RemoteNotificationTokenBroker {
    static let shared = RemoteNotificationTokenBroker()

    var onToken: ((String) -> Void)?
    private(set) var cachedToken: String?

    private init() {}

    nonisolated func updateToken(_ token: String) {
        Task { @MainActor in
            self.cachedToken = token
            self.onToken?(token)
        }
    }
}
