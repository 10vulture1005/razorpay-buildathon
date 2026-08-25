"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type Ticket } from "@/lib/api";
import { inr } from "@/lib/format";

const REASON_LABELS: Record<string, { label: string; why: string }> = {
  max_retries_exceeded: {
    label: "Retries exhausted",
    why: "The agent tried every allowed touch and got no payment. A human call usually lands better than a fourth email.",
  },
  dispute: {
    label: "Disputed invoice",
    why: "The customer claims something is wrong with the invoice. Humans must resolve disputes — the agent stands down.",
  },
  window_expired: {
    label: "Recovery window expired",
    why: "Too much time has passed since detection. Policy requires human review before any further outreach.",
  },
};

function behaviorLine(t: Ticket): string {
  const c = t.company;
  const bits: string[] = [];
  if (c.invoices_total !== null && c.invoices_total !== undefined) {
    bits.push(`${c.invoices_total} invoice${c.invoices_total === 1 ? "" : "s"} on record`);
  }
  if (c.on_time_rate !== null && c.on_time_rate !== undefined) {
    bits.push(`${Math.round(c.on_time_rate * 100)}% paid on time`);
  }
  if (c.avg_days_late !== null && c.avg_days_late !== undefined && c.avg_days_late > 0.5) {
    bits.push(`typically ~${Math.round(c.avg_days_late)}d late`);
  }
  if (c.broken_promise_count !== null && c.broken_promise_count !== undefined) {
    bits.push(
      c.broken_promise_count === 0
        ? "never broke a payment promise"
        : `broke ${c.broken_promise_count} payment promise${c.broken_promise_count === 1 ? "" : "s"}`,
    );
  }
  return bits.join(" · ") || "no history yet — first invoice";
}

export default function Tickets() {
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [down, setDown] = useState(false);
  const [resolving, setResolving] = useState<string | null>(null);
  const [showResolved, setShowResolved] = useState(false);
  const [query, setQuery] = useState("");

  const load = useCallback(() => {
    api
      .tickets(showResolved ? "all" : "open")
      .then((t) => setTickets(t))
      .catch(() => setDown(true));
  }, [showResolved]);

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  const filtered = (tickets ?? []).filter((t) =>
    query.trim() === ""
      ? true
      : t.ticket_id.toLowerCase().includes(query.trim().toLowerCase()),
  );

  const resolve = async (ticketId: string) => {
    setResolving(ticketId);
    try {
      await api.resolveTicket(ticketId);
      setTickets((prev) =>
        (prev ?? []).filter((t) => showResolved || t.ticket_id !== ticketId),
      );
    } catch {
      /* leave row; next poll refreshes */
    } finally {
      setResolving(null);
    }
  };

  return (
    <main className="tickets">
      <section className="sb-hero">
        <p className="kicker">human review queue · agent stood down</p>
        <h1>Escalation tickets</h1>
        <p className="lede">
          Cases the agent could not close on its own. Every card shows who you are
          about to contact and how they behave — the agent&apos;s full dossier — so
          the call starts informed.
        </p>
      </section>

      <div className="section-head">
        <h2>
          {showResolved ? "All tickets" : `${tickets?.length ?? 0} open`}
          {query.trim() !== "" && (
            <span className="tk-count-hint">
              {" "}· {filtered.length} match{filtered.length === 1 ? "" : "es"}
            </span>
          )}
        </h2>
        <div className="tk-controls">
          <input
            type="search"
            className="tk-search"
            placeholder="Search by ticket ID…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search by ticket ID"
          />
          <label className="resolved-toggle">
            <input
              type="checkbox"
              checked={showResolved}
              onChange={(e) => setShowResolved(e.target.checked)}
            />
            show resolved
          </label>
        </div>
      </div>

      {down && <p className="empty-state">API offline.</p>}
      {!down && tickets?.length === 0 && (
        <p className="empty-state">
          No {showResolved ? "" : "open "}tickets. When the agent exhausts its
          ladder or hits a dispute, it lands here.
        </p>
      )}
      {!down && tickets && tickets.length > 0 && filtered.length === 0 && (
        <p className="empty-state">
          No tickets match &ldquo;{query}&rdquo;.
        </p>
      )}

      {filtered.map((t) => {
        const meta = REASON_LABELS[t.reason] ?? {
          label: t.reason,
          why: "Policy required human review.",
        };
        const diagCause =
          (t.diagnosis as { likely_cause?: string } | null)?.likely_cause ?? "unknown";
        return (
          <article key={t.ticket_id} className="ticket-card">
            <header className="tk-head">
              <span className={`reason-badge reason-${t.reason}`}>{meta.label}</span>
              <span className="tk-id mono" title={t.ticket_id}>{t.ticket_id}</span>
              <span className="tk-amount mono">{inr(t.amount_at_risk)}</span>
              <span className="tk-overdue">{t.days_overdue}d overdue</span>
              {t.ticket_status === "resolved" && (
                <span className="status-pill status-RECOVERED">resolved</span>
              )}
            </header>

            <h3 className="tk-company">
              {t.company.name}
              {t.company.email && (
                <a className="tk-email" href={`mailto:${t.company.email}`}>
                  {t.company.email}
                </a>
              )}
              {t.company.opted_out && (
                <span className="status-pill status-other">opted out</span>
              )}
            </h3>
            <p className="tk-behavior">{behaviorLine(t)}</p>

            <div className="tk-body">
              <div className="tk-block">
                <h4>Why it escalated</h4>
                <p>{meta.why}</p>
              </div>
              <div className="tk-block">
                <h4>Agent&apos;s summary</h4>
                <p>&ldquo;{t.summary}&rdquo;</p>
                <p className="tk-meta">
                  diagnosed as <strong>{diagCause}</strong> · tried{" "}
                  {t.actions_tried.length > 0 ? t.actions_tried.join(" → ") : "nothing"}
                </p>
              </div>
            </div>

            <footer className="tk-actions">
              <Link className="btn small ghost" href="/dashboard">
                Full audit tape →
              </Link>
              {t.ticket_status === "open" && (
                <button
                  className="btn small primary"
                  disabled={resolving === t.ticket_id}
                  onClick={() => resolve(t.ticket_id)}
                >
                  {resolving === t.ticket_id ? "closing…" : "Mark handled"}
                </button>
              )}
            </footer>
          </article>
        );
      })}
    </main>
  );
}
