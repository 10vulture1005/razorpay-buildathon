"use client";

import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useEffect, useState } from "react";
import { api, type CaseSummary, type Timeline } from "@/lib/api";

const AXIS = { fontSize: 11, fill: "var(--ink-3)", fontFamily: "var(--mono)" };

export default function Charts() {
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [cases, setCases] = useState<CaseSummary[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      Promise.all([api.timeline(14), api.cases()])
        .then(([t, c]) => alive && (setTimeline(t), setCases(c)))
        .catch(() => {});
    load();
    const id = setInterval(load, 8000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!timeline || !cases) return null;

  const statuses = ["RECOVERED", "AWAITING_OUTCOME", "DIAGNOSED", "NEW", "ACTION_SELECTED", "ESCALATED", "STOPPED"];
  const statusCounts = statuses
    .map((s) => ({ status: s, count: cases.filter((c) => c.status === s).length }))
    .filter((s) => s.count > 0);
  const maxStatus = Math.max(...statusCounts.map((s) => s.count), 1);

  return (
    <section className="charts-band" aria-label="Recovery analytics">
      <div className="chart-block">
        <div className="section-head">
          <h2>Recovery over time — last 14 days</h2>
          <span className="hint">verified recovered ₹ · automated actions · policy rejections</span>
        </div>
        <div className="chart-frame">
          <ResponsiveContainer width="100%" height={220}>
            <ComposedChart data={timeline.days} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
              <CartesianGrid stroke="var(--rule)" strokeDasharray="2 4" vertical={false} />
              <XAxis dataKey="date" tick={AXIS} tickFormatter={(d: string) => d.slice(5)} tickLine={false} />
              <YAxis yAxisId="l" tick={AXIS} tickLine={false} width={44} />
              <YAxis yAxisId="r" orientation="right" tick={AXIS} tickLine={false} width={30} allowDecimals={false} />
              <Tooltip
                contentStyle={{
                  background: "var(--panel)",
                  border: "1px solid var(--rule-strong)",
                  fontFamily: "var(--mono)",
                  fontSize: 12,
                }}
                formatter={(value, name) =>
                  name === "recovered ₹"
                    ? [Number(value ?? 0).toLocaleString("en-IN"), String(name)]
                    : [String(value ?? ""), String(name)]
                }
              />
              <Legend wrapperStyle={{ fontSize: 11, fontFamily: "var(--mono)" }} />
              <Bar yAxisId="r" dataKey="actions" name="actions" fill="var(--rule-strong)" barSize={10} />
              <Bar yAxisId="r" dataKey="rejections" name="policy rejections" fill="var(--red)" barSize={10} />
              <Line
                yAxisId="l"
                type="monotone"
                dataKey="recovered_amount"
                name="recovered ₹"
                stroke="var(--green)"
                strokeWidth={2}
                dot={{ r: 2.5, fill: "var(--green)" }}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
        <p className="foot-note">
          Rejections are a feature: policy blocking the agent is the safety system working.
          {timeline.payment_events_total > 0 &&
            ` ${timeline.payment_events_total} verified payment event(s) on the ledger.`}
        </p>
      </div>

      <div className="chart-block">
        <div className="section-head">
          <h2>Case states</h2>
          <span className="hint">{cases.length} cases on the book</span>
        </div>
        <ul className="state-bars">
          {statusCounts.map((s) => (
            <li key={s.status}>
              <span className="state-label">{s.status.replace(/_/g, " ").toLowerCase()}</span>
              <span className="state-track">
                <span
                  className={"state-fill tone-" + s.status.toLowerCase()}
                  style={{ width: `${(s.count / maxStatus) * 100}%` }}
                />
              </span>
              <span className="state-count">{s.count}</span>
            </li>
          ))}
          {statusCounts.length === 0 && <li className="hint">no cases yet</li>}
        </ul>
      </div>
    </section>
  );
}
