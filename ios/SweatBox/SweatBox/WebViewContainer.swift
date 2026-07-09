import SwiftUI
import UIKit
import WebKit

struct WebViewContainer: UIViewRepresentable {
    let url: URL
    let allowedHost: String
    let bridge: WebBridge
    var deepLinkURL: URL? = nil
    var onExternalURL: (URL) -> Void
    var onLoadFinished: () -> Void = {}

    func makeCoordinator() -> Coordinator {
        Coordinator(
            allowedHost: allowedHost,
            bridge: bridge,
            onExternalURL: onExternalURL,
            onLoadFinished: onLoadFinished
        )
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.allowsInlineMediaPlayback = true
        config.applicationNameForUserAgent = AppConfig.userAgentSuffix
        bridge.configure(config)

        let webView = WKWebView(frame: .zero, configuration: config)
        bridge.attach(webView: webView)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true

        // Lock the scroll view to 1.0 — the PWA viewport handles scaling,
        // and native pinch zoom on top makes text jump to weird sizes.
        webView.scrollView.minimumZoomScale = 1.0
        webView.scrollView.maximumZoomScale = 1.0
        webView.scrollView.bouncesZoom = false

        #if DEBUG
        webView.isInspectable = true
        #endif

        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        // Deep link arrived (Universal Link, sweatbox:// scheme, or a
        // notification tap) — navigate the already-loaded web app to it.
        if let target = deepLinkURL, context.coordinator.lastDeepLinkURL != target {
            context.coordinator.lastDeepLinkURL = target
            webView.load(URLRequest(url: target))
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let allowedHost: String
        weak var bridge: WebBridge?
        let onExternalURL: (URL) -> Void
        let onLoadFinished: () -> Void
        var lastDeepLinkURL: URL?
        private var hasFinishedFirstLoad = false

        init(
            allowedHost: String,
            bridge: WebBridge,
            onExternalURL: @escaping (URL) -> Void,
            onLoadFinished: @escaping () -> Void
        ) {
            self.allowedHost = allowedHost
            self.bridge = bridge
            self.onExternalURL = onExternalURL
            self.onLoadFinished = onLoadFinished
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            if !hasFinishedFirstLoad {
                hasFinishedFirstLoad = true
                onLoadFinished()
            }
            // Replay any cached Live Activity / push-to-start tokens. The page
            // may have reloaded after the original token arrived from APNs, in
            // which case the JS listener missed the dispatch — repost here so
            // the freshly-loaded listeners can upload them to the server.
            bridge?.repostCachedTokens()
        }

        // If WebContent crashes (low memory, etc.) reload so the user
        // doesn't end up staring at a blank webview.
        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            if let url = webView.url {
                webView.load(URLRequest(url: url))
            } else {
                webView.reload()
            }
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.allow)
                return
            }
            if url.scheme == "about" || url.scheme == "data" || url.scheme == "blob" {
                decisionHandler(.allow)
                return
            }
            if url.host == allowedHost {
                decisionHandler(.allow)
                return
            }
            if url.scheme == "http" || url.scheme == "https" {
                onExternalURL(url)
                decisionHandler(.cancel)
                return
            }
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            guard let url = navigationAction.request.url else { return nil }
            if url.host == allowedHost {
                webView.load(navigationAction.request)
            } else {
                onExternalURL(url)
            }
            return nil
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping () -> Void
        ) {
            presentJavaScriptDialog(
                title: webView.url?.host ?? "Sweat Box",
                message: message,
                actions: [UIAlertAction(title: "OK", style: .default) { _ in completionHandler() }],
                fallback: completionHandler
            )
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (Bool) -> Void
        ) {
            presentJavaScriptDialog(
                title: webView.url?.host ?? "Sweat Box",
                message: message,
                actions: [
                    UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) },
                    UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) },
                ],
                fallback: { completionHandler(false) }
            )
        }

        private func presentJavaScriptDialog(
            title: String,
            message: String,
            actions: [UIAlertAction],
            fallback: @escaping () -> Void
        ) {
            guard let presenter = UIApplication.shared.connectedScenes
                .compactMap({ $0 as? UIWindowScene })
                .flatMap({ $0.windows })
                .first(where: { $0.isKeyWindow })?
                .rootViewController
            else {
                fallback()
                return
            }

            let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
            actions.forEach(alert.addAction)
            presenter.present(alert, animated: true)
        }
    }
}
