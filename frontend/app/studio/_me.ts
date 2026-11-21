"use client";

import { useSession } from "next-auth/react";

/** The signed-in person's e-mail, for permission-aware UX only (the backend authorises every request). Null while the session loads. */
export function useMyEmail(): string | null {
  const { data } = useSession();
  return data?.user?.email ?? null;
}
