import SwiftUI
import VikramKit

/// A bot's face: a soft orb of three blended colors with a friendly face on
/// it. Each bot keeps the orb it was given; one that never chose gets a stable
/// orb from its name.
struct BotIcon: View {
  let icon: String?
  let name: String
  var size: CGFloat = 22
  var paused = false
  @Environment(\.accessibilityReduceMotion) private var reduceMotion
  @State private var blink: CGFloat = 0
  @State private var look: CGSize = .zero
  @State private var breathing = false

  init(_ bot: Bot, size: CGFloat = 22) {
    icon = bot.icon
    name = bot.name
    self.size = size
    paused = !bot.enabled
  }

  init(icon: String?, name: String, size: CGFloat = 22) {
    self.icon = icon
    self.name = name
    self.size = size
  }

  var body: some View {
    let orb = BotOrb.resolve(icon, name: name)
    let c = orb.colors
    MeshGradient(
      width: 3, height: 3,
      points: [
        [0, 0], [0.5, 0], [1, 0],
        [0, 0.5], [0.62, 0.42], [1, 0.5],
        [0, 1], [0.5, 1], [1, 1],
      ],
      colors: [c[0], c[0], c[1], c[0], c[1], c[2], c[1], c[2], c[2]]
    )
    .overlay {
      // A highlight up and to the left gives the orb its depth.
      RadialGradient(
        colors: [.white.opacity(0.45), .clear], center: UnitPoint(x: 0.32, y: 0.28),
        startRadius: 0, endRadius: size * 0.55)
    }
    .clipShape(Circle())
    .overlay {
      BotFace(expression: orb.expression, light: orb.light, size: size, blink: blink, look: look)
    }
    .overlay { Circle().strokeBorder(.black.opacity(0.08), lineWidth: 0.5) }
    .scaleEffect(breathing ? 1.035 : 1)
    .frame(width: size, height: size)
    .saturation(paused ? 0 : 1)
    .opacity(paused ? 0.6 : 1)
    .accessibilityHidden(true)
    .task(id: paused || reduceMotion) { await live() }
  }

  /// Blink now and then, glance about, and breathe slowly. Each icon keeps its
  /// own random rhythm, so a list of bots never blinks in step. A paused bot,
  /// or Reduce Motion, keeps the face still.
  private func live() async {
    blink = 0
    look = .zero
    breathing = false
    guard !paused && !reduceMotion else { return }
    withAnimation(.easeInOut(duration: .random(in: 2.6...3.4)).repeatForever()) {
      breathing = true
    }
    while !Task.isCancelled {
      try? await Task.sleep(for: .seconds(.random(in: 2.5...6)))
      if Task.isCancelled { return }
      if Double.random(in: 0...1) < 0.35 {
        withAnimation(.easeInOut(duration: 0.35)) {
          look = CGSize(width: .random(in: -1...1), height: .random(in: -0.6...0.4))
        }
        try? await Task.sleep(for: .seconds(.random(in: 0.9...1.6)))
        withAnimation(.easeInOut(duration: 0.4)) { look = .zero }
      } else {
        withAnimation(.easeIn(duration: 0.08)) { blink = 1 }
        try? await Task.sleep(for: .milliseconds(110))
        withAnimation(.easeOut(duration: 0.12)) { blink = 0 }
        // Sometimes a quick double blink.
        if Double.random(in: 0...1) < 0.2 {
          try? await Task.sleep(for: .milliseconds(180))
          withAnimation(.easeIn(duration: 0.08)) { blink = 1 }
          try? await Task.sleep(for: .milliseconds(100))
          withAnimation(.easeOut(duration: 0.12)) { blink = 0 }
        }
      }
    }
  }
}

struct BotOrb: Identifiable, Hashable {
  let id: String
  let title: String
  let colors: [Color]
  let expression: BotFace.Expression
  /// Dark orbs get light features.
  var light = false

  static let all: [BotOrb] = [
    orb(1, "Ember", 0xF2783B, 0xF5B14C, 0xE0475B),
    orb(2, "Tide", 0x1E8FA6, 0x52C7B8, 0x2E5FD1),
    orb(3, "Dusk", 0x7A4CE0, 0xE06BB0, 0x3B3F9E),
    orb(4, "Moss", 0x4E9A3E, 0xB7D65A, 0x1F8A7A),
    orb(5, "Sky", 0x7DB8F2, 0xE6F1FB, 0x8C86E8),
    orb(6, "Rose", 0xE85D8A, 0xF7B39A, 0xC2304B),
    orb(7, "Citrus", 0xF2C230, 0xB9DB3C, 0xF28A2E),
    orb(8, "Ink", 0x23305E, 0x6B4FB8, 0x4A5A75, light: true),
    orb(9, "Lagoon", 0x22B8CF, 0x3CC77F, 0x2C6EDB),
    orb(10, "Clay", 0xA0552D, 0xE3B184, 0x6E3B22),
    orb(11, "Iris", 0xA99BF2, 0x6E8FF2, 0xF2A0D2),
    orb(12, "Graphite", 0x8A867E, 0xD6D2C8, 0x4A4740),
  ]

  /// The saved orb, or a stable one picked from the name.
  static func resolve(_ id: String?, name: String) -> BotOrb {
    if let id, let orb = all.first(where: { $0.id == id }) { return orb }
    // FNV-1a: stable across launches, unlike Hasher.
    var hash: UInt32 = 2_166_136_261
    for byte in name.lowercased().utf8 { hash = (hash ^ UInt32(byte)) &* 16_777_619 }
    return all[Int(hash % UInt32(all.count))]
  }

  private static func orb(
    _ index: Int, _ title: String, _ a: UInt32, _ b: UInt32, _ c: UInt32, light: Bool = false
  ) -> BotOrb {
    let expressions = BotFace.Expression.allCases
    return BotOrb(
      id: "orb-\(index)", title: title, colors: [a, b, c].map(color),
      expression: expressions[(index - 1) % expressions.count], light: light)
  }

  private static func color(_ value: UInt32) -> Color {
    Color(
      red: Double((value >> 16) & 0xFF) / 255, green: Double((value >> 8) & 0xFF) / 255,
      blue: Double(value & 0xFF) / 255)
  }
}

/// A small, friendly face: two eyes with a glint, and at larger sizes a mouth
/// and rosy cheeks. Nothing angular or staring. `blink` closes open eyes
/// (0 open, 1 shut); `look` shifts the face a little (-1...1 each way).
struct BotFace: View, Animatable {
  enum Expression: CaseIterable, Hashable {
    case happy, joyful, wink, curious, sleepy, content
  }

  let expression: Expression
  var light = false
  let size: CGFloat
  var blink: CGFloat = 0
  var look: CGSize = .zero

  nonisolated var animatableData: AnimatablePair<CGFloat, AnimatablePair<CGFloat, CGFloat>> {
    get { AnimatablePair(blink, AnimatablePair(look.width, look.height)) }
    set {
      blink = newValue.first
      look = CGSize(width: newValue.second.first, height: newValue.second.second)
    }
  }

  var body: some View {
    let feature = light ? Color(white: 0.97) : Color(red: 0.12, green: 0.1, blue: 0.09)
    let detailed = size >= 20
    Canvas { context, canvas in
      let s = canvas.width
      context.translateBy(x: look.width * s * 0.06, y: look.height * s * 0.05)
      let line = max(1, s * 0.045)
      let style = StrokeStyle(lineWidth: line, lineCap: .round, lineJoin: .round)
      let eyeY = s * 0.47
      let eyes = [s * 0.36, s * 0.64]

      func openEye(_ x: CGFloat) {
        let w = s * 0.12, h = s * 0.16 * (1 - 0.85 * blink)
        context.fill(
          Path(ellipseIn: CGRect(x: x - w / 2, y: eyeY - h / 2, width: w, height: h)),
          with: .color(feature))
        if detailed && blink < 0.4 {
          let r = s * 0.032
          context.fill(
            Path(ellipseIn: CGRect(x: x + w * 0.05, y: eyeY - h * 0.38, width: r * 2, height: r * 2)),
            with: .color(light ? Color(white: 0.2) : .white))
        }
      }
      // A smiling closed eye curves up (^); a sleepy one curves down.
      func closedEye(_ x: CGFloat, up: Bool) {
        var path = Path()
        let w = s * 0.14
        path.move(to: CGPoint(x: x - w / 2, y: eyeY + (up ? s * 0.03 : -s * 0.01)))
        path.addQuadCurve(
          to: CGPoint(x: x + w / 2, y: eyeY + (up ? s * 0.03 : -s * 0.01)),
          control: CGPoint(x: x, y: eyeY + (up ? -s * 0.07 : s * 0.07)))
        context.stroke(path, with: .color(feature), style: style)
      }

      switch expression {
      case .happy, .curious, .content: eyes.forEach(openEye)
      case .joyful: eyes.forEach { closedEye($0, up: true) }
      case .wink:
        openEye(eyes[0])
        closedEye(eyes[1], up: true)
      case .sleepy: eyes.forEach { closedEye($0, up: false) }
      }

      guard detailed else { return }
      let mouthY = s * 0.64
      switch expression {
      case .happy, .wink, .sleepy:
        var smile = Path()
        smile.move(to: CGPoint(x: s * 0.43, y: mouthY))
        smile.addQuadCurve(
          to: CGPoint(x: s * 0.57, y: mouthY), control: CGPoint(x: s * 0.5, y: mouthY + s * 0.07))
        context.stroke(smile, with: .color(feature), style: style)
      case .joyful:
        var open = Path()
        open.move(to: CGPoint(x: s * 0.42, y: mouthY - s * 0.01))
        open.addQuadCurve(
          to: CGPoint(x: s * 0.58, y: mouthY - s * 0.01),
          control: CGPoint(x: s * 0.5, y: mouthY + s * 0.13))
        open.closeSubpath()
        context.fill(open, with: .color(feature))
      case .curious:
        let r = s * 0.04
        context.stroke(
          Path(ellipseIn: CGRect(x: s * 0.5 - r, y: mouthY - r * 0.6, width: r * 2, height: r * 2.2)),
          with: .color(feature), style: StrokeStyle(lineWidth: line * 0.85))
      case .content: break
      }
      for x in [s * 0.25, s * 0.75] {
        context.fill(
          Path(ellipseIn: CGRect(x: x - s * 0.065, y: s * 0.6, width: s * 0.13, height: s * 0.07)),
          with: .color(Color(red: 1, green: 0.42, blue: 0.5).opacity(light ? 0.45 : 0.35)))
      }
    }
    .frame(width: size, height: size)
    .allowsHitTesting(false)
  }
}

/// A row of orbs to choose from; the bot's current one is ringed.
struct BotIconPicker: View {
  @Binding var icon: String?
  let name: String

  var body: some View {
    let current = BotOrb.resolve(icon, name: name).id
    HStack(spacing: 8) {
      ForEach(BotOrb.all) { orb in
        Button {
          icon = orb.id
        } label: {
          BotIcon(icon: orb.id, name: name, size: 26)
            .padding(3)
            .overlay {
              Circle().strokeBorder(
                orb.id == current ? Theme.accent : .clear, lineWidth: 2)
            }
        }
        .buttonStyle(.plain)
        .help(orb.title)
        .accessibilityLabel("\(orb.title) icon")
        .accessibilityAddTraits(orb.id == current ? .isSelected : [])
      }
    }
  }
}
