import SwiftUI
import VikramKit

private let runStatus = [
    "running": "Running", "complete": "Complete", "failed": "Failed", "stopped": "Stopped",
    "interrupted": "Interrupted", "missed": "Missed",
]
private let triggers = [
    "schedule": "On schedule", "catch_up": "Caught up after sleep", "manual": "Run now",
]

private func stateText(_ task: ScheduledTask) -> String {
    switch task.state {
    case "paused": "Paused"
    case "done": "Done"
    default: "Next: \(when(task.nextRun))"
    }
}

/// The scheduled tasks: a list, one task with its runs, and one run as a turn.
struct SchedulesView: View {
    @Environment(AppState.self) private var state
    @Environment(ScheduleController.self) private var schedules
    @Environment(ChatController.self) private var chats
    @State private var editing: ScheduledTask?
    @State private var creating = false

    var body: some View {
        @Bindable var state = state
        NavigationStack(path: $state.schedulePath) {
            TaskList(new: { creating = true })
                .navigationDestination(for: ScheduleRoute.self) { route in
                    switch route {
                    case .task: TaskDetail(edit: { editing = $0 })
                    case .run: RunDetail()
                    }
                }
        }
        .onChange(of: state.schedulePath) { _, path in
            Task {
                switch path.last {
                case .task(let id)?: await schedules.openTask(id)
                case .run(let id)?:
                    if case .task(let task)? = path.dropLast().last, schedules.task?.id != task {
                        await schedules.openTask(task)
                    }
                    await schedules.openRun(id)
                case nil: await schedules.openTask(nil)
                }
            }
        }
        .sheet(isPresented: $creating) {
            TaskEditor(task: nil)
        }
        .sheet(item: $editing) { task in
            TaskEditor(task: task)
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            if let error = schedules.error {
                Banner(text: error) {
                    Button("Dismiss", systemImage: "xmark") { schedules.error = nil }
                        .labelStyle(.iconOnly).buttonStyle(.borderless)
                }
            }
        }
    }
}

private struct TaskList: View {
    @Environment(AppState.self) private var state
    @Environment(ScheduleController.self) private var schedules
    @Environment(ChatController.self) private var chats
    let new: () -> Void

    var body: some View {
        Group {
            if schedules.tasks.isEmpty {
                Welcome(
                    title: "Nothing scheduled yet.",
                    text:
                        "Ask in any chat, for example “every weekday at 8am, summarize the tech news” or “remind me tomorrow at 3pm to call the dentist”, or create a task here."
                ) {
                    Button("New Task", action: new).buttonStyle(.glassProminent).disabled(chats.offline)
                }
            } else {
                List {
                    if let note = backgroundNote {
                        Section { Text(note).font(.callout).foregroundStyle(Theme.muted) }
                    }
                    Section {
                        ForEach(schedules.tasks) { task in
                            NavigationLink(value: ScheduleRoute.task(task.id)) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(task.name).font(Theme.display(16, weight: .medium, relativeTo: .headline))
                                    Text(task.description).foregroundStyle(Theme.muted)
                                    Text(
                                        stateText(task)
                                            + (task.lastRun.map {
                                                " · last \((runStatus[$0.status] ?? $0.status).lowercased())"
                                            } ?? "")
                                    )
                                    .font(.caption)
                                    .foregroundStyle(task.state == "paused" ? Theme.accent : Theme.muted)
                                }
                                .padding(.vertical, 3)
                            }
                        }
                    }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .navigationTitle("Scheduled Tasks")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("New Task", systemImage: "plus", action: new)
                    .disabled(chats.offline)
                    .help("Create a scheduled task")
            }
        }
    }

    private var backgroundNote: String? {
        switch schedules.background {
        case "on":
            "Tasks run on this Mac at their times, even when Vikram is closed. Questions are skipped and the browser is not used."
        case "unavailable":
            "Background runs need the installed Vikram app. In development, run `uv run vikram schedules --run-due`."
        case "failed":
            "Vikram could not start its background service. Quit and reopen Vikram, then try again."
        default: nil
        }
    }
}

private struct TaskDetail: View {
    @Environment(AppState.self) private var state
    @Environment(ScheduleController.self) private var schedules
    @Environment(ChatController.self) private var chats
    let edit: (ScheduledTask) -> Void
    @State private var deleting = false
    @State private var converting = false

    var body: some View {
        if let task = schedules.task {
            let running = task.runs?.contains { $0.status == "running" } ?? false
            let disabled = chats.offline || schedules.working
            List {
                Section {
                    LabeledContent("Schedule", value: task.description)
                    LabeledContent("Status", value: stateText(task))
                    LabeledContent(
                        "Model", value: task.model.map(formatModelName) ?? "No model chosen")
                    LabeledContent(
                        "Created",
                        value: task.origin == "chat"
                            ? "In a chat" : task.origin == "cli" ? "In the terminal" : "In the app")
                }
                Section {
                    Text(task.prompt)
                        .font(Theme.display(16, weight: .regular, relativeTo: .body))
                        .textSelection(.enabled)
                } header: {
                    Eyebrow("Each run asks")
                }
                Section {
                    if let runs = task.runs, !runs.isEmpty {
                        ForEach(runs) { run in
                            NavigationLink(value: ScheduleRoute.run(run.id)) {
                                RunRow(run: run)
                            }
                            .disabled(run.status == "missed")
                        }
                    } else {
                        Text("No runs yet.").foregroundStyle(Theme.muted)
                    }
                } header: {
                    Eyebrow("Runs")
                }
            }
            .scrollContentBackground(.hidden)
            .navigationTitle(task.name)
            .toolbar {
                ToolbarItemGroup(placement: .primaryAction) {
                    Button(task.runRequested ? "Starting…" : "Run Now", systemImage: "play") {
                        Task { await schedules.runNow(task.id) }
                    }
                    .disabled(disabled || running || task.runRequested)
                    Button(
                        task.enabled ? "Pause" : "Resume",
                        systemImage: task.enabled ? "pause" : "play.circle"
                    ) {
                        Task { await schedules.setPaused(task.id, task.enabled) }
                    }
                    .disabled(disabled)
                    Button("Edit", systemImage: "pencil") { edit(task) }.disabled(disabled)
                    Button("Convert to Bot", systemImage: "sparkles") { converting = true }
                        .disabled(disabled || running)
                    Button("Delete", systemImage: "trash", role: .destructive) { deleting = true }
                        .disabled(disabled || running)
                }
            }
            .confirmationDialog("Convert “\(task.name)” to a bot?", isPresented: $converting) {
                Button("Convert and Pause") {
                    Task {
                        if let bot = await state.bots.convertSchedule(task.id) {
                            await schedules.openTask(nil)
                            try? await schedules.refresh()
                            state.openBot(bot.id)
                        }
                    }
                }
            } message: {
                Text("Its history is preserved. Review its responsibility, sources, and policy before resuming. No cloud permission is carried over.")
            }
            .confirmationDialog(
                "Delete “\(task.name)”?", isPresented: $deleting, titleVisibility: .visible
            ) {
                Button("Delete Task", role: .destructive) {
                    Task {
                        if await schedules.remove(task.id) { state.schedulePath = [] }
                    }
                }
            } message: {
                Text(
                    "The task and its run history will be permanently removed. Chats you continued from its runs stay."
                )
            }
        } else {
            ProgressView()
        }
    }
}

private struct RunRow: View {
    let run: RunSummary

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            HStack(spacing: 4) {
                if run.status == "running" { ActivityDot() }
                Text(runStatus[run.status] ?? run.status)
            }
            .frame(width: 100, alignment: .leading)
            .foregroundStyle(run.status == "failed" || run.status == "missed" ? Theme.danger : Theme.ink)
            Text(when(run.started)).monospacedDigit()
            Text(triggers[run.trigger] ?? "").foregroundStyle(Theme.muted)
            Text(run.status == "missed" ? run.error ?? "" : run.preview ?? run.error ?? "")
                .foregroundStyle(Theme.muted).lineLimit(1)
        }
    }
}

private struct RunDetail: View {
    @Environment(AppState.self) private var state
    @Environment(ScheduleController.self) private var schedules
    @Environment(ChatController.self) private var chats

    var body: some View {
        if let run = schedules.run {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Eyebrow("\(triggers[run.summary.trigger] ?? "Run") · \(when(run.summary.started))")
                    UserMessage(text: run.prompt, attachments: [], label: "Task prompt")
                    AnswerView(attempt: run.attempt, openTask: { state.openTask($0) })
                }
                .frame(maxWidth: 700)
                .padding(28)
                .frame(maxWidth: .infinity)
            }
            .navigationTitle("\(schedules.task?.name ?? "Run") · \(when(run.summary.started))")
            .toolbar {
                if run.summary.status != "running" {
                    ToolbarItem(placement: .primaryAction) {
                        Button("Continue in a Chat", systemImage: "bubble.left.and.text.bubble.right") {
                            Task {
                                if let chat = await schedules.continueInChat(run.id) {
                                    state.showsScheduled = false
                                    await chats.adopt(chat)
                                }
                            }
                        }
                        .disabled(chats.offline || schedules.working)
                    }
                }
            }
        } else {
            ProgressView()
        }
    }
}

/// Create or edit a scheduled task.
struct TaskEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(ScheduleController.self) private var schedules
    @Environment(ChatController.self) private var chats
    let task: ScheduledTask?

    @State private var name = ""
    @State private var prompt = ""
    @State private var repeatKind = "weekly"
    @State private var days: Set<String> = ["mon", "tue", "wed", "thu", "fri"]
    @State private var time = TaskEditor.date(hour: 8, minute: 0)
    @State private var date = Calendar.current.date(byAdding: .day, value: 1, to: .now) ?? .now
    @State private var dayOfMonth = 1
    @State private var every = 1
    @State private var hours = true
    @State private var model = ""
    @State private var loaded = false

    private static let weekdays = [
        ("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"), ("fri", "Fri"),
        ("sat", "Sat"), ("sun", "Sun"),
    ]

    private var available: [LocalModel] { chats.models.filter(\.available) }

    private var minutes: Int { hours ? every * 60 : every }

    private var schedule: ScheduleTime {
        let clock = Calendar.current.dateComponents([.hour, .minute], from: time)
        let hhmm = String(format: "%02d:%02d", clock.hour ?? 0, clock.minute ?? 0)
        let day = Calendar.current.dateComponents([.year, .month, .day], from: date)
        let ymd = String(format: "%04d-%02d-%02d", day.year ?? 2000, day.month ?? 1, day.day ?? 1)
        return ScheduleTime(
            repeatKind: repeatKind, time: hhmm, date: ymd,
            days: Self.weekdays.map(\.0).filter(days.contains), dayOfMonth: dayOfMonth,
            everyMinutes: minutes)
    }

    private var valid: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty
            && !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !model.isEmpty
            && (repeatKind != "interval" || minutes >= 15)
            && (repeatKind != "weekly" || !days.isEmpty)
    }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                TextField("Name", text: $name, prompt: Text("Morning tech news"))
                TextField(
                    "What should Vikram do each time?", text: $prompt,
                    prompt: Text("Summarize today's top technology news in five bullet points."),
                    axis: .vertical
                )
                .lineLimit(3...8)
                Picker("Repeat", selection: $repeatKind) {
                    Text("Once").tag("once")
                    Text("Every day").tag("daily")
                    Text("On chosen days").tag("weekly")
                    Text("Every month").tag("monthly")
                    Text("At an interval").tag("interval")
                }
                if repeatKind == "weekly" {
                    LabeledContent("Days") {
                        HStack(spacing: 4) {
                            ForEach(Self.weekdays, id: \.0) { day, label in
                                Toggle(
                                    label,
                                    isOn: Binding(
                                        get: { days.contains(day) },
                                        set: { on in
                                            if on { days.insert(day) } else { days.remove(day) }
                                        })
                                )
                                .toggleStyle(.button)
                            }
                        }
                    }
                }
                if repeatKind == "once" {
                    DatePicker("Date", selection: $date, displayedComponents: .date)
                }
                if repeatKind == "monthly" {
                    Stepper("Day of the month: \(dayOfMonth)", value: $dayOfMonth, in: 1...31)
                }
                if repeatKind == "interval" {
                    LabeledContent("Every") {
                        HStack {
                            TextField("Every", value: $every, format: .number)
                                .labelsHidden()
                                .frame(width: 60)
                            Picker("Unit", selection: $hours) {
                                Text("minutes").tag(false)
                                Text("hours").tag(true)
                            }
                            .labelsHidden()
                            .fixedSize()
                        }
                    }
                    if minutes < 15 {
                        Text("Run at most every 15 minutes.").font(.caption).foregroundStyle(Theme.danger)
                    }
                } else {
                    DatePicker("Time", selection: $time, displayedComponents: .hourAndMinute)
                }
                Picker("Model", selection: $model) {
                    if model.isEmpty { Text("Select a local model").tag("") }
                    if !model.isEmpty && !available.contains(where: { $0.name == model }) {
                        Text("\(formatModelName(model)) · unavailable").tag(model)
                    }
                    ForEach(available) { Text(formatModelName($0.name)).tag($0.name) }
                }
                Section {
                    Text(
                        "Runs happen on this Mac, even when Vikram is closed. Nobody is there to answer questions, so Vikram decides and says what it chose. Web search and the Python sandbox work; the browser is not used."
                    )
                    .font(.callout)
                    .foregroundStyle(Theme.muted)
                }
            }
            .formStyle(.grouped)
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .buttonStyle(.glass)
                    .keyboardShortcut(.cancelAction)
                Button(task == nil ? "Create Task" : "Save") {
                    Task {
                        let saved = await schedules.save(
                            name: name.trimmingCharacters(in: .whitespaces),
                            prompt: prompt.trimmingCharacters(in: .whitespacesAndNewlines),
                            schedule: schedule, model: model, taskId: task?.id)
                        if saved { dismiss() }
                    }
                }
                .buttonStyle(.glassProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!valid || schedules.working)
            }
            .padding()
        }
        .frame(width: 520)
        .navigationTitle(task == nil ? "New Scheduled Task" : "Edit Scheduled Task")
        .onAppear(perform: load)
    }

    private static func date(hour: Int, minute: Int) -> Date {
        Calendar.current.date(bySettingHour: hour, minute: minute, second: 0, of: .now) ?? .now
    }

    private func load() {
        guard !loaded else { return }
        loaded = true
        model = task?.model ?? chats.chat?.model ?? chats.lastModel ?? available.first?.name ?? ""
        guard let task else { return }
        name = task.name
        prompt = task.prompt
        let s = task.schedule
        repeatKind = s.repeatKind
        if let list = s.days { days = Set(list) }
        if let text = s.time {
            let parts = text.split(separator: ":").compactMap { Int($0) }
            if parts.count == 2 { time = Self.date(hour: parts[0], minute: parts[1]) }
        }
        if let text = s.date {
            let parts = text.split(separator: "-").compactMap { Int($0) }
            if parts.count == 3,
                let day = Calendar.current.date(
                    from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
            {
                date = day
            }
        }
        dayOfMonth = s.dayOfMonth ?? 1
        let interval = s.everyMinutes ?? 60
        hours = interval % 60 == 0
        every = hours ? interval / 60 : interval
    }
}
