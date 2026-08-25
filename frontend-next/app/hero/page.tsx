import HeroSplash from "@/components/HeroSplash";

export default function HeroPage() {
  return (
    <main>
      <HeroSplash />
      <section id="content" style={{ padding: "80px 28px" }}>
        <p>Content below the fold.</p>
      </section>
    </main>
  );
}
