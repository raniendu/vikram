import Darwin
import Foundation

/// Where Vikram's browser may go, and the actions it accepts.
///
/// Only public web pages open, plus the blank and inline documents pages use
/// for frames: never local files, app schemes, or hosts on this Mac or its
/// networks. Every action is validated here before it reaches a page.
public enum BrowserPolicy {
    static let blockedSuffixes = [
        ".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".test",
    ]

    /// WebKit content blocker URL filters (no alternation): private and local
    /// hosts are blocked for every request a page makes, not only navigations.
    public static let privateFilters = [
        #"^[a-z]+://[^./:]+[:/]"#,
        #"^[a-z]+://[^/]*\.localhost[:/]"#,
        #"^[a-z]+://[^/]*\.local[:/]"#,
        #"^[a-z]+://[^/]*\.internal[:/]"#,
        #"^[a-z]+://[^/]*\.lan[:/]"#,
        #"^[a-z]+://[^/]*\.home[:/]"#,
        #"^[a-z]+://[^/]*\.home\.arpa[:/]"#,
        #"^[a-z]+://[^/]*\.test[:/]"#,
        #"^[a-z]+://0\."#,
        #"^[a-z]+://10\."#,
        #"^[a-z]+://127\."#,
        #"^[a-z]+://169\.254\."#,
        #"^[a-z]+://172\.1[6-9]\."#,
        #"^[a-z]+://172\.2[0-9]\."#,
        #"^[a-z]+://172\.3[01]\."#,
        #"^[a-z]+://192\.168\."#,
        #"^[a-z]+://100\.6[4-9]\."#,
        #"^[a-z]+://100\.[7-9][0-9]\."#,
        #"^[a-z]+://100\.1[01][0-9]\."#,
        #"^[a-z]+://100\.12[0-7]\."#,
        #"^[a-z]+://\["#,
    ]

    /// The content blocker that keeps pages from reaching private addresses.
    public static var privateRules: String {
        let rules: [JSONValue] = privateFilters.map {
            ["trigger": ["url-filter": .string($0)], "action": ["type": "block"]]
        }
        return String(decoding: (try? JSONValue.array(rules).encoded()) ?? Data(), as: UTF8.self)
    }

    /// Whether the browser may navigate to `url`.
    public static func allowed(_ url: URL) -> Bool {
        switch url.scheme?.lowercased() {
        case "about": ["about:blank", "about:srcdoc"].contains(url.absoluteString)
        case "blob", "data": true
        case "http", "https": publicHost(url)
        default: false
        }
    }

    static func publicHost(_ url: URL) -> Bool {
        if url.user?.isEmpty == false || url.password != nil { return false }
        guard var host = url.host(percentEncoded: false), !host.isEmpty else { return false }
        if host.hasPrefix("[") && host.hasSuffix("]") { host = String(host.dropFirst().dropLast()) }
        if host.contains(":") {
            guard let address = ipv6(host) else { return false }
            return globalV6(address)
        }
        let name = host.lowercased().trimmingSuffix(".")
        if endsInNumber(name) {
            // Browsers read these as IPv4 addresses, in any of their notations.
            guard let address = ipv4(name) else { return false }
            return globalV4(address)
        }
        return name.contains(".") && !blockedSuffixes.contains { name.hasSuffix($0) }
    }

    // MARK: Addresses, as the URL standard parses hosts

    static func endsInNumber(_ host: String) -> Bool {
        var parts = host.split(separator: ".", omittingEmptySubsequences: false)
        if parts.last == "" {
            if parts.count == 1 { return false }
            parts.removeLast()
        }
        guard let last = parts.last, !last.isEmpty else { return false }
        if last.allSatisfy(\.isASCIIDigit) { return true }
        return number(last) != nil
    }

    static func number(_ part: Substring) -> UInt64? {
        var text = part
        var radix = 10
        if text.count >= 2, text.lowercased().hasPrefix("0x") {
            text = text.dropFirst(2)
            radix = 16
        } else if text.count >= 2, text.hasPrefix("0") {
            text = text.dropFirst()
            radix = 8
        }
        if text.isEmpty { return 0 }
        guard text.count <= 32 else { return nil }
        return UInt64(text, radix: radix)
    }

    static func ipv4(_ host: String) -> [UInt8]? {
        var parts = host.split(separator: ".", omittingEmptySubsequences: false)
        if parts.last == "" { parts.removeLast() }
        guard !parts.isEmpty, parts.count <= 4 else { return nil }
        let numbers = parts.map(number)
        guard numbers.allSatisfy({ $0 != nil }) else { return nil }
        let values = numbers.map { $0! }
        guard values.dropLast().allSatisfy({ $0 <= 255 }) else { return nil }
        let limit = UInt64(1) << (8 * UInt64(5 - values.count))
        guard values.last! < limit else { return nil }
        var address = values.last!
        for (index, value) in values.dropLast().enumerated() {
            address += value << (8 * UInt64(3 - index))
        }
        return [24, 16, 8, 0].map { UInt8((address >> $0) & 0xff) }
    }

    static func ipv6(_ host: String) -> [UInt16]? {
        var address = in6_addr()
        guard inet_pton(AF_INET6, host, &address) == 1 else { return nil }
        return withUnsafeBytes(of: address) { bytes in
            stride(from: 0, to: 16, by: 2).map { UInt16(bytes[$0]) << 8 | UInt16(bytes[$0 + 1]) }
        }
    }

    static func globalV4(_ ip: [UInt8]) -> Bool {
        let (a, b, c) = (ip[0], ip[1], ip[2])
        let blocked =
            a == 0 || a == 10 || a == 127 || a >= 224
            || (a == 169 && b == 254)
            || (a == 172 && (16...31).contains(b))
            || (a == 192 && b == 168)
            || (a == 100 && (64..<128).contains(b))
            || (a == 198 && (b == 18 || b == 19))
            || (a == 192 && b == 0 && c == 2)
            || (a == 198 && b == 51 && c == 100)
            || (a == 203 && b == 0 && c == 113)
        return !blocked
    }

    static func globalV6(_ ip: [UInt16]) -> Bool {
        if ip[0..<5].allSatisfy({ $0 == 0 }) && ip[5] == 0xffff {
            return globalV4([
                UInt8(ip[6] >> 8), UInt8(ip[6] & 0xff), UInt8(ip[7] >> 8), UInt8(ip[7] & 0xff),
            ])
        }
        let loopback = ip[0..<7].allSatisfy { $0 == 0 } && ip[7] == 1
        let unspecified = ip.allSatisfy { $0 == 0 }
        let first = ip[0]
        return !(loopback || unspecified
            || first & 0xff00 == 0xff00
            || first & 0xfe00 == 0xfc00
            || first & 0xffc0 == 0xfe80
            || (first == 0x2001 && ip[1] == 0x0db8))
    }

    // MARK: Actions

    static let maxTyped = 2_000

    /// Parse one action call; anything malformed or unexpected is refused.
    public static func parse(_ call: JSONValue) throws -> (BrowserAction, screenshot: Bool) {
        let invalid = ServiceError("The browser received an invalid action.")
        guard let object = call.object else { throw invalid }
        func flag(_ key: String) throws -> Bool {
            switch object[key] {
            case nil: return false
            case .bool(let value)?: return value
            default: throw invalid
            }
        }
        func element() throws -> Int {
            guard let value = object["element"]?.number, value.rounded() == value,
                (1...100_000).contains(value)
            else { throw invalid }
            return Int(value)
        }
        let screenshot = try flag("screenshot")
        let action: BrowserAction
        switch object["action"]?.string {
        case "open":
            guard let text = object["url"]?.string, text.count <= 2048,
                let url = URL(string: text)
            else { throw invalid }
            guard ["http", "https"].contains(url.scheme?.lowercased()), allowed(url) else {
                throw ServiceError(
                    "That address is blocked: only public http and https pages open in Vikram's browser."
                )
            }
            action = .open(url)
        case "click":
            action = .click(element: try element(), approved: try flag("approved"))
        case "type":
            guard let text = object["text"]?.string, text.unicodeScalars.count <= maxTyped else {
                throw invalid
            }
            action = .type(
                element: try element(), text: text, submit: try flag("submit"),
                approved: try flag("approved"))
        case "scroll":
            switch object["direction"] {
            case nil, .string("down")?: action = .scroll(up: false)
            case .string("up")?: action = .scroll(up: true)
            default: throw invalid
            }
        case "back": action = .back
        case "look": action = .look
        default: throw invalid
        }
        return (action, screenshot)
    }
}

/// One validated action. Everything the engine runs comes from this, so a
/// malformed or unexpected call never reaches the page.
public enum BrowserAction: Sendable, Equatable {
    case open(URL)
    case click(element: Int, approved: Bool)
    case type(element: Int, text: String, submit: Bool, approved: Bool)
    case scroll(up: Bool)
    case back
    case look
}

extension String {
    func trimmingSuffix(_ suffix: String) -> String {
        hasSuffix(suffix) ? String(dropLast(suffix.count)) : self
    }

    /// At most `limit` Unicode scalars.
    func bounded(_ limit: Int) -> String {
        String(String.UnicodeScalarView(unicodeScalars.prefix(limit)))
    }
}

extension Character {
    var isASCIIDigit: Bool { isASCII && isNumber }
}
