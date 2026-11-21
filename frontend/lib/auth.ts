// The single NextAuth v4 configuration (M1-G). It lives here, not in the route file: an App Router route
// module may only export HTTP-method handlers, and server code (the token endpoint, proxy) imports it.

import type { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";

import { SESSION_MAX_AGE_SECONDS, jwtCallback, sessionCallback, signInCallback } from "./auth-callbacks";

export const authOptions: NextAuthOptions = {
  providers: [
    GoogleProvider({
      clientId: process.env.GOOGLE_CLIENT_ID ?? "",
      clientSecret: process.env.GOOGLE_CLIENT_SECRET ?? "",
    }),
  ],
  // Explicit secret and lifetime (D018): the NextAuth session lasts 7 days; the API access token (10 minutes) is
  // issued separately by /api/backend-token and is never part of this session.
  secret: process.env.NEXTAUTH_SECRET,
  session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_SECONDS },
  callbacks: {
    signIn: ({ account, profile }) => signInCallback({ account: account as never, profile: profile as never }),
    jwt: ({ token, account, profile }) =>
      jwtCallback({ token: token as never, account: account as never, profile: profile as never }) as unknown as typeof token,
    session: ({ session }) => sessionCallback({ session: session as never }) as unknown as typeof session,
  },
};
