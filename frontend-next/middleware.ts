import { NextResponse, type NextRequest } from "next/server";

/**
 * Injects the backend API key into server-proxied /api/* requests.
 * The key lives only in server env (API_KEY) — never exposed to the browser.
 *
 * If API_KEY is not configured we fail fast with a 503 and a clear reason,
 * instead of silently proxying an unauthenticated request that the backend
 * rejects with a generic-looking 401.
 */
export function middleware(request: NextRequest) {
  const apiKey = process.env.API_KEY ?? "";
  if (!apiKey) {
    return NextResponse.json(
      {
        detail:
          "Dashboard misconfigured: API_KEY is not set in frontend-next/.env.local — requests would be rejected by the backend.",
      },
      { status: 503 },
    );
  }
  const requestHeaders = new Headers(request.headers);
  if (!requestHeaders.has("x-api-key")) {
    requestHeaders.set("x-api-key", apiKey);
  }
  return NextResponse.next({ request: { headers: requestHeaders } });
}

export const config = {
  matcher: ["/api/:path*"],
};
