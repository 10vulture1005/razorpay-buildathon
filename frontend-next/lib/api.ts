export type Metrics = {
  revenue_at_risk: number;
  recovered_amount: number;
  recovery_rate: number;
  active_cases: number;
  total_cases: number;
  escalated_cases: number;
  stopped_cases: number;
  recovered_cases: number;
  automation_rate: number;
  escalation_rate: number;
};

export type Funnel = {
  revenue_at_risk: number;
  total_cases: number;
  eligible_cases: number;
  automated_actions: number;
  successful_recovery: number;
  human_escalation: number;
};

export type CaseSummary = {
  case_id: string;
  customer_id: string;
  invoice_id: string;
  status: string;
  amount_at_risk: number;
  attempt_count: number;
  last_action: string | null;
  archetype?: string | null;
  opted_out?: boolean;
};

export type AuditEvent = {
  seq: number;
  case_id: string | null;
  timestamp: string | null;
  actor: "agent" | "policy" | "system" | "human";
  event_type: string;
  description: string;
  payload: Record<string, unknown>;
  agent_reasoning: string | null;
};

export type Ticket = {
  ticket_id: string;
  case_id: string;
  reason: string;
  summary: string;
  ticket_status: string;
  created_at: string | null;
  amount_at_risk: number;
  days_overdue: number;
  actions_tried: string[];
  diagnosis: Record<string, unknown> | null;
  company: {
    name: string;
    email: string | null;
    opted_out: boolean;
    on_time_rate: number | null;
    avg_days_late: number | null;
    broken_promise_count: number | null;
    invoices_total: number | null;
  };
};

export type TimelineDay = {
  date: string;
  actions: number;
  rejections: number;
  recovered_amount: number;
};

export type Timeline = {
  days: TimelineDay[];
  payment_events_total: number;
};

export type ChartSeries = { name: string; data: number[] };

export type EmailDraft = { to: string; subject: string; body: string };

export type ChartSpec = {
  type: "bar" | "line" | "pie";
  title: string;
  unit: string | null;
  labels: string[];
  series: ChartSeries[];
};

export type ChatMessage = { role: "user" | "assistant"; content: string; chart?: ChartSpec | null };

export type RunResult = {
  case_id: string;
  status: string;
  terminal_reason: string | null;
  attempt_count: number;
};

export type OverdueEventResult = { case_id: string; duplicate: boolean };

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "/api";

async function errMessage(res: Response, path: string): Promise<string> {
  let detail = "";
  try {
    const body = await res.json();
    if (body?.detail) detail = typeof body.detail === "string" ? `: ${body.detail}` : "";
  } catch {
    // non-JSON error body — fall back to the status line only
  }
  return `API ${res.status} on ${path}${detail}`;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(await errMessage(res, path));
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errMessage(res, path));
  return res.json();
}

export const api = {
  metrics: () => get<Metrics>("/metrics/recovery"),
  funnel: () => get<Funnel>("/metrics/funnel"),
  cases: () => get<CaseSummary[]>("/cases"),
  activity: (limit = 60) => get<{ events: AuditEvent[] }>(`/metrics/activity?limit=${limit}`),
  audit: (caseId: string) => get<{ events: AuditEvent[] }>(`/cases/${caseId}/audit`),
  tickets: (status = "open") => get<Ticket[]>(`/tickets?status=${encodeURIComponent(status)}`),
  resolveTicket: (ticketId: string) => post<{ ticket_id: string; status: string }>(
    `/tickets/${ticketId}/resolve`, {},
  ),
  timeline: (days = 14) => get<Timeline>(`/metrics/timeline?days=${days}`),
  chat: (messages: ChatMessage[], caseId?: string | null) =>
    post<{
      answer: string;
      chart: ChartSpec | null;
      email_draft: EmailDraft | null;
    }>("/chat", {
      messages,
      case_id: caseId ?? null,
    }),
  sendEmail: (draft: EmailDraft, caseId?: string | null) =>
    post<{ status: string; to: string; provider: string; provider_message_id: string | null }>(
      "/chat/send-email",
      { ...draft, case_id: caseId ?? null },
    ),
  reportOverdue: (body: {
    invoice_id: string;
    customer_id: string;
    amount: number;
    customer_email?: string | null;
    customer_name?: string | null;
    days_overdue?: number;
  }) => post<OverdueEventResult>("/events/invoice-overdue", body),
  runAgent: (caseId: string) => post<RunResult>(`/agent/run/${caseId}`, {}),
  simulatePayment: (caseId: string) =>
    post<{ payment_event_id: number }>(`/cases/${caseId}/simulate-payment`, {}),
};
