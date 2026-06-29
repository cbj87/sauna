import Foundation

enum AppConfig {
    enum Environment {
        case production
        case test
        case local(port: Int)
    }

    static let environment: Environment = .production
    static let userAgentSuffix = "SweatBoxNative/1.0"
    static let urlScheme = "sweatbox"

    static var webAppURL: URL {
        switch environment {
        case .production:
            return URL(string: "https://sweatbox.cbj87.dev")!
        case .test:
            return URL(string: "https://sweatbox-test.cbj87.dev")!
        case .local(let port):
            return URL(string: "http://localhost:\(port)")!
        }
    }

    static var webAppHost: String {
        webAppURL.host ?? "sweatbox.cbj87.dev"
    }
}
