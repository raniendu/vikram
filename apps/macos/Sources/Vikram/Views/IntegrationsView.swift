import SwiftUI
import VikramKit

struct IntegrationsView: View {
    @Environment(WorkbenchController.self) private var workbench
    @State private var serverName = ""
    @State private var configuration =
        "{\n  \"transport\": \"stdio\",\n  \"command\": \"\",\n  \"args\": []\n}"
    @State private var source = ""

    var body: some View {
        Form {
            Section("MCP Servers") {
                if workbench.status?.mcp.isEmpty != false {
                    Text("No MCP servers configured.").foregroundStyle(Theme.muted)
                }
                ForEach(workbench.status?.mcp ?? []) { server in
                    IntegrationRow(kind: "mcp", item: server)
                }
                DisclosureGroup("Add a Server") {
                    TextField("Server name", text: $serverName)
                    TextEditor(text: $configuration)
                        .font(.system(.caption, design: .monospaced))
                        .frame(height: 130)
                        .accessibilityLabel("MCP configuration JSON")
                    Text(
                        "Enter stdio or HTTP configuration as JSON. HTTP servers can use OAuth with Sign In; do not paste credentials into settings. Server tools require approval for unknown effects."
                    )
                    .font(.caption).foregroundStyle(Theme.muted)
                    Button("Add Server") { addServer() }
                        .disabled(
                            serverName.trimmingCharacters(in: .whitespaces).isEmpty || workbench.working)
                }
            }
            Section("Python Extensions") {
                if workbench.status?.extensions.isEmpty != false {
                    Text("No extensions installed.").foregroundStyle(Theme.muted)
                }
                ForEach(workbench.status?.extensions ?? []) { item in
                    IntegrationRow(kind: "extensions", item: item)
                }
                DisclosureGroup("Install an Extension") {
                    TextField("Local path, PyPI package, or Git source", text: $source)
                    Text(
                        "Packages run with your user privileges. Installation is explicit and may access the network. Install only sources you trust."
                    )
                    .font(.caption).foregroundStyle(Theme.muted)
                    Button("Install Package") {
                        let requested = source
                        Task {
                            if await workbench.perform(
                                "extensions",
                                options: [
                                    "operation": "install", "source": .string(requested),
                                ]) != nil
                            {
                                source = ""
                            }
                        }
                    }
                    .disabled(
                        source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || workbench.working)
                }
            }
        }
        .formStyle(.grouped)
    }

    private func addServer() {
        guard configuration.utf8.count <= 32768,
            let value = try? JSONDecoder().decode(JSONValue.self, from: Data(configuration.utf8)),
            let object = value.object
        else {
            workbench.error = "Enter a JSON object no larger than 32 KiB."
            return
        }
        Task {
            if await workbench.perform(
                "mcp",
                options: [
                    "operation": "add", "name": .string(serverName), "config": .object(object),
                ]) != nil
            {
                serverName = ""
            }
        }
    }
}

private struct IntegrationRow: View {
    @Environment(WorkbenchController.self) private var workbench
    let kind: String
    let item: IntegrationStatus
    @State private var removing = false
    @State private var controls = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.name).fontWeight(.medium)
                    Text(statusText).font(.caption).foregroundStyle(Theme.muted)
                }
                Spacer()
                Toggle(
                    "Enabled",
                    isOn: Binding(
                        get: { item.enabled ?? false },
                        set: { enabled in act(enabled ? "enable" : "disable") })
                )
                .toggleStyle(.switch).controlSize(.small)
            }
            if let source = item.source {
                Text(source).font(.caption.monospaced()).textSelection(.enabled)
            }
            if let error = item.error {
                Text(error).font(.caption).foregroundStyle(Theme.danger).textSelection(.enabled)
            }
            HStack {
                if kind == "mcp" {
                    Button("Reconnect") { act("reconnect") }
                    Button("Sign In…") { act("login") }
                    Button("Sign Out") { act("logout") }
                } else {
                    Button("Update") { act("update") }
                    Button("Settings & Commands…") { controls = true }
                }
                Button("Remove…", role: .destructive) { removing = true }
            }
            .controlSize(.small)
        }
        .disabled(workbench.working)
        .sheet(isPresented: $controls) { ExtensionControls(name: item.name) }
        .confirmationDialog("Remove \(item.name)?", isPresented: $removing, titleVisibility: .visible) {
            Button("Remove", role: .destructive) { act("remove") }
        } message: {
            Text(
                kind == "mcp"
                    ? "Remove this server configuration and disconnect its tools."
                    : "Remove this installed extension and its tools.")
        }
    }

    private var statusText: String {
        var text = item.status ?? (item.enabled == true ? "Enabled" : "Disabled")
        if let version = item.version { text += " · \(version)" }
        if let count = item.toolCount { text += " · \(count) tools" }
        return text
    }

    private func act(_ operation: String) {
        Task {
            await workbench.perform(
                kind, options: ["operation": .string(operation), "name": .string(item.name)])
        }
    }
}
