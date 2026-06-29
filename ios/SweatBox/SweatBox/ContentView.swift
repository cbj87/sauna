import SwiftUI

struct ContentView: View {
    @StateObject private var bridge = WebBridge()
    @State private var externalURL: IdentifiableURL?
    @State private var loaded = false

    var body: some View {
        ZStack {
            WebViewContainer(
                url: AppConfig.webAppURL,
                allowedHost: AppConfig.webAppHost,
                bridge: bridge,
                onExternalURL: { externalURL = IdentifiableURL(url: $0) },
                onLoadFinished: {
                    withAnimation(.easeOut(duration: 0.2)) {
                        loaded = true
                    }
                }
            )
            .ignoresSafeArea()

            if !loaded {
                SplashView()
                    .transition(.opacity)
            }
        }
        .sheet(item: $externalURL) { item in
            SafariView(url: item.url).ignoresSafeArea()
        }
    }
}

private struct IdentifiableURL: Identifiable {
    let id = UUID()
    let url: URL
}

private struct SplashView: View {
    var body: some View {
        ZStack {
            Color(.systemBackground).ignoresSafeArea()
            VStack(spacing: 14) {
                Text("Sweat Box")
                    .font(.largeTitle.bold())
                ProgressView()
                    .progressViewStyle(.circular)
            }
        }
    }
}

#Preview {
    ContentView()
}
