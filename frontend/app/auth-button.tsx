"use client";

import { signIn, signOut, useSession } from "next-auth/react";

function GoogleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 18 18" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.583-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58Z"
      />
    </svg>
  );
}

export function AuthButton() {
  const { data: session, status } = useSession();

  if (status === "loading") {
    return (
      <span className="inline-flex h-9 w-fit items-center gap-2 rounded-xl border border-hairline bg-panel px-4 py-2 text-sm font-medium text-muted backdrop-blur">
        <span className="status-dot" />
        <span className="mono text-xs uppercase tracking-wider">Loading</span>
      </span>
    );
  }

  if (session?.user) {
    return (
      <div className="inline-flex w-fit items-center gap-2.5 rounded-xl border border-hairline bg-panel py-1.5 pl-2 pr-1.5 text-sm text-ink/80 backdrop-blur">
        {session.user.image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={session.user.image}
            alt={session.user.name ?? "User avatar"}
            width={24}
            height={24}
            className="h-6 w-6 rounded-full"
            referrerPolicy="no-referrer"
          />
        ) : (
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/20 text-[11px] font-semibold text-accent">
            {(session.user.email ?? "?").charAt(0).toUpperCase()}
          </span>
        )}
        <span className="hidden max-w-[180px] truncate text-xs text-ink/70 sm:inline">
          {session.user.email}
        </span>
        <button
          type="button"
          onClick={() => signOut()}
          className="rounded-lg border border-transparent px-2.5 py-1 text-xs font-medium text-ink/70 transition-colors hover:border-accent/40 hover:text-ink"
        >
          Sign out
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => signIn("google")}
      className="inline-flex w-fit items-center gap-2 rounded-xl border border-hairline bg-panel px-4 py-2 text-sm font-medium text-ink/80 backdrop-blur transition-colors hover:border-accent/50 hover:text-ink"
    >
      <GoogleIcon />
      Sign in with Google
    </button>
  );
}
