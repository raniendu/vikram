import Foundation

private let words: [String: String] = [
    "ai": "AI", "awq": "AWQ", "chatgpt": "ChatGPT", "deepseek": "DeepSeek",
    "ggml": "GGML", "gguf": "GGUF", "gpt": "GPT", "gptq": "GPTQ", "it": "IT",
    "mlx": "MLX", "oss": "OSS", "qat": "QAT",
]

private let sizeOrQuant = try! NSRegularExpression(
    pattern: #"^(?:[ae]?\d+(?:\.\d+)?[bm]|(?:i?q|fp?|bf|r)\d[\w.]*)$"#,
    options: [.caseInsensitive])
private let familyNumber = try! NSRegularExpression(
    pattern: #"([a-z]{2,})(\d+(?:\.\d+)*)(?=$|[-_])"#, options: [.caseInsensitive])
private let separators = try! NSRegularExpression(pattern: #"[-_\s]+"#)

private func formatWord(_ word: String) -> String {
    if let known = words[word.lowercased()] { return known }
    let range = NSRange(word.startIndex..., in: word)
    if sizeOrQuant.firstMatch(in: word, range: range) != nil { return word.uppercased() }
    return word.prefix(1).uppercased() + word.dropFirst()
}

private func split(_ text: String, by pattern: NSRegularExpression) -> [String] {
    var parts: [String] = []
    var start = text.startIndex
    for match in pattern.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
        guard let range = Range(match.range, in: text) else { continue }
        parts.append(String(text[start..<range.lowerBound]))
        start = range.upperBound
    }
    parts.append(String(text[start...]))
    return parts
}

/// A model's readable name, such as "Gemma 4 · 26B A4B IT QAT".
///
/// Presentation only: selections, saved chats, and requests keep the Ollama ID.
public func formatModelName(_ id: String) -> String {
    let slash = id.lastIndex(of: "/")
    let namespace = slash.map { String(id[...$0]) } ?? ""
    let local = slash.map { String(id[id.index(after: $0)...]) } ?? id
    let colon = local.firstIndex(of: ":")
    let family = colon.map { String(local[..<$0]) } ?? local
    let tag = colon.map { String(local[local.index(after: $0)...]) } ?? ""
    let spaced = familyNumber.stringByReplacingMatches(
        in: family, range: NSRange(family.startIndex..., in: family), withTemplate: "$1 $2")
    let name = split(spaced, by: separators).map(formatWord).joined(separator: " ")
    let variant =
        tag.isEmpty ? "" : tag.split(separator: "-", omittingEmptySubsequences: false)
        .map { formatWord(String($0)) }.joined(separator: " ")
    return namespace + name + (variant.isEmpty ? "" : " · \(variant)")
}
