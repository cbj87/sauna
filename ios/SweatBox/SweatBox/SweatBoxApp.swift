//
//  SweatBoxApp.swift
//  SweatBox
//
//  Created by Cameron Jones on 6/29/26.
//

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
