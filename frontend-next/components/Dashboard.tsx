"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type AuditEvent, type CaseSummary, type Funnel, type Metrics } from "@/lib/api";
import Charts from "@/components/Charts";
import {
  eventTone,
  funnelSegments,
  inr,
  isRejection,
  shortCaseId,
  statusClass,
  timeOf,
} from "@/lib/format";

const POLL_MS = 4000;

export default function Dashboard() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [feed, setFeed] = useState<AuditEvent[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [caseAudit, setCaseAudit] = useState<AuditEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [m, f, c, a] = await Promise.all([
        api.metrics(),
        api.funnel(),
        api.cases(),
        api.activity(80),
      ]);
      setMetrics(m);
      setFunnel(f);
      setCases(c);
      setFeed(a.events);
      setError(null);
      setStale(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error reaching the API");
      setStale(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (!selected) {
      setCaseAudit(null);
      return;
    }
    let alive = true;
    api
      .audit(selected)
      .then((r) => alive && setCaseAudit(r.events))
      .catch(() => alive && setCaseAudit([]));
    return () => {
      alive = false;
    };
  }, [selected]);

  const selectedCase = cases?.find((c) => c.case_id === selected) ?? null;
  const events: AuditEvent[] | null = selected ? caseAudit : feed;
  const loading = !metrics && !error;
  const emptyRepo = cases !== null && cases.length === 0;

  return (
    <>
      <header className="masthead sub">
        <span className="live" role="status">
          <span className={"live-dot" + (stale ? " stale" : "")} aria-hidden />
          {stale ? "reconnecting" : "live"}
        </span>
        <span className="spacer" />
        <span className="config-chip" title="Active policy limits (app/policy/policy_config.yaml)">
          retries ≤3 · window 7d · cap/day 1 · escalate ≥₹1L
        </span>
      </header>

      {error && (
        <div className="error-banner" role="alert">
          <span>Can't reach the API — showing last known data. ({error})</span>
          <button onClick={refresh}>Retry now</button>
        </div>
      )}

      {loading ? (
        <section aria-label="Loading metrics" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <div key={i} className="skeleton-row" style={{ width: `${90 - i * 18}%` }} />
          ))}
        </section>
      ) : (
        <>
          {metrics && (
            <section className="counters" aria-label="Money position">
              <div className="counter">
                <div className="label">At Risk</div>
                <div className="value">{inr(metrics.revenue_at_risk)}</div>
                <div className="sub">{metrics.total_cases} cases total</div>
              </div>
              <div className="counter">
                <div className="label">Recovered</div>
                <div className="value money-positive">{inr(metrics.recovered_amount)}</div>
                <div className="sub">{metrics.recovered_cases} cases closed</div>
              </div>
              <div className="counter">
                <div className="label">Recovery %</div>
                <div className="value">{(metrics.recovery_rate * 100).toFixed(1)}%</div>
                <div className="sub">of amount at risk</div>
              </div>
              <div className="counter">
                <div className="label">Active</div>
                <div className="value">{metrics.active_cases}</div>
                <div className="sub">{metrics.stopped_cases} stopped</div>
              </div>
              <div className="counter">
                <div className="label">Escalated</div>
                <div className="value">{metrics.escalated_cases}</div>
                <div className="sub">{(metrics.escalation_rate * 100).toFixed(0)}% of all cases</div>
              </div>
            </section>
          )}

          {funnel && !emptyRepo && (
            <section className="funnel-band" aria-label="Recovery funnel">
              <div className="funnel-bar">
                {funnelSegments(funnel).map((s) => (
                  <div
                    key={s.label}
                    className="funnel-seg"
                    style={{ flexGrow: s.pct, background: s.color }}
                    title={s.label}
                  />
                ))}
              </div>
              <div className="funnel-key">
                {funnelSegments(funnel).map((s) => (
                  <span className="k" key={s.label}>
                    <span className="swatch" style={{ background: s.color }} aria-hidden />
                    {s.label}
                  </span>
                ))}
              </div>
            </section>
          )}
        </>
      )}

      <Charts />

      <div className="main">
        <section className="tape-col">
          <div className="section-head">
            <h2>{selected ? `Case tape — ${shortCaseId(selected)}` : "Agent activity tape"}</h2>
            <span className="hint">
              {selected ? "full audit trail for this case" : "every event across every case"}
            </span>
          </div>

          {emptyRepo && (
            <div className="empty-state">
              No cases yet. Seed the database and run a batch:
              <br />
              <code>python -m scripts.run_full_batch --fresh</code>
              <br />
              The tape fills as the agent works.
            </div>
          )}
          {!emptyRepo && events !== null && events.length === 0 && (
            <div className="empty-state">No events on the tape yet — it fills within seconds of agent activity.</div>
          )}

          <div className="tape" aria-live="polite" aria-busy={events === null}>
            {events === null &&
              !emptyRepo &&
              [0, 1, 2, 3, 4].map((i) => <div key={i} className="skeleton-row" />)}
            {events?.map((e) => (
              <TapeRow key={e.seq} e={e} onSelect={setSelected} selected={selected} />
            ))}
          </div>
        </section>

        <aside className="rail">
          {selectedCase && (
            <div className="rail-block case-facts">
              <dl style={{ margin: 0 }}>
                <div className="fact-row">
                  <dt>Status</dt>
                  <dd><span className={`status-pill ${statusClass(selectedCase.status)}`}>{selectedCase.status}</span></dd>
                </div>
                <div className="fact-row"><dt>Amount at risk</dt><dd>{inr(selectedCase.amount_at_risk)}</dd></div>
                <div className="fact-row"><dt>Attempts</dt><dd>{selectedCase.attempt_count}</dd></div>
                <div className="fact-row"><dt>Last action</dt><dd>{selectedCase.last_action ?? "—"}</dd></div>
                {selectedCase.archetype && (
                  <div className="fact-row"><dt>Archetype</dt><dd>{selectedCase.archetype}</dd></div>
                )}
                {selectedCase.opted_out && (
                  <div className="fact-row"><dt>Opted out</dt><dd style={{ color: "var(--red)" }}>yes</dd></div>
                )}
              </dl>
            </div>
          )}
          <div className="rail-block">
            <div className="section-head" style={{ paddingLeft: 20, paddingRight: 12 }}>
              <h2>Cases</h2>
              <button
                className="hint"
                style={{ textDecoration: "underline", color: selected ? "var(--ink-2)" : "var(--ink-3)" }}
                onClick={() => setSelected(null)}
                disabled={!selected}
              >
                View all
              </button>
            </div>
            <ul className="case-list">
              {(cases ?? []).map((c) => (
                <li className="case-item" key={c.case_id}>
                  <button
                    aria-current={selected === c.case_id}
                    onClick={() => setSelected(c.case_id)}
                  >
                    <span>
                      {shortCaseId(c.case_id)}
                      {c.opted_out ? <span title="opted out — policy will refuse actions"> ⃠</span> : null}
                    </span>
                    <span className="amt">{inr(c.amount_at_risk)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </aside>
      </div>
    </>
  );
}

function TapeRow({
  e,
  onSelect,
  selected,
}: {
  e: AuditEvent;
  onSelect: (id: string) => void;
  selected: string | null;
}) {
  const caseId = typeof e.payload?.case_id === "string" ? e.payload.case_id : e.case_id ?? null;
  const tone = eventTone(e);
  return (
    <div className={"tape-row" + (isRejection(e) ? " rejection" : "")}>
      <span className="t">{timeOf(e.timestamp)}</span>
      <span className="case-ref">
        {caseId ? (
          <button
            className={"case-link" + (selected === caseId ? " active" : "")}
            onClick={() => onSelect(caseId)}
          >
            {shortCaseId(caseId)}
          </button>
        ) : (
          "—"
        )}
      </span>
      <span className={`actor-tag actor-${e.actor}`}>{e.actor}</span>
      <span className={`desc ${tone}`}>{e.description}</span>
      {e.agent_reasoning && (
        <span className="reasoning-sub">
          <b>agent's stated reasoning</b> (audit only, never decision input): {e.agent_reasoning}
        </span>
      )}
    </div>
  );
}
