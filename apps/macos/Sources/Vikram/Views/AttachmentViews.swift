import AppKit
import SwiftUI
import VikramKit

/// Thumbnails of a message's or draft's attachments.
struct AttachmentStrip: View {
    let attachments: [Attachment]
    var remove: ((Attachment) -> Void)?

    var body: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 8) {
                ForEach(attachments) { attachment in
                    AttachmentThumbnail(attachment: attachment, remove: remove)
                }
            }
        }
        .scrollIndicators(.never)
    }
}

private struct AttachmentThumbnail: View {
    @Environment(ChatController.self) private var chats
    let attachment: Attachment
    let remove: ((Attachment) -> Void)?
    @State private var image: NSImage?
    @State private var failed = false
    @State private var previewing = false

    var body: some View {
        Button {
            previewing = true
        } label: {
            Group {
                if attachment.isDocument {
                    VStack(spacing: 4) {
                        Image(systemName: "doc.text").font(.title2)
                        Text(attachment.name).font(.caption).lineLimit(1).truncationMode(.middle)
                        Text(
                            attachment.extractionNotice?.contains("Partial:") == true
                                ? "Partial text" : "Text ready"
                        )
                        .font(.caption2).foregroundStyle(Theme.muted)
                    }
                    .padding(6)
                } else if let image {
                    Image(nsImage: image).resizable().scaledToFill()
                } else if failed {
                    Image(systemName: "photo.badge.exclamationmark").font(.title2)
                } else {
                    ProgressView().controlSize(.small)
                }
            }
            .frame(width: attachment.isDocument ? 120 : 76, height: 76)
            .background(Theme.surface)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay { RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.rule) }
        }
        .buttonStyle(.plain)
        .help(attachment.name)
        .accessibilityLabel("View \(attachment.name)")
        .disabled(!attachment.isDocument && image == nil)
        .overlay(alignment: .topTrailing) {
            if let remove {
                Button {
                    remove(attachment)
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .symbolRenderingMode(.palette)
                        .foregroundStyle(.white, .black.opacity(0.6))
                }
                .buttonStyle(.plain)
                .offset(x: 5, y: -5)
                .accessibilityLabel("Remove \(attachment.name)")
            }
        }
        .task(id: attachment.id) {
            guard !attachment.isDocument, image == nil else { return }
            if let data = await chats.imageData(attachment), let loaded = NSImage(data: data) {
                image = loaded
            } else {
                failed = true
            }
        }
        .sheet(isPresented: $previewing) {
            AttachmentPreview(attachment: attachment, image: image)
        }
    }
}

private struct AttachmentPreview: View {
    @Environment(\.dismiss) private var dismiss
    let attachment: Attachment
    let image: NSImage?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(attachment.name).font(.headline)
            if attachment.isDocument {
                Text("Extracted text available to the model. \(attachment.extractionNotice ?? "")")
                    .font(.callout).foregroundStyle(Theme.muted)
                ScrollView {
                    Text(attachment.extractedText ?? "")
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(minHeight: 300)
            } else if let image {
                Image(nsImage: image).resizable().scaledToFit().frame(maxHeight: 520)
            }
            HStack {
                Text(
                    (attachment.isDocument
                        ? "" : "\(attachment.width ?? 0) × \(attachment.height ?? 0) · ")
                        + ByteCountFormatter.string(
                            fromByteCount: Int64(attachment.size), countStyle: .file)
                )
                .font(.caption).foregroundStyle(Theme.muted)
                Spacer()
                Button("Done") { dismiss() }.buttonStyle(.glassProminent).keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(minWidth: 480, idealWidth: 640, maxWidth: 900)
    }
}
