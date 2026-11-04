import Foundation
import Testing

@testable import VikramKit

private func chat(_ attempt: Attempt, draft: String = "", draftRevision: Int = 0) -> Chat {
    Chat(
        summary: ChatSummary(id: "c1", title: "Chat", draft: draft, draftRevision: draftRevision),
        messages: [Message(id: "m1", text: "Hi", attempts: [attempt])])
}

private func attempt(
    status: String = "running", content: String = "", revision: Int = 1, request: String = "q1"
) -> Attempt {
    Attempt(
        id: "a1", chatId: "c1", messageId: "m1", requestId: request, requestedModel: "m",
        status: status, content: content, revision: revision)
}

private func event(
    status: String = "running", content: String? = nil, revision: Int = 2, chat: String = "c1",
    request: String = "q1", unsaved: Bool? = nil, activity: [ActivityRecord]? = nil
) -> RuntimeEvent {
    RuntimeEvent(
        event: "attempt", chatId: chat, attemptId: "a1", requestId: request, status: status,
        content: content, revision: revision, unsaved: unsaved, activity: activity)
}

@Suite struct AttemptRoutingTests {
    @Test func rejectsChunksAfterStop() {
        let stopped = chat(attempt(status: "stopped", content: "Partial"))
        let late = applyAttempt(stopped, event(content: "Partial and more", revision: 9))
        #expect(late.messages[0].attempts[0].content == "Partial")
        #expect(late.messages[0].attempts[0].status == "stopped")
    }

    @Test func rejectsChunksForAnotherChatOrRequest() {
        let running = chat(attempt())
        #expect(applyAttempt(running, event(content: "x", chat: "c2")) == running)
        #expect(applyAttempt(running, event(content: "x", request: "q2")) == running)
    }

    @Test func rejectsOlderRevisions() {
        let running = chat(attempt(content: "New", revision: 5))
        #expect(applyAttempt(running, event(content: "Old", revision: 4)).messages[0].attempts[0].content == "New")
    }

    @Test func keepsActivityWhenLaterChunksOmitIt() {
        let code = ActivityRecord.code(
            CodeRecord(id: "r1", status: "complete", code: "1+1", output: "2", error: nil))
        var running = applyAttempt(chat(attempt()), event(revision: 2, activity: [code]))
        running = applyAttempt(running, event(content: "Two", revision: 3))
        #expect(running.messages[0].attempts[0].activity == [code])
        #expect(running.messages[0].attempts[0].content == "Two")
    }

    @Test func endedRepliesAreNotActive() {
        let active = Active(chatId: "c1", attemptId: "a1", requestId: "q1")
        #expect(reconcileActive(active, [:]) == active)
        #expect(reconcileActive(active, ["a1": event(status: "complete")]) == nil)
        #expect(reconcileActive(active, ["a1": event(status: "running")]) == active)
    }
}

@Suite struct RecoveryTests {
    @Test func prefersNewerSavedTextOverAnOlderOverlay() {
        let saved = chat(attempt(status: "complete", content: "Final", revision: 8))
        let recovered = recoverChat(saved, ["a1": event(content: "Older", revision: 5, unsaved: true)])
        #expect(recovered.messages[0].attempts[0].content == "Final")
    }

    @Test func preservesGenuinelyUnsavedNewerOutput() {
        let saved = chat(attempt(status: "running", content: "Saved", revision: 3))
        let recovered = recoverChat(
            saved, ["a1": event(content: "Saved and unsaved", revision: 6, unsaved: true)])
        let result = recovered.messages[0].attempts[0]
        #expect(result.content == "Saved and unsaved")
        #expect(result.status == "interrupted")
        #expect(result.unsaved == true)
        #expect(result.revision == 6)
    }

    @Test func doesNotRecreateADraftWhoseSendWentThrough() {
        let saved = chat(attempt(request: "sent"), draft: "", draftRevision: 4)
        let draft = DraftRecord(text: "Hello", revision: 3)
        let recovered = recoverDraft(
            saved, draft, Submission(requestId: "sent", text: "Hello", revision: 3))
        #expect(recovered == DraftRecord(text: "", revision: 5))
    }

    @Test func keepsADraftTypedAfterTheSend() {
        let saved = chat(attempt(request: "sent"), draft: "", draftRevision: 4)
        let recovered = recoverDraft(
            saved, DraftRecord(text: "Hello again", revision: 5),
            Submission(requestId: "sent", text: "Hello", revision: 3))
        #expect(recovered == DraftRecord(text: "Hello again", revision: 6))
    }

    @Test func keepsTheDraftWhenTheSendNeverArrived() {
        let saved = chat(attempt(request: "other"), draft: "", draftRevision: 2)
        let recovered = recoverDraft(
            saved, DraftRecord(text: "Hello", revision: 3),
            Submission(requestId: "lost", text: "Hello", revision: 3))
        #expect(recovered.text == "Hello")
    }
}

@Suite struct ThinkingDefaultTests {
    @Test func isOnUnlessTheModelOnlyAdvertisesNamedLevels() {
        #expect(thinkingOn(LocalModel(name: "a", thinking: [.bool(true), .bool(false)])) == .bool(true))
        #expect(
            thinkingOn(
                LocalModel(
                    name: "b", thinking: [.level("low"), .level("high")],
                    thinkingDefault: .level("high"))) == .level("high"))
        #expect(thinkingOn(LocalModel(name: "c", thinking: [.level("low"), .level("high")])) == .level("low"))
        #expect(thinkingOn(LocalModel(name: "d")) == nil)
    }
}

@Suite struct ProtocolDecodingTests {
    @Test func decodesAChatAndItsActivity() throws {
        let json = """
            {"id":"c1","title":"Plans","mode":"plan","folder_id":null,"model":"gemma4:26b",
             "thinking":{"gemma4:26b":null,"qwen3":"high"},"draft":"","draft_revision":2,
             "created":1,"updated":2,
             "attachments":[{"id":"f1","chat_id":"c1","message_id":null,"name":"a.pdf",
               "media_type":"application/pdf","width":0,"height":0,"size":10,"kind":"document",
               "extracted_text":"Hello","extraction_notice":null}],
             "messages":[{"id":"m1","text":"Hi","created":1,"attempts":[{"id":"a1",
               "chat_id":"c1","message_id":"m1","request_id":"q1","requested_model":"gemma4:26b",
               "actual_model":null,"thinking":true,"status":"complete","content":"Hello",
               "error":null,"activity":[
                 {"kind":"question","id":"x","status":"answered","questions":[{"header":"H",
                   "question":"Q?","options":[{"label":"A"}],"multi_select":false}],
                   "answers":{"H":["A"]}},
                 {"kind":"future","id":"y","status":"complete"}]}]}]}
            """
        let chat = try JSONDecoder().decode(Chat.self, from: Data(json.utf8))
        #expect(chat.mode == .plan)
        #expect(chat.summary.thinking["qwen3"] == .some(.level("high")))
        #expect(chat.summary.thinking["gemma4:26b"] == .some(nil))
        #expect(chat.attachments[0].isDocument)
        let activity = try #require(chat.messages[0].attempts[0].activity)
        guard case .question(let question) = activity[0] else {
            Issue.record("Expected a question")
            return
        }
        #expect(question.answers?["H"] == ["A"])
        #expect(activity[1].kind == "future")
    }

    @Test func scheduleFieldsKeepOnlyWhatTheirKindUses() {
        let weekly = ScheduleTime(
            repeatKind: "weekly", time: "08:00", date: "2026-01-01", days: ["mon"],
            dayOfMonth: 3, everyMinutes: 60)
        #expect(weekly.fields == ["repeat": "weekly", "days": ["mon"], "time": "08:00"])
        let interval = ScheduleTime(repeatKind: "interval", time: "08:00", everyMinutes: 90)
        #expect(interval.fields == ["repeat": "interval", "every_minutes": 90])
    }

    @Test func wholeNumbersStayIntegersOnTheWire() throws {
        let text = String(decoding: try (["revision": 3, "ratio": .number(0.5)] as JSONValue).encoded(), as: UTF8.self)
        #expect(text.contains("\"revision\":3"))
        #expect(text.contains("\"ratio\":0.5"))
    }
}

@Suite struct ModelNameTests {
    @Test(arguments: [
        ("gemma4:26b-a4b-it-qat", "Gemma 4 · 26B A4B IT QAT"),
        ("llama3.2", "Llama 3.2"),
        ("qwen3:8b", "Qwen 3 · 8B"),
        ("hf.co/unsloth/gpt-oss-20b:q4_k_m", "hf.co/unsloth/GPT OSS 20B · Q4_K_M"),
        ("deepseek-r1:7b", "DeepSeek R1 · 7B"),
    ])
    func readableNames(_ id: String, _ expected: String) {
        #expect(formatModelName(id) == expected)
    }
}
