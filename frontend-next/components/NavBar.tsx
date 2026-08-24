"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/dashboard", label: "Dashboard" },
  { href: "/chat", label: "Copilot" },
  { href: "/sandbox", label: "Sandbox" },
];

export default function NavBar() {
  const pathname = usePathname();
  // The landing page is a full-bleed immersive surface (dark hero, its own
  // status bar) — it carries no chrome of its own, so the app nav stands down.
  if (pathname === "/") return null;
  return (
    <nav className="topnav" aria-label="Primary">
      <Link href="/" className="brand">
        <span className="brand-mark" aria-hidden>
          ₹
        </span>
        Revenue Recovery Autopilot
      </Link>
      <div className="nav-links">
        {LINKS.map((l) => (
          <Link
            key={l.href}
            href={l.href}
            className={"nav-link" + (pathname === l.href ? " active" : "")}
            aria-current={pathname === l.href ? "page" : undefined}
          >
            {l.label}
          </Link>
        ))}
      </div>
      <span className="nav-note">AI proposes · code decides</span>
    </nav>
  );
}
