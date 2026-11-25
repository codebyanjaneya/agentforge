import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { ThemeToggle } from "../theme-toggle";
import { SignInCard } from "./signin-card";
import "../studio/studio.css";
import "../studio/foundation.css";

export const metadata: Metadata = {
  title: "Sign in | AgentForge",
  description: "Sign in to AgentForge with your Google account.",
  robots: { index: false },
};

export default function SignInPage() {
  return (
    <div className="st si-shell">
      <header className="lp-nav">
        <div className="lp-wrap">
          <Link href="/" className="st-brand" aria-label="AgentForge, home">
            <span className="st-brand-mark" aria-hidden="true">
              ⌁
            </span>
            AgentForge
          </Link>
          <div className="lp-nav-end">
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main className="si-main" id="main">
        {/* useSearchParams needs a Suspense boundary on a prerendered page (Next 16 docs: use-search-params). The fallback reserves the card's space so nothing shifts. */}
        <Suspense fallback={<div className="st-panel si-card" aria-busy="true" style={{ minHeight: "22rem" }} />}>
          <SignInCard />
        </Suspense>
      </main>
      <footer className="lp-footer" style={{ marginTop: 0 }}>
        <div className="lp-wrap">
          <Link href="/">← Back to the home page</Link>
          <span>Early access</span>
        </div>
      </footer>
    </div>
  );
}
