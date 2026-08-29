import type { NextConfig } from "next";

// `API_URL` is the upstream the dashboard proxies /api/* to.
// In development, default to localhost. In production, it MUST be set —
// missing-in-prod is treated as a build error to prevent accidentally
// shipping a dashboard that talks to localhost.
const API = process.env.API_URL
  || process.env.NEXT_PUBLIC_API_BASE_URL
  || (process.env.NODE_ENV === "production" ? "" : "http://localhost:8000");

if (!API) {
  throw new Error(
    "API_URL (or NEXT_PUBLIC_API_BASE_URL) must be set in production. " +
    "Set it in your host's environment before running `next build`."
  );
}

const nextConfig: NextConfig = {
  // `standalone` produces a minimal server that copies only the deps
  // the runtime needs — ~80% smaller image for the dashboard container.
  output: "standalone",
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/:path*` },
    ];
  },
};

export default nextConfig;
