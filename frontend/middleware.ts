import { NextResponse } from "next/server";
import { buildCsp, originFromEnv } from "@/lib/security/csp";

/**
 * Override the Content-Security-Policy header per-request using runtime
 * env. The static CSP from `next.config.ts headers()` is baked at build
 * time (`npm run build` inside the Docker build stage), so any value
 * configured on Railway *after* the build — like `COPILOT_URL` — never
 * reaches `buildSecurityHeaders`. Middleware runs per-request in the Node
 * runtime and sees the live `process.env`, so we can compute a CSP that
 * accurately reflects the deployed config.
 *
 * We only override CSP. The other security headers (X-Content-Type-Options,
 * Referrer-Policy, Permissions-Policy, X-Frame-Options) are static and
 * keep their values from `next.config.ts headers()`.
 */
export function middleware() {
  const res = NextResponse.next();
  res.headers.set(
    "Content-Security-Policy",
    buildCsp({ copilotOrigin: originFromEnv(process.env.COPILOT_URL) }),
  );
  return res;
}

export const config = {
  // Skip Next.js internals and static assets — they don't need CSP overrides
  // and matching them would burn middleware overhead per static file.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
