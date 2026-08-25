"use client";

import { useEffect, useRef, useState } from "react";
import ChatChart from "@/components/ChatChart";
import { api, type CaseSummary, type ChatMessage, type EmailDraft } from "@/lib/api";

const SUGGESTIONS = [
  "How much money is at risk right now?",
  "Which cases are closest to escalating?",
  "Why did the policy block actions recently?",
  "What happened on my biggest open case?",
];

type DraftStatus = "pending" | "sending" | "sent" | "discarded" | "failed";
type DraftCard = EmailDraft & { status: DraftStatus; error?: string };
type Msg = ChatMessage & { draft?: DraftCard };

function EmailDraftCard({
  msg,
  index,
  onChange,
  onConfirm,
  onDiscard,
}: {
  msg: Msg;
  index: number;
  onChange: (index: number, patch: Partial<DraftCard>) => void;
  onConfirm: (index: number) => void;
  onDiscard: (index: number) => void;
}) {
  const d = msg.draft!;
  if (d.status === "discarded") {
    return <div className="email-card discarded">Draft discarded.</div>;
  }
  if (d.status === "sent") {
    return <div className="email-card sent">Email sent to {d.to} ✓</div>;
  }
  const locked = d.status === "sending";
  const ready = d.to.includes("@") && d.subject.trim() && d.body.trim() && !locked;
  return (
    <div className="email-card">
      <div className="email-card-title">Draft email — review and edit before sending</div>
      <label className="email-field">
        To
        <input
          value={d.to}
          onChange={(e) => onChange(index, { to: e.target.value })}
          disabled={locked}
          aria-label="Recipient email"
        />
      </label>
      <label className="email-field">
        Subject
        <input
          value={d.subject}
          onChange={(e) => onChange(index, { subject: e.target.value })}
          disabled={locked}
          aria-label="Email subject"
        />
      </label>
      <label className="email-field">
        Body
        <textarea
          value={d.body}
          onChange={(e) => onChange(index, { body: e.target.value })}
          disabled={locked}
          rows={8}
          aria-label="Email body"
        />
      </label>
      {d.status === "failed" && (
        <p className="email-error" role="alert">
          Send failed ({d.error}) — edit and try again.
        </p>
      )}
      <div className="email-actions">
        <button className="btn primary" disabled={!ready} onClick={() => onConfirm(index)}>
          {locked ? "Sending…" : "Confirm & send"}
        </button>
        <button className="btn" disabled={locked} onClick={() => onDiscard(index)}>
          Discard
        </button>
      </div>
    </div>
  );
}

export default function Copilot() {
  const [messages, setMessages] = useState<Msg[]>([
    {
      role: "assistant",
      content:
        "Recovery copilot online. I read the live ledger — money at risk, every case, every policy decision, full audit trails. Ask me anything about it. I can explain what happened, and if you ask me to email someone I'll draft it for you here — you edit it and confirm before anything is sent; only the policy-gated agent executes actions on its own.",
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
    const next: Msg[] = [...messages, { role: "user" as const, content }];
    setMessages(next);
    setInput("");
    setBusy(true);
    setError(null);
    try {
      const res = await api.chat(
        next
          .filter((m, i) => !(i === 0 && m.role === "assistant"))
          .map(({ role, content }) => ({ role, content })),
        focus,
      );
      const reply: Msg = { role: "assistant", content: res.answer, chart: res.chart ?? null };
      if (res.email_draft) {
        reply.draft = { ...res.email_draft, status: "pending" };
      }
      setMessages((m) => [...m, reply]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "copilot unreachable");
    } finally {
      setBusy(false);
    }
  }

  function patchDraft(index: number, patch: Partial<DraftCard>) {
    setMessages((m) =>
      m.map((msg, i) =>
        i === index && msg.draft ? { ...msg, draft: { ...msg.draft, ...patch } } : msg,
      ),
    );
  }

  async function confirmSend(index: number) {
    const msg = messages[index];
    if (!msg?.draft) return;
    patchDraft(index, { status: "sending", error: undefined });
    try {
      await api.sendEmail(
        { to: msg.draft.to.trim(), subject: msg.draft.subject.trim(), body: msg.draft.body },
        focus,
      );
      patchDraft(index, { status: "sent", error: undefined });
    } catch (e) {
      patchDraft(index, {
        status: "failed",
        error: e instanceof Error ? e.message : "delivery error",
      });
    }
  }

  function discardDraft(index: number) {
    patchDraft(index, { status: "discarded" });
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
            <div className={"bubble " + m.role + (m.draft ? " has-draft" : "")}>
              {m.content.split("\n").map((line, j) => (
                <p key={j}>{line}</p>
              ))}
              {m.role === "assistant" && m.chart && <ChatChart spec={m.chart} />}
              {m.role === "assistant" && m.draft && (
                <EmailDraftCard
                  msg={m}
                  index={i}
                  onChange={patchDraft}
                  onConfirm={confirmSend}
                  onDiscard={discardDraft}
                />
              )}
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
