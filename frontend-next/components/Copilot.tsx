"use client";

import { useEffect, useRef, useState } from "react";
import { api, type CaseSummary, type ChatMessage } from "@/lib/api";

const SUGGESTIONS = [
  "How much money is at risk right now?",
  "Which cases are closest to escalating?",
  "Why did the policy block actions recently?",
  "What happened on my biggest open case?",
];

export default function Copilot() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: "assistant",
      content:
        "Recovery copilot online. I read the live ledger — money at risk, every case, every policy decision, full audit trails. Ask me anything about it. I can explain, but I cannot execute actions; only the policy-gated agent can.",
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .cases()
      .then(setCases)
      .catch(() => setCases([]));
  }, []);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  async function send(text: string) {
    const content = text.trim();
    if (!content || busy) return;
    const next = [...messages, { role: "user" as const, content }];
    setMessages(next);
    setInput("");
    setBusy(true);
    setError(null);
    try {
      const res = await api.chat(next.filter((m, i) => !(i === 0 && m.role === "assistant")), focus);
      setMessages((m) => [...m, { role: "assistant", content: res.answer }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "copilot unreachable");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="copilot">
      <header className="masthead">
        <h1 className="wordmark">Recovery Copilot</h1>
        <span className="live" role="status">
          <span className={"live-dot" + (error ? " stale" : "")} aria-hidden />
          context-grounded · read-only
        </span>
        <span className="spacer" />
        <label className="focus-picker">
          Focus case{" "}
          <select value={focus ?? ""} onChange={(e) => setFocus(e.target.value || null)}>
            <option value="">— none (whole book) —</option>
            {(cases ?? []).slice(0, 50).map((c) => (
              <option key={c.case_id} value={c.case_id}>
                {c.case_id} · ₹{c.amount_at_risk.toLocaleString("en-IN")} · {c.status}
              </option>
            ))}
          </select>
        </label>
      </header>

      <div className="chat-scroll" ref={listRef} aria-live="polite">
        {messages.map((m, i) => (
          <div key={i} className={"bubble-row " + m.role}>
            <div className={"bubble " + m.role}>
              {m.content.split("\n").map((line, j) => (
                <p key={j}>{line}</p>
              ))}
            </div>
          </div>
        ))}
        {busy && (
          <div className="bubble-row assistant">
            <div className="bubble assistant" aria-busy="true">
              <p className="thinking">reading the ledger…</p>
            </div>
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            Copilot unavailable ({error}) — is the API running with LLM configured?
          </div>
        )}
      </div>

      <div className="suggest-row">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="suggest" onClick={() => send(s)} disabled={busy}>
            {s}
          </button>
        ))}
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(input);
            }
          }}
          placeholder={focus ? `Asking about ${focus}…` : "Ask about cases, policy blocks, recoveries…"}
          rows={2}
          aria-label="Message the recovery copilot"
        />
        <button type="submit" className="btn primary" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </main>
  );
}
