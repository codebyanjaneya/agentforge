// Optimistic page gate (M1-G, Next 16 `proxy`, formerly middleware): a request for a feature page without a valid
// NextAuth session cookie is redirected to the NextAuth sign-in page. This is a convenience, NOT the security
// boundary: the backend verifies a bearer token on every API call (M1-D/M1-F). `/` stays public; API routes
// (including /api/auth and /api/backend-token) are not matched here and enforce their own rules.

import { getToken } from "next-auth/jwt";
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export async function proxy(request: NextRequest) {
  const token = await getToken({ req: request, secret: process.env.NEXTAUTH_SECRET });
  if (token && typeof token.providerSubject === "string" && token.emailVerified === true) {
    return NextResponse.next();
  }
  const signIn = new URL("/api/auth/signin", request.url);
  signIn.searchParams.set("callbackUrl", request.nextUrl.pathname + request.nextUrl.search);
  return NextResponse.redirect(signIn);
}

export const config = {
  matcher: ["/autonomous/:path*", "/history/:path*", "/improve/:path*", "/twin/:path*", "/studio/:path*"],
};
