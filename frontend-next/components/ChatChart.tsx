"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartSpec } from "@/lib/api";

const AXIS = { fontSize: 11, fill: "var(--ink-3)", fontFamily: "var(--mono)" };
const PALETTE = [
  "var(--green)",
  "var(--red)",
  "var(--ink-3)",
  "var(--rule-strong)",
];

function fmt(v: number, unit: string | null): string {
  if (unit === "₹") return Number(v).toLocaleString("en-IN");
  return String(v);
}

export default function ChatChart({ spec }: { spec: ChartSpec }) {
  const data = spec.labels.map((label, i) => {
    const row: Record<string, string | number> = { label };
    for (const s of spec.series) row[s.name] = s.data[i] ?? 0;
    return row;
  });

  const tooltipStyle = {
    background: "var(--panel)",
    border: "1px solid var(--rule-strong)",
    fontFamily: "var(--mono)",
    fontSize: 12,
  };

  return (
    <figure className="chat-chart" aria-label={spec.title}>
      <figcaption>{spec.title}</figcaption>
      <ResponsiveContainer width="100%" height={180}>
        {spec.type === "pie" ? (
          <PieChart>
            <Tooltip contentStyle={tooltipStyle} />
            <Legend wrapperStyle={{ fontSize: 11, fontFamily: "var(--mono)" }} />
            <Pie
              data={data}
              dataKey={spec.series[0].name}
              nameKey="label"
              innerRadius={38}
              outerRadius={64}
              paddingAngle={2}
              stroke="var(--panel)"
            >
              {data.map((_, i) => (
                <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
              ))}
            </Pie>
          </PieChart>
        ) : spec.type === "line" ? (
          <LineChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
            <CartesianGrid stroke="var(--rule)" strokeDasharray="2 4" vertical={false} />
            <XAxis dataKey="label" tick={AXIS} tickLine={false} />
            <YAxis tick={AXIS} tickLine={false} width={44} />
            <Tooltip contentStyle={tooltipStyle}
              formatter={(v, n) => [fmt(Number(v ?? 0), spec.unit), String(n)]} />
            <Legend wrapperStyle={{ fontSize: 11, fontFamily: "var(--mono)" }} />
            {spec.series.map((s, i) => (
              <Line
                key={s.name}
                type="monotone"
                dataKey={s.name}
                stroke={PALETTE[i % PALETTE.length]}
                strokeWidth={2}
                dot={{ r: 2.5 }}
              />
            ))}
          </LineChart>
        ) : (
          <BarChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
            <CartesianGrid stroke="var(--rule)" strokeDasharray="2 4" vertical={false} />
            <XAxis dataKey="label" tick={AXIS} tickLine={false} />
            <YAxis tick={AXIS} tickLine={false} width={44} allowDecimals={false} />
            <Tooltip contentStyle={tooltipStyle}
              formatter={(v, n) => [fmt(Number(v ?? 0), spec.unit), String(n)]} />
            <Legend wrapperStyle={{ fontSize: 11, fontFamily: "var(--mono)" }} />
            {spec.series.length === 1
              ? <Bar dataKey={spec.series[0].name} fill="var(--green)" barSize={18}
                  radius={[3, 3, 0, 0]}>
                  {data.map((_, i) => (
                    <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                  ))}
                </Bar>
              : spec.series.map((s, i) => (
                  <Bar key={s.name} dataKey={s.name} fill={PALETTE[i % PALETTE.length]}
                    barSize={14} radius={[3, 3, 0, 0]} />
                ))}
          </BarChart>
        )}
      </ResponsiveContainer>
      {spec.unit && <span className="chat-chart-unit">{spec.unit}</span>}
    </figure>
  );
}
