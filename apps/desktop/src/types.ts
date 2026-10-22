export type Thinking = boolean | string | null;
export type Mode = "normal" | "plan";
export interface QuestionOption {
  label: string;
  description?: string | null;
}
export interface Question {
  header: string;
  question: string;
  options: QuestionOption[];
  multi_select?: boolean;
}
export interface QuestionRecord {
  kind: "question";
  id: string;
  status: string;
  questions: Question[];
  answers: Record<string, string[]> | null;
}
export interface PlanRecord {
  kind: "plan";
  id: string;
  status: string;
  plan: string;
  feedback: string | null;
}
export interface CodeRecord {
  kind: "code";
  id: string;
  status: string;
  code: string;
  output: string;
  error: string | null;
}
export interface BrowserRecord {
  kind: "browser";
  id: string;
  action: "open" | "click" | "type" | "scroll" | "back" | "look";
  status: string;
  target: string | null;
  text: string | null;
  url: string | null;
  title: string | null;
  error: string | null;
  approval: { summary: string; reason: string } | null;
}
export type ActivityRecord =
  QuestionRecord | PlanRecord | CodeRecord | BrowserRecord;
/** The browser panel's page, reported by the app. */
export interface BrowserPage {
  open: boolean;
  url: string;
  title: string;
  loading: boolean;
}
/** Who drives the browser: Vikram, or the person who took it over. */
export type BrowserControl = "agent" | "person";
export interface QuestionAnswer {
  header: string;
  selected?: string[];
  custom_answer?: string;
}
export type Reply =
  | { answers: QuestionAnswer[] }
  | { cancelled: true }
  | { decision: "approve" | "dismiss" }
  | { decision: "revise"; feedback: string };
export interface LocalModel {
  name: string;
  available: boolean;
  reason: string | null;
  vision: boolean;
  can_think: boolean;
  tools?: boolean;
  thinking: (boolean | string)[];
  thinking_default?: boolean | string | null;
  size: number;
  context_length: number | null;
}
export interface Folder {
  id: string;
  name: string;
}
export interface Attachment {
  id: string;
  chat_id: string;
  message_id: string | null;
  name: string;
  media_type: string;
  width: number;
  height: number;
  size: number;
  kind?: "image" | "document";
  extracted_text?: string;
  extraction_notice?: string | null;
}
export type Status =
  "running" | "complete" | "failed" | "stopped" | "interrupted";
export interface Source {
  id: string;
  title: string;
  url: string;
  excerpt: string;
  retrieved_at: string;
}
export interface Search {
  tool: string;
  arguments: { search_queries?: string[]; urls?: string[]; objective?: string };
  status: string;
  error: string | null;
}
export interface Attempt {
  activity?: ActivityRecord[];
  mode?: Mode;
  reasoning?: string;
  reasoning_truncated?: boolean;
  revision?: number;
  sources?: Source[];
  searches?: Search[];
  web?: "auto" | "off";
  id: string;
  chat_id: string;
  message_id: string;
  request_id: string;
  requested_model: string;
  actual_model: string | null;
  thinking: Thinking;
  status: Status;
  content: string;
  error: string | null;
  unsaved?: boolean;
  phase?: string;
}
export interface Message {
  id: string;
  text: string;
  created: number;
  attempts: Attempt[];
}
export interface ChatSummary {
  id: string;
  title: string;
  mode?: Mode;
  folder_id: string | null;
  model: string | null;
  thinking: Record<string, Thinking>;
  draft: string;
  draft_revision: number;
  created: number;
  updated: number;
}
export interface Chat extends ChatSummary {
  messages: Message[];
  attachments: Attachment[];
}
export interface Active {
  chat_id: string;
  attempt_id: string;
  request_id: string;
}
export interface Snapshot {
  protocol?: number;
  chats: ChatSummary[];
  folders: Folder[];
  preferences: Record<string, unknown>;
  active: Active | null;
  models: LocalModel[];
  browser?: { control: BrowserControl } | null;
}
export interface RuntimeEvent extends Partial<Active> {
  control?: BrowserControl;
  activity?: ActivityRecord[];
  mode?: Mode;
  reasoning?: string;
  reasoning_truncated?: boolean;
  revision?: number;
  sources?: Source[];
  searches?: Search[];
  v: 1;
  event: string;
  status?: Status;
  content?: string;
  actual_model?: string | null;
  phase?: string;
  error?: string | null;
  unsaved?: boolean;
  message?: string;
}
