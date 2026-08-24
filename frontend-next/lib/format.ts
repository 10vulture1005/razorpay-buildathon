import type { AuditEvent, Funnel } from "./api";

export const inr = (n: number): string =>
  "₹" + Math.round(n).toLocaleString("en-IN");

export const timeOf = (iso: string | null): string =>
  iso ? iso.slice(11, 19) : "--:--:--";

export type TapeClass = "approved" | "refused" | "";

/** Classification derives ONLY from structured payload fields — never from
 *  free text in descriptions or reasoning. */
export function eventTone(e: AuditEvent): TapeClass {
  if (e.event_type === "policy_check") {
    return e.payload && e.payload.allowed === false ? "refused" : "approved";
  }
  if (e.event_type === "mark_recovered" || e.event_type === "payment_detected") return "approved";
  return "";
}

export const isRejection = (e: AuditEvent): boolean =>
  e.event_type === "policy_check" && e.payload?.allowed === false;

export type FunnelSegment = { label: string; value: number; color: string; pct: number };

const SEGMENT_COLORS = ["#8a8f85", "#565b54", "#b9b6a9", "#1e6b4a", "#b23a30"];

/** Display proportions only; every figure shown is the server-computed value. */
export function funnelSegments(f: Funnel): FunnelSegment[] {
  const raw = [
    { label: `Revenue at risk · ${inr(f.revenue_at_risk)}`, value: f.revenue_at_risk },
    { label: `Eligible cases · ${f.eligible_cases}`, value: f.eligible_cases },
    { label: `Automated actions · ${f.automated_actions}`, value: f.automated_actions },
    { label: `Recovered · ${f.successful_recovery}`, value: f.successful_recovery },
    { label: `Escalated · ${f.human_escalation}`, value: f.human_escalation },
  ];
  return raw.map((r, i) => ({
    ...r,
    color: SEGMENT_COLORS[i],
    pct: Math.max(r.value, i === 0 ? 100 : 3),
  }));
}

export const statusClass = (s: string): string =>
  ["RECOVERED", "ESCALATED", "STOPPED"].includes(s) ? `status-${s}` : "status-other";

export const shortCaseId = (id: string): string =>
  id.replace(/^case_/, "").replace(/_/g, " ");
