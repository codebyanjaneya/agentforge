"use client";

// The only client code on the public landing page: the call to action knows whether you are signed in (it is a plain link either way, so it works before JavaScript
// and costs nothing in layout: both labels share a minimum width), and the theme toggle already used across the app.

import Link from "next/link";
import { useSession } from "next-auth/react";
import { ThemeToggle } from "../theme-toggle";

export function LandingCta({ size = "lg", signedOutLabel = "Sign in with Google" }: { size?: "lg" | "md"; signedOutLabel?: string }) {
  const { status } = useSession();
  const signedIn = status === "authenticated";
  return (
    <Link href={signedIn ? "/studio" : "/signin"} className={`st-btn st-btn-primary ${size === "lg" ? "st-btn-lg" : ""}`}>
      {signedIn ? "Open Studio" : signedOutLabel}
    </Link>
  );
}

export function LandingNavEnd() {
  const { status } = useSession();
  const signedIn = status === "authenticated";
  return (
    <div className="lp-nav-end">
      <ThemeToggle />
      {signedIn ? (
        <Link href="/studio" className="st-btn st-btn-primary">
          Open Studio
        </Link>
      ) : (
        <Link href="/signin" className="st-btn">
          Sign in
        </Link>
      )}
    </div>
  );
}
