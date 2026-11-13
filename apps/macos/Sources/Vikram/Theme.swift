import AppKit
import CoreText
import SwiftUI

/// Brand colors for content and accents. Window chrome uses system materials.
enum Theme {
    static let surface = dynamic(light: 0xF4F2EC, dark: 0x1C1A16)
    /// Raised surfaces that sit on paper: the composer, cards in dark appearance.
    static let raised = dynamic(light: 0xFFFFFF, dark: 0x1C1A16)
    static let ink = dynamic(light: 0x14130F, dark: 0xF4F2EC)
    static let muted = dynamic(light: 0x5C5952, dark: 0xA8A49A)
    static let rule = dynamic(light: 0xE3E0D8, dark: 0x2B2924)
    static let strongRule = dynamic(light: 0xCFCBC1, dark: 0x3A372F)
    static let accent = dynamic(light: 0xA0552D, dark: 0xD6995F)
    /// Accent-colored text and links on paper.
    static let link = dynamic(light: 0x8A4423, dark: 0xE3B184)
    static let success = dynamic(light: 0x4E7A3E, dark: 0x9BBF7E)
    static let danger = dynamic(light: 0xA63C2B, dark: 0xEF9B86)

    private static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(
            nsColor: NSColor(name: nil) { appearance in
                let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
                let value = isDark ? dark : light
                return NSColor(
                    srgbRed: CGFloat((value >> 16) & 0xFF) / 255,
                    green: CGFloat((value >> 8) & 0xFF) / 255,
                    blue: CGFloat(value & 0xFF) / 255, alpha: 1)
            })
    }

    // MARK: Type

    static let displayFamily = "Space Grotesk"

    /// Space Grotesk for titles and the person's own words; the system font
    /// when the bundled face is unavailable. Sizes scale with the text style.
    static func display(_ size: CGFloat, weight: Font.Weight = .medium, relativeTo style: Font.TextStyle = .title3)
        -> Font
    {
        registered
            ? .custom(displayFamily, size: size, relativeTo: style).weight(weight)
            : .system(size: size, weight: weight)
    }

    /// Register the bundled display face once, from the app's resources or,
    /// in a development build, from the package.
    static let registered: Bool = {
        let bundled = Bundle.main.url(
            forResource: "SpaceGrotesk", withExtension: "ttf", subdirectory: "Fonts")
        let package = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts/SpaceGrotesk.ttf")
        guard
            let url = [bundled, package].compactMap({ $0 }).first(where: {
                FileManager.default.fileExists(atPath: $0.path)
            })
        else { return false }
        var error: Unmanaged<CFError>?
        if CTFontManagerRegisterFontsForURL(url as CFURL, .process, &error) { return true }
        // Already registered by this process is fine.
        return NSFont(name: "SpaceGrotesk-Regular", size: 12) != nil
            || NSFontManager.shared.availableMembers(ofFontFamily: displayFamily) != nil
    }()
}

/// A small spaced-capitals label, as raniendu.dev marks sections: YOU, MODEL.
struct Eyebrow: View {
    let text: String
    var color: Color = Theme.muted

    init(_ text: String, color: Color = Theme.muted) {
        self.text = text
        self.color = color
    }

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(1.3)
            .foregroundStyle(color)
    }
}

/// The "v." mark.
struct BrandMark: View {
    var size: CGFloat = 15

    var body: some View {
        Text("v.")
            .font(Theme.display(size, weight: .semibold))
            .foregroundStyle(Theme.accent)
            .accessibilityHidden(true)
    }
}

/// A capsule that shows or hides details below it: thinking, web activity,
/// sandbox runs, plans, and browser steps.
struct ChipDisclosure<Label: View, Content: View>: View {
    @Binding var isExpanded: Bool
    @ViewBuilder var label: Label
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                    label
                }
                .font(.caption)
                .foregroundStyle(Theme.muted)
                .padding(.horizontal, 10)
                .frame(minHeight: 24)
                .overlay { Capsule().strokeBorder(Theme.rule) }
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
            if isExpanded {
                content.transition(.opacity)
            }
        }
    }
}
