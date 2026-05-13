// MAGI Apple Translation sidecar
//
// Usage:
//   magi_translator_sidecar <source_lang> <target_lang>
//   stdin:  UTF-8 text to translate
//   stdout: UTF-8 translated text
//   exit codes:
//     0  success
//     2  usage error
//     3  empty/invalid stdin
//     4  translation runtime error
//    10  language pack not installed (supported but needs download)
//    11  language pair unsupported on this macOS
//    12  availability check returned unknown status
//
// Languages use BCP-47 codes: "zh-Hant", "zh-Hans", "en", "ja", "ko", "fr", "de", "es", "it", "pt", "ru", "ar", "th", "vi", etc.

import SwiftUI
import Translation
import Foundation
import AppKit

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write(Data("usage: magi_translator_sidecar <source_lang> <target_lang>\n".utf8))
    exit(2)
}
let sourceLang = args[1]
let targetLang = args[2]

let inputData = FileHandle.standardInput.readDataToEndOfFile()
guard let inputText = String(data: inputData, encoding: .utf8),
      !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
    FileHandle.standardError.write(Data("empty_or_invalid_input\n".utf8))
    exit(3)
}

let sourceLanguage = Locale.Language(identifier: sourceLang)
let targetLanguage = Locale.Language(identifier: targetLang)

struct TranslatorView: View {
    let text: String
    let sourceLanguage: Locale.Language
    let targetLanguage: Locale.Language
    @State private var configuration: TranslationSession.Configuration?

    var body: some View {
        Color.clear
            .frame(width: 1, height: 1)
            .task {
                let availability = LanguageAvailability()
                let status = await availability.status(from: sourceLanguage, to: targetLanguage)
                switch status {
                case .installed:
                    configuration = TranslationSession.Configuration(
                        source: sourceLanguage,
                        target: targetLanguage
                    )
                case .supported:
                    FileHandle.standardError.write(Data("language_pack_not_installed\n".utf8))
                    exit(10)
                case .unsupported:
                    FileHandle.standardError.write(Data("language_pair_unsupported\n".utf8))
                    exit(11)
                @unknown default:
                    FileHandle.standardError.write(Data("unknown_availability\n".utf8))
                    exit(12)
                }
            }
            .translationTask(configuration) { session in
                do {
                    let response = try await session.translate(text)
                    FileHandle.standardOutput.write(Data(response.targetText.utf8))
                    exit(0)
                } catch {
                    let msg = "translation_error: \(error.localizedDescription)\n"
                    FileHandle.standardError.write(Data(msg.utf8))
                    exit(4)
                }
            }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let view = TranslatorView(
    text: inputText,
    sourceLanguage: sourceLanguage,
    targetLanguage: targetLanguage
)
let hostingView = NSHostingView(rootView: view)
let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: 1, height: 1),
    styleMask: [.borderless],
    backing: .buffered,
    defer: false
)
window.contentView = hostingView
window.alphaValue = 0
window.orderFrontRegardless()

// Hard timeout safety net (15s): exit 5 if translationTask never fires.
DispatchQueue.global().asyncAfter(deadline: .now() + 15.0) {
    FileHandle.standardError.write(Data("sidecar_timeout\n".utf8))
    exit(5)
}

app.run()
