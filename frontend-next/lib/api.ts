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

export type ChatMessage = { role: "user" | "assistant"; content: string };

export type RunResult = {
  case_id: string;
  status: string;
  terminal_reason: string | null;
  attempt_count: number;
};

export type OverdueEventResult = { case_id: string; duplicate: boolean };

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${res.status} on ${path}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${res.status} on ${path}`);
  return res.json();
}

export const api = {
  metrics: () => get<Metrics>("/metrics/recovery"),
  funnel: () => get<Funnel>("/metrics/funnel"),
  cases: () => get<CaseSummary[]>("/cases"),
  activity: (limit = 60) => get<{ events: AuditEvent[] }>(`/metrics/activity?limit=${limit}`),
  audit: (caseId: string) => get<{ events: AuditEvent[] }>(`/cases/${caseId}/audit`),
  timeline: (days = 14) => get<Timeline>(`/metrics/timeline?days=${days}`),
  chat: (messages: ChatMessage[], caseId?: string | null) =>
    post<{ answer: string }>("/chat", { messages, case_id: caseId ?? null }),
  reportOverdue: (body: {
    invoice_id: string;
    customer_id: string;
    amount: number;
    customer_email?: string | null;
  }) => post<OverdueEventResult>("/events/invoice-overdue", body),
  runAgent: (caseId: string) => post<RunResult>(`/agent/run/${caseId}`, {}),
  simulatePayment: (caseId: string) =>
    post<{ payment_event_id: number }>(`/cases/${caseId}/simulate-payment`, {}),
};
