"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { signIn, useSession } from "next-auth/react";
import { useState } from "react";
import { safeCallbackUrl, signInError } from "@/lib/signin";

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.9 6.1C12.4 13.6 17.7 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.5 5.8c4.4-4.1 7.1-10.1 7.1-17.5z" />
      <path fill="#FBBC05" d="M10.5 28.7a14.5 14.5 0 0 1 0-9.4l-7.9-6.1a24 24 0 0 0 0 21.6l7.9-6.1z" />
      <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.5-5.8c-2.1 1.4-4.9 2.3-8.4 2.3-6.3 0-11.6-4.1-13.5-9.8l-7.9 6.1C6.5 42.6 14.6 48 24 48z" />
    </svg>
  );
}

export function SignInCard() {
  const params = useSearchParams();
  const { data: session, status } = useSession();
  const [busy, setBusy] = useState(false);
  const error = signInError(params.get("error"));
  const destination = safeCallbackUrl(params.get("callbackUrl"), typeof window === "undefined" ? "http://localhost" : window.location.origin);
  const signedIn = status === "authenticated";

  return (
    <div className="st-panel si-card st-rise">
      <div>
        <div className="st-eyebrow">AgentForge</div>
        <h1>{signedIn ? "You are signed in" : "Sign in to AgentForge"}</h1>
      </div>
      {error ? (
        <div className="si-error" role="alert">
          <strong>Couldn&apos;t sign you in.</strong> {error}
        </div>
      ) : null}
      {signedIn ? (
        <>
          <p className="st-muted m-0">
            Signed in as <span className="st-mono">{session?.user?.email}</span>.
          </p>
          <Link href={destination} className="st-btn st-btn-primary st-btn-lg">
            Continue
          </Link>
        </>
      ) : (
        <>
          <p className="st-muted m-0" style={{ lineHeight: 1.55 }}>
            Use your Google account. AgentForge reads your name and verified email address to know who you are; it does not post or read anything else from Google.
          </p>
          <button
            type="button"
            className="st-btn st-btn-primary st-btn-lg"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void signIn("google", { callbackUrl: destination });
            }}
          >
            <GoogleMark />
            {busy ? "Opening Google…" : "Continue with Google"}
          </button>
        </>
      )}
      <ul className="si-points" aria-label="What to expect">
        <li>Nothing runs until you approve the plan and start it.</li>
        <li>Every sensitive action asks you first, with the exact values.</li>
        <li>Your workspace&apos;s plans, runs and approvals are visible only to its members.</li>
      </ul>
    </div>
  );
}
