import type { Metadata } from "next";
import NavBar from "@/components/NavBar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Revenue Recovery Autopilot",
  description: "AI proposes, code decides — receivables recovery with a full audit trail.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/*
          THESIS: the audit trail IS the interface — a continuous event tape the
          agent writes to, not a grid of metric cards; money counters are a ruled
          header strip on that tape, never hero cards.
          OWN-WORLD: ledger paper ground (#F5F4EF), ink near-black (#1B1E1B),
          one deep ledger green (#1E6B4A) for recovered/positive, signal red
          (#B23A30) reserved for policy rejections, graphite rules instead of
          shadows or cards; tabular numerals throughout; mono for IDs/data.
          STORY: an ops reviewer sees in seconds what the agent did, what policy
          refused, and where the money stands — then drills into one case's full
          trail without leaving the page.
          FIRST VIEWPORT: ruled counter band across the top (At Risk · Recovered ·
          Recovery % · Active · Escalated), funnel bar beneath, then the tape
          filling the viewport left with the case rail right; primary action is
          selecting a case from the rail.
          FORM: audit-tape terminal, grounded candidate 6 of 7, seed key 0100cafa.
          FINISH: unreviewed and undocumented is unfinished; this build ends with
          the finish review, the verdict, and DESIGN.md.
        */}
        <NavBar />
        {children}
      </body>
    </html>
  );
}
