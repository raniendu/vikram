import SwiftUI
import VikramKit

/// Versioned extension results are inert native views, never executable markup.
struct ExtensionDeclarations: View {
    let values: [JSONValue]

    var body: some View {
        ForEach(Array(values.prefix(20).enumerated()), id: \.offset) { _, value in
            switch value["type"]?.string {
            case "text", "notice":
                Text(value["text"]?.string ?? "").textSelection(.enabled)
            case "progress":
                ProgressView(
                    value["label"]?.string ?? "Progress",
                    value: value["value"]?.number ?? 0,
                    total: max(1, value["total"]?.number ?? 1))
            case "table":
                VStack(alignment: .leading, spacing: 4) {
                    if let title = value["title"]?.string { Text(title).fontWeight(.medium) }
                    Text((value["columns"]?.array ?? []).map(display).joined(separator: "  ·  "))
                        .fontWeight(.medium)
                    ForEach(Array((value["rows"]?.array ?? []).prefix(100).enumerated()), id: \.offset) { _, row in
                        Text((row.array ?? []).map(display).joined(separator: "  ·  "))
                    }
                }
                .font(.caption.monospaced()).textSelection(.enabled)
            default:
                EmptyView()
            }
        }
    }

    private func display(_ value: JSONValue) -> String {
        value.string ?? ((try? value.encoded()).map { String(decoding: $0, as: UTF8.self) } ?? "")
    }
}

struct ExtensionControls: View {
    @Environment(WorkbenchController.self) private var workbench
    let name: String
    @State private var settings: [String: JSONValue] = [:]
    @State private var commands: [JSONValue] = []
    @State private var selected = ""
    @State private var arguments = "{}"
    @State private var result: JSONValue?
    @State private var values: [String: String] = [:]

    var body: some View {
        Form {
            Section("Settings") {
                ForEach(settings.keys.sorted(), id: \.self) { key in
                    HStack {
                        TextField(key + " (JSON)", text: Binding(
                            get: { values[key] ?? "null" }, set: { values[key] = $0 }))
                        Button("Save") { save(key) }
                    }
                }
                if settings.isEmpty { Text("No settings registered.") }
            }
            Section("Commands") {
                Picker("Command", selection: $selected) {
                    Text("Choose a command").tag("")
                    ForEach(commands, id: \.self) { command in
                        Text(command["name"]?.string ?? "").tag(command["name"]?.string ?? "")
                    }
                }
                TextEditor(text: $arguments).font(.system(.caption, design: .monospaced))
                    .frame(height: 110).accessibilityLabel("Command arguments as JSON")
                Button("Run Command") { run() }.disabled(selected.isEmpty || workbench.working)
                if let output = result?["output"]?.string { Text(output).textSelection(.enabled) }
                if let error = result?["error"]?.string { Text(error).foregroundStyle(Theme.danger) }
                ExtensionDeclarations(values: result?["ui"]?.array ?? [])
            }
            if let error = workbench.error { Text(error).foregroundStyle(Theme.danger) }
        }
        .formStyle(.grouped).frame(minWidth: 500, minHeight: 400)
        .task {
            if let result = await workbench.perform("extensions", options: ["operation": "settings", "name": .string(name)]) {
                settings = result.object ?? [:]
                values = settings.mapValues { value in
                    (try? (value["value"] ?? .null).encoded()).map { String(decoding: $0, as: UTF8.self) } ?? "null"
                }
            }
            if let result = await workbench.perform("extension_commands") {
                commands = (result.array ?? []).filter { $0["name"]?.string?.hasPrefix("ext__\(name)__") == true }
            }
        }
    }

    private func save(_ key: String) {
        guard let parsed = try? JSONDecoder().decode(JSONValue.self, from: Data((values[key] ?? "null").utf8)) else {
            workbench.error = "Enter a valid JSON value."; return
        }
        Task { await workbench.perform("extensions", options: ["operation": "setting", "name": .string(name), "key": .string(key), "value": parsed]) }
    }

    private func run() {
        guard let parsed = try? JSONDecoder().decode(JSONValue.self, from: Data(arguments.utf8)), parsed.object != nil else {
            workbench.error = "Enter a JSON object for command arguments."; return
        }
        Task { result = await workbench.perform("extension_command", value: selected, options: ["arguments": parsed]) }
    }
}
