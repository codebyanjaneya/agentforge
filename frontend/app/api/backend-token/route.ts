// POST /api/backend-token (D018): same-origin, no-store issuance of the 10-minute API access token. All logic
// lives in lib/backend-token.ts; this file supplies the server-side identity: NextAuth v4's `getToken` decrypts the
// session cookie into the server-side JWT, which holds the exact Google `sub`. That subject is deliberately NOT
// part of the browser-visible session (see lib/auth-callbacks.ts).

import { getToken } from "next-auth/jwt";
import type { NextRequest } from "next/server";

import { handleTokenRequest, methodNotAllowed } from "@/lib/backend-token";

export async function POST(request: NextRequest) {
  return handleTokenRequest(request, {
    getToken: () => getToken({ req: request, secret: process.env.NEXTAUTH_SECRET }),
    env: process.env,
  });
}

// Every other method answers 405 with `Allow: POST` and no-store headers.
export const GET = methodNotAllowed;
export const PUT = methodNotAllowed;
export const PATCH = methodNotAllowed;
export const DELETE = methodNotAllowed;
