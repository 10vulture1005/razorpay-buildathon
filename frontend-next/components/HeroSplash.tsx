"use client";

import { useEffect, useRef, useState } from "react";

/* ------------------------------------------------------------------ */
/* Time-of-day periods — one gradient (top → middle → bottom),         */
/* a status-bar label and a serif headline each.                       */
/* `center` is the fractional hour at which the period peaks; themes   */
/* are continuously interpolated BETWEEN these anchors so the gradient */
/* drifts across the day instead of snapping at boundaries.            */
/* ------------------------------------------------------------------ */
type Period = {
  key: string;
  label: string;
  headline: string;
  stops: [string, string, string];
  center: number; // 0–24 fractional hour
};

const NIGHT: Period = {
  key: "night",
  label: "NIGHT",
  headline: "While you sleep, we recover",
  stops: ["#04060e", "#0d1330", "#232b58"],
  center: 0,
};

const PERIODS: Period[] = [
  NIGHT,
  {
    key: "dawn",
    label: "DAWN",
    headline: "The dawn of recovered revenue",
    stops: ["#e07856", "#f2a67a", "#fbd68a"],
    center: 6,
  },
  {
    key: "morning",
    label: "MORNING",
    headline: "Revenue rising with the sun",
    stops: ["#f0975c", "#ffc275", "#ffe6ad"],
    center: 9,
  },
  {
    key: "midday",
    label: "MIDDAY",
    headline: "Recovery in full daylight",
    stops: ["#3f9bdc", "#8ccbf1", "#eaf6fd"],
    center: 13.5,
  },
  {
    key: "dusk",
    label: "DUSK",
    headline: "Chasing invoices ends today",
    stops: ["#5d3f86", "#d76f95", "#f6a56b"],
    center: 17.5,
  },
];

/* Anchors on a circular 24h timeline — night appears at both ends so
   the 11pm→5am stretch keeps blending toward deep night. */
const ANCHORS: Period[] = [...PERIODS, { ...NIGHT, center: 24 }];

/* ------------------------------ helpers --------------------------- */

function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function mix(a: string, b: string, t: number): string {
  const [r1, g1, b1] = hexToRgb(a);
  const [r2, g2, b2] = hexToRgb(b);
  const r = Math.round(r1 + (r2 - r1) * t);
  const g = Math.round(g1 + (g2 - g1) * t);
  const bl = Math.round(b1 + (b2 - b1) * t);
  return `rgb(${r}, ${g}, ${bl})`;
}

function smoothstep(t: number): number {
  return t * t * (3 - 2 * t);
}

function formatClock(d: Date): string {
  let h = d.getHours();
  const suffix = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(h)}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${suffix}`;
}

type Theme = {
  stops: [string, string, string];
  dominant: Period;
  /** 0 = pure sun, 1 = pure moon (used to crossfade the icons) */
  nightWeight: number;
};

function themeAt(date: Date): Theme {
  const h =
    date.getHours() +
    date.getMinutes() / 60 +
    date.getSeconds() / 3600;

  let i = 0;
  while (i < ANCHORS.length - 2 && h > ANCHORS[i + 1].center) i++;
  const a = ANCHORS[i];
  const b = ANCHORS[i + 1];
  const t = smoothstep(
    Math.min(1, Math.max(0, (h - a.center) / (b.center - a.center))),
  );

  const stops: [string, string, string] = [
    mix(a.stops[0], b.stops[0], t),
    mix(a.stops[1], b.stops[1], t),
    mix(a.stops[2], b.stops[2], t),
  ];

  const nightWeight =
    a.key === "night" ? 1 - t : b.key === "night" ? t : 0;

  return { stops, dominant: t < 0.5 ? a : b, nightWeight };
}

/* ------------------------------ icon ------------------------------ */

const RAYS = Array.from({ length: 12 }, (_, i) => (i / 12) * Math.PI * 2);

function Sunburst({ dimmed }: { dimmed: number }) {
  return (
    <svg
      className="splash-icon"
      viewBox="-100 -100 200 200"
      width="150"
      height="150"
      aria-hidden
      style={{ opacity: 1 - dimmed }}
    >
      {/* slow continuous rotation + gentle pulse */}
      <g className="splash-rays">
        {RAYS.map((angle, i) => {
          const inner = 34;
          const outer = 62 + (i % 2 ? 10 : 0); // alternating lengths
          return (
            <line
              key={i}
              className="splash-ray"
              style={{ "--delay": `${250 + i * 70}ms` } as React.CSSProperties}
              x1={Math.cos(angle) * inner}
              y1={Math.sin(angle) * inner}
              x2={Math.cos(angle) * outer}
              y2={Math.sin(angle) * outer}
            />
          );
        })}
      </g>
      <circle className="splash-core" r="5" />
    </svg>
  );
}

/* ---------------------------- component --------------------------- */

export default function HeroSplash({
  fixed = false,
  anchor = "#content",
}: {
  /** position: fixed layer for the sheet-reveal layout */
  fixed?: boolean;
  /** scroll-cue target */
  anchor?: string;
}) {
  const [now, setNow] = useState<Date | null>(null);
  const centerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  /* Scroll-linked zoom: in fixed mode, the icon+headline block scales up
     and dissolves as scroll progress (0→1, same reveal distance as the
     sheet in Landing) increases. Tightly bound to scrollY — no duration. */
  useEffect(() => {
    if (!fixed) return;
    const el = centerRef.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let raf = 0;
    const update = () => {
      raf = 0;
      const vh = window.innerHeight;
      const mobile = window.innerWidth < 640;
      const dist = vh * (mobile ? 0.55 : 0.7); // match sheet reveal distance
      const p = Math.min(1, Math.max(0, window.scrollY / dist));
      // zoom in from center: 1 → ~1.22
      const scale = 1 + p * (mobile ? 0.14 : 0.22);
      el.style.transform = `scale(${scale})`;
      // hold fully visible until a third of the way, then dissolve
      const fade = Math.min(1, Math.max(0, (p - 0.35) / 0.5));
      el.style.opacity = String(1 - fade);
      el.style.visibility = p >= 1 ? "hidden" : "";
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
  }, [fixed]);

  // Deterministic fallback for SSR markup; replaced after first tick.
  // The section fades in on the first real tick (splash-live) so the
  // fallback→actual theme swap is never seen as a hard jump.
  const theme = themeAt(now ?? new Date("2026-01-01T06:00:00"));
  const mounted = now !== null;

  return (
    <section
      className={`splash${fixed ? " splash-fixed" : ""}${mounted ? " splash-live" : ""}`}
      style={
        {
          "--splash-g1": theme.stops[0],
          "--splash-g2": theme.stops[1],
          "--splash-g3": theme.stops[2],
          "--moon-opacity": theme.nightWeight,
        } as React.CSSProperties
      }
    >
      {/* live gradient layer — fades in over the section's static fallback
          gradient on first tick, masking the SSR→real-time theme handoff */}
      <div
        className={`splash-bg splash-bg-live`}
        aria-hidden
      />

      {/* grainy texture overlay */}
      <div className="splash-grain" aria-hidden />


      {/* centered icon + headline */}
      <div className="splash-center" ref={centerRef}>
        <div className="splash-icons">
          <Sunburst dimmed={theme.nightWeight} />
          <span
            className="splash-moonwrap"
            style={{ opacity: theme.nightWeight }}
            aria-hidden={theme.nightWeight < 0.5}
          >
            <MoonSvg />
          </span>
        </div>
        <h1 className="splash-headline">
          <span key={theme.dominant.key}>{theme.dominant.headline}</span>
        </h1>
      </div>

      {/* scroll cue */}
      <a href={anchor} className="splash-chevron" aria-label="Scroll down">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path
            d="M5 9l7 7 7-7"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </a>
    </section>
  );
}

/* Standalone moon svg (opacity driven by wrapper) */
function MoonSvg() {
  return (
    <svg className="splash-icon" viewBox="-100 -100 200 200" width="150" height="150" aria-hidden>
      <path
        className="splash-moon"
        d="M 18 -52 A 55 55 0 1 0 18 52 A 44 44 0 1 1 18 -52 Z"
      />
      <circle className="splash-star" cx="-38" cy="-30" r="2.6" />
      <circle className="splash-star" cx="-14" cy="-52" r="1.8" style={{ animationDelay: "300ms" }} />
      <circle className="splash-star" cx="42" cy="-18" r="2.2" style={{ animationDelay: "600ms" }} />
      <circle className="splash-star" cx="30" cy="34" r="1.6" style={{ animationDelay: "900ms" }} />
      <circle className="splash-star" cx="-30" cy="26" r="2" style={{ animationDelay: "1200ms" }} />
    </svg>
  );
}
