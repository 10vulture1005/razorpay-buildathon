"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Instrument_Serif, Plus_Jakarta_Sans } from "next/font/google";
import {
  useCallback,
  useEffect,
  useRef,
  type CSSProperties,
} from "react";
import HeroSplash from "@/components/HeroSplash";

const solDisplay = Instrument_Serif({
  weight: "400",
  style: ["normal", "italic"],
  subsets: ["latin"],
  variable: "--f-display",
  display: "swap",
});
const solSans = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--f-sans",
  display: "swap",
});

/* ------------------------------------------------------------------ */
/* Reveal-on-scroll: blur-in entrance, exactly like the reference site */
/* ------------------------------------------------------------------ */
function useReveal() {
  useEffect(() => {
    const els = Array.from(document.querySelectorAll<HTMLElement>("[data-reveal]"));
    if (!("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("sol-in"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            (e.target as HTMLElement).classList.add("sol-in");
            io.unobserve(e.target);
          }
        }
      },
      { threshold: 0.18 },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}

function Reveal({
  children,
  delay = 0,
  className = "",
  as: Tag = "div",
  ...rest
}: {
  children: React.ReactNode;
  delay?: number;
  className?: string;
  as?: "div" | "li" | "section";
} & Omit<React.HTMLAttributes<HTMLElement>, "children">) {
  return (
    <Tag
      data-reveal=""
      className={`sol-reveal ${className}`}
      style={{ "--reveal-delay": `${delay}ms` } as CSSProperties}
      {...rest}
    >
      {children}
    </Tag>
  );
}

/* ------------------------------------------------------------------ */
/* Scroll-linked sheet reveal: the white card starts pushed below the  */
/* viewport and eases up over the fixed gradient hero as you scroll.   */
/* Pure scroll-position math (rAF), no scroll hijacking.               */
/* ------------------------------------------------------------------ */
function useSheetReveal() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let raf = 0;
    const update = () => {
      raf = 0;
      const vh = window.innerHeight;
      const mobile = window.innerWidth < 640;
      // reveal completes after this much scrolling…
      const dist = vh * (mobile ? 0.55 : 0.7);
      const p = Math.min(1, Math.max(0, window.scrollY / dist));
      // …with easeOutCubic so the sheet decelerates as it locks in
      const eased = 1 - Math.pow(1 - p, 3);
      const maxOffset = vh * (mobile ? 0.22 : 0.38);
      el.style.transform =
        p >= 1 ? "" : `translate3d(0, ${(1 - eased) * maxOffset}px, 0)`;
      const radius = (mobile ? 24 : 32) - eased * (mobile ? 6 : 8);
      el.style.borderRadius = `${radius}px ${radius}px 0 0`;
    };
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  return ref;
}

const PIPELINE = [
  {
    step: "01",
    title: "Leakage detected",
    by: "your system → API",
    body: "Any billing system fires POST /events/invoice-overdue. That instant, a case is opened with the exact amount at risk. This is the leak entering the pipeline.",
    tag: "deterministic",
  },
  {
    step: "02",
    title: "Context assembled",
    by: "code",
    body: "The agent pulls only what matters: amount, days overdue, on-time rate, broken promises, last 3 messages. State lives in Postgres, never in a prompt.",
    tag: "deterministic",
  },
  {
    step: "03",
    title: "Diagnose the cause",
    by: "LLM (frontier model)",
    body: "Cashflow issue, dispute, forgotten invoice — or unwilling payer? The LLM returns a structured verdict with confidence. Free text never flows downstream.",
    tag: "LLM",
  },
  {
    step: "04",
    title: "Choose the move",
    by: "LLM + expected-value math",
    body: "The LLM proposes: gentle reminder, payment link, or human escalation. Code computes net expected value, so a ₹500 leak never triggers a ₹5,000 phone call.",
    tag: "LLM proposes",
  },
  {
    step: "05",
    title: "Policy gate",
    by: "deterministic code — zero LLM",
    body: "Opt-out respected, max 3 attempts, 7-day window, 1 message/day, contact-hours only. Every block is logged. The agent cannot argue its way around a rule.",
    tag: "code decides",
  },
  {
    step: "06",
    title: "Real money moves",
    by: "Razorpay + email",
    body: "Approved actions send real emails and create real Razorpay payment links. Recovery is confirmed only by a verified webhook — never by the agent's own claim.",
    tag: "real integrations",
  },
];

const SPLIT = [
  {
    label: "What AI handles",
    tone: "",
    items: [
      "Diagnosing why payment stopped",
      "Choosing intervention style & tone",
      "Drafting personalized reminders",
      "Explaining escalations to humans",
    ],
  },
  {
    label: "What code enforces",
    tone: "code",
    items: [
      "Retry limits, windows, daily caps",
      "Idempotency — no double sends",
      "Payment verification via webhook",
      "Every decision written to the audit ledger",
    ],
  },
];

export default function Landing() {
  const sheetRef = useSheetReveal();
  const router = useRouter();
  useReveal();

  const onCta = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      router.push("/dashboard");
    },
    [router],
  );

  return (
    <main className={`sol ${solDisplay.variable} ${solSans.variable}`}>
      {/* ============ FIXED TIME-OF-DAY HERO (z-index: 0) ============ */}
      <HeroSplash fixed anchor="#manifesto" />
      <div className="splash-spacer" aria-hidden />

      {/* ============= WHITE SHEET SLIDING OVER HERO (z-index: 10) ==== */}
      <div className="sol-card" ref={sheetRef}>
        {/* ------------------------ MANIFESTO ------------------------ */}
        <section className="sol-manifesto" id="manifesto">
          <Reveal>
            <h2 className="sol-display">
              Invoices went quiet.
              <br />
              The money stayed stuck.
            </h2>
          </Reveal>
          <div className="sol-manifesto-body">
            <Reveal delay={90}>
              <p>You know the pattern.</p>
              <p>
                Revenue was earned, invoiced — and then… nothing. Each unpaid invoice
                drifts somewhere between a forgotten inbox and a broken promise.
              </p>
              <p>
                These open loops cost more than they look: cashflow, follow-up hours,
                relationships. And every week an invoice sits, the odds of ever seeing
                that money quietly shrink.
              </p>
              <p className="sol-strong">
                Recovery has always been a human grind of reminders and awkward calls.
                It deserves the same attention automation just gave everything else.
              </p>
              <p className="sol-strong">
                We built the Revenue Recovery Autopilot so the money finds its own way
                home.
              </p>
            </Reveal>
          </div>
        </section>

        {/* ------------------------- PIPELINE ------------------------ */}
        <section className="sol-section">
          <Reveal className="sol-section-head">
            <span className="sol-microlabel">HOW IT WORKS</span>
            <h2>The life of one leaked rupee</h2>
          </Reveal>
          <ol className="sol-pipeline">
            {PIPELINE.map((s, i) => (
              <Reveal as="li" key={s.step} delay={i * 60} className={s.tag.includes("LLM") ? "is-llm" : ""}>
                <span className="sol-stepno">{s.step}</span>
                <div>
                  <div className="sol-stephead">
                    <h3>{s.title}</h3>
                    <span
                      className={`sol-chip ${
                        s.tag.includes("LLM")
                          ? "chip-llm"
                          : s.tag === "real integrations"
                            ? "chip-real"
                            : "chip-code"
                      }`}
                    >
                      {s.tag}
                    </span>
                  </div>
                  <p>{s.body}</p>
                  <span className="sol-stepby">{s.by}</span>
                </div>
              </Reveal>
            ))}
          </ol>
        </section>

        {/* --------------------------- SPLIT -------------------------- */}
        <section className="sol-section">
          <Reveal className="sol-section-head">
            <span className="sol-microlabel">THE SEPARATION</span>
            <h2>Where the LLM is used — and where it is forbidden</h2>
          </Reveal>
          <div className="sol-split">
            {SPLIT.map((col, ci) => (
              <Reveal key={col.label} delay={ci * 120}>
                <div className={`sol-split-col ${col.tone}`}>
                  <h3>{col.label}</h3>
                  <ul>
                    {col.items.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              </Reveal>
            ))}
          </div>
        </section>

        {/* --------------------------- STACK -------------------------- */}
        <section className="sol-backers">
          <Reveal>
            <h2>Built on rails you can audit</h2>
          </Reveal>
          <Reveal delay={120}>
            <ul className="sol-logos" aria-label="Technology stack">
              {["FastAPI", "LangGraph", "PostgreSQL", "Razorpay", "OpenRouter"].map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
            <p className="sol-backers-note">
              <b>every action</b> logged, replayable, and reversible.
            </p>
          </Reveal>
        </section>

        {/* -------------------------- FOOTER -------------------------- */}
        <footer className="sol-footer">
          <div className="sol-foot-rule" />
          <div className="sol-foot-row">
            <span>© 2026 REVENUE RECOVERY AUTOPILOT — AI PROPOSES · CODE DECIDES</span>
            <span className="sol-foot-links">
              <Link href="/dashboard">DASHBOARD</Link>
              <span className="sol-dot" aria-hidden />
              <Link href="/chat">COPILOT</Link>
              <span className="sol-dot" aria-hidden />
              <Link href="/sandbox">SANDBOX</Link>
            </span>
          </div>
        </footer>
      </div>

      {/* ========================= FINAL CTA ========================== */}
      <section className="sol-final">
        <div className="sol-final-glow" aria-hidden />
        <Reveal className="sol-final-inner">
          <h2>Watch it work on real data</h2>
          <p>
            The dashboard polls the same API your billing system integrates with. Every
            diagnosis, every action, every policy block — as they happen.
          </p>
          <form className="sol-cta-form" onSubmit={onCta}>
            <input
              type="email"
              placeholder="Enter your work email"
              aria-label="Work email"
            />
            <button type="submit">
              OPEN THE DASHBOARD
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </form>
        </Reveal>
      </section>
    </main>
  );
}
