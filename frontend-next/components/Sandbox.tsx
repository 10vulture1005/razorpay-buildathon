"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type CaseSummary } from "@/lib/api";
import { inr } from "@/lib/format";

type Toast = { kind: "ok" | "err"; text: string };
type PayLink = { caseId: string; url: string; amount?: number };

const OPEN_STATUSES = new Set([
  "new",
  "diagnosed",
  "action_selected",
  "executing",
  "awaiting_outcome",
]);

function randomInvoiceId() {
  return `inv_${Math.random().toString(36).slice(2, 8)}`;
}

export default function Sandbox() {
  const [invoiceId, setInvoiceId] = useState(randomInvoiceId);
  const [customerId, setCustomerId] = useState("cust_acme_corp");
  const [companyName, setCompanyName] = useState("Acme Corp");
  const [amount, setAmount] = useState("25000");
  const [email, setEmail] = useState("finance@acme.example.com");
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [down, setDown] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [payLink, setPayLink] = useState<PayLink | null>(null);
  const [copied, setCopied] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flash = useCallback((kind: Toast["kind"], text: string) => {
    setToast({ kind, text });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 5000);
  }, []);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .cases()
        .then((c) => alive && (setCases(c), setDown(false)))
        .catch(() => alive && setDown(true));
    load();
    const id = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const fireOverdue = async (e: React.FormEvent) => {
    e.preventDefault();
    const amt = Number(amount);
    if (!invoiceId.trim() || !customerId.trim() || !(amt > 0)) {
      flash("err", "invoice id, customer id and a positive amount are required");
      return;
    }
    setBusy(true);
    try {
      const res = await api.reportOverdue({
        invoice_id: invoiceId.trim(),
        customer_id: customerId.trim(),
        amount: amt,
        customer_email: email.trim() || null,
        customer_name: companyName.trim() || null,
      });
      flash(
        "ok",
        res.duplicate
          ? `case already open for ${res.case_id} — idempotent, no duplicate created`
          : `case opened: ${res.case_id}`,
      );
      setInvoiceId(randomInvoiceId());
    } catch (err) {
      flash("err", err instanceof Error ? err.message : "request failed");
    } finally {
      setBusy(false);
    }
  };

  const runAgent = async (caseId: string) => {
    setRunning(caseId);
    try {
      const res = await api.runAgent(caseId);
      flash(
        "ok",
        `${caseId}: agent step done → ${res.status}${
          res.terminal_reason ? ` (${res.terminal_reason})` : ""
        }`,
      );
      try {
        const trail = await api.audit(caseId);
        const linkEvt = [...trail.events]
          .reverse()
          .find(
            (e) =>
              e.event_type === "send_payment_link" &&
              typeof e.payload?.short_url === "string",
          );
        if (linkEvt) {
          const caseRow = (cases ?? []).find((c) => c.case_id === caseId);
          setCopied(false);
          setPayLink({
            caseId,
            url: linkEvt.payload.short_url as string,
            amount: caseRow?.amount_at_risk,
          });
        }
      } catch {
        /* audit fetch is best-effort; the run result toast already fired */
      }
    } catch (err) {
      flash("err", err instanceof Error ? err.message : "agent run failed");
    } finally {
      setRunning(null);
    }
  };

  const simulatePayment = async (caseId: string, _invoiceIdOfCase: string) => {
    try {
      // Dev-only shortcut standing in for a real Razorpay webhook:
      // inserts the payment event the poller would see after a verified
      // payment_link.paid. In test mode with webhooks wired, pay the real
      // link instead and leave this button alone.
      await api.simulatePayment(caseId);
      flash("ok", `payment inserted for ${caseId} — watch the tape`);
    } catch (err) {
      try {
        await api.simulatePayment(caseId);
        flash("ok", `payment inserted for ${caseId} — watch the tape`);
      } catch (err) {
        flash(
          "err",
          err instanceof Error
            ? `${err.message} (simulate-payment needs an admin key + non-prod environment)`
            : "failed",
        );
      }
    }
  };

  const openCases = (cases ?? []).filter((c) =>
    OPEN_STATUSES.has(c.status.toLowerCase()),
  );

  return (
    <main className="sandbox">
      <section className="sb-hero">
        <p className="kicker">test rig · stands in for your billing system</p>
        <h1>Merchant sandbox</h1>
        <p className="lede">
          This page plays the role of your business site. Fire an invoice-overdue
          event exactly the way your production code would (
          <code>POST /events/invoice-overdue</code>), then watch the agent work the
          case on the dashboard tape. When Razorpay test keys are configured, the
          agent&apos;s payment links are real test-mode links you can actually pay.
        </p>
      </section>

      <div className="sb-grid">
        <form className="sb-card" onSubmit={fireOverdue}>
          <h2>1 · Fire an overdue invoice</h2>
          <label>
            Invoice ID
            <input value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)} required />
          </label>
          <label>
            Customer ID
            <input value={customerId} onChange={(e) => setCustomerId(e.target.value)} required />
          </label>
          <label>
            Company name <span className="opt">shown on tickets & reminders</span>
            <input value={companyName} onChange={(e) => setCompanyName(e.target.value)} />
          </label>
          <label>
            Amount (₹)
            <input
              type="number"
              min="1"
              step="0.01"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              required
            />
          </label>
          <label>
            Customer email <span className="opt">optional — needed for real reminders</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="finance@customer.example.com"
            />
          </label>
          <button className="btn primary" disabled={busy}>
            {busy ? "firing…" : "POST /events/invoice-overdue"}
          </button>
        </form>

        <div className="sb-card">
          <h2>2 · Wire it from real code</h2>
          <p className="sb-note">
            The only integration touchpoint your billing system needs:
          </p>
          <pre>{`curl -X POST $API_URL/events/invoice-overdue \\
  -H "x-api-key: $RUN_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "invoice_id": "inv_1042",
    "customer_id": "cust_acme",
    "amount": 25000,
    "customer_email": "finance@acme.com"
  }'`}</pre>
          <p className="sb-note">
            Then either let the cron worker (<code>scripts.run_due_cases</code>, every
            15 min) work the case, or hit “run agent” below to step it immediately.
          </p>
        </div>
      </div>

      <section className="sb-open">
        <div className="section-head">
          <h2>3 · Open cases</h2>
          <span className="hint">
            {down ? "API offline" : `${openCases.length} open · polling every 5s`}
          </span>
        </div>
        {openCases.length === 0 ? (
          <p className="empty-state">
            No open cases. Fire an overdue invoice above — or wait for the agent to
            finish the ones below.
          </p>
        ) : (
          <table className="sb-table">
            <thead>
              <tr>
                <th>Case</th>
                <th>Status</th>
                <th className="num">At risk</th>
                <th className="num">Attempts</th>
                <th>Last action</th>
                <th aria-label="actions"></th>
              </tr>
            </thead>
            <tbody>
              {openCases.map((c) => (
                <tr key={c.case_id}>
                  <td className="mono">{c.case_id}</td>
                  <td>
                    <span
                      className={`status-pill ${
                        c.status === "RECOVERED"
                          ? "status-RECOVERED"
                          : c.status === "ESCALATED"
                            ? "status-ESCALATED"
                            : c.status === "STOPPED"
                              ? "status-STOPPED"
                              : "status-other"
                      }`}
                    >
                      {c.status.toLowerCase()}
                    </span>
                  </td>
                  <td className="num mono">{inr(c.amount_at_risk)}</td>
                  <td className="num mono">{c.attempt_count}</td>
                  <td className="last-action">{c.last_action ?? "—"}</td>
                  <td className="row-actions">
                    <button
                      className="btn small"
                      disabled={running === c.case_id}
                      onClick={() => runAgent(c.case_id)}
                    >
                      {running === c.case_id ? "running…" : "Run agent"}
                    </button>
                    <button
                      className="btn small ghost"
                      onClick={() => simulatePayment(c.case_id, c.invoice_id)}
                      title="Dev-only: stand in for a verified Razorpay webhook"
                    >
                      Pay (simulate)
                    </button>
                    <Link className="btn small ghost" href={`/dashboard`}>
                      Tape
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="sb-footnote">
          “Pay (simulate)” is a dev-only double-gated endpoint that mimics what a
          verified <code>payment_link.paid</code> webhook does. With Razorpay test keys +
          webhook wired, skip it — pay the real link Razorpay emails out, and recovery
          confirms through the webhook alone.
        </p>
      </section>

      {toast && (
        <div className={"sb-toast " + toast.kind} role="status">
          {toast.text}
        </div>
      )}

      {payLink && (
        <div className="modal-overlay" role="dialog" aria-modal="true" onClick={() => setPayLink(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <p className="kicker">action required · {payLink.caseId}</p>
            <h2>Payment link is live</h2>
            <p className="modal-lede">
              The agent sent a real Razorpay test-mode link
              {typeof payLink.amount === "number" ? <> for {inr(payLink.amount)}</> : null}.
              Pay it with the test card to fire the{" "}
              <code>payment_link.paid</code> webhook and close the loop.
            </p>
            <div className="modal-url mono">{payLink.url}</div>
            <div className="modal-actions">
              <a className="btn primary" href={payLink.url} target="_blank" rel="noopener noreferrer">
                Open payment page ↗
              </a>
              <button
                className="btn ghost"
                onClick={() => {
                  navigator.clipboard?.writeText(payLink.url);
                  setCopied(true);
                }}
              >
                {copied ? "copied ✓" : "copy link"}
              </button>
              <button className="btn ghost" onClick={() => setPayLink(null)}>
                dismiss
              </button>
            </div>
            <p className="sb-footnote">
              Test card: 4111 1111 1111 1111 · any future expiry · any CVV
            </p>
          </div>
        </div>
      )}
    </main>
  );
}
