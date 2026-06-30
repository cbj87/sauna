import SwiftUI
import UIKit
import WebKit

struct WebViewContainer: UIViewRepresentable {
    let url: URL
    let allowedHost: String
    let bridge: WebBridge
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

    func updateUIView(_ webView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let allowedHost: String
        weak var bridge: WebBridge?
        let onExternalURL: (URL) -> Void
        let onLoadFinished: () -> Void
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
    }
}
