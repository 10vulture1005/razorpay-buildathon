import type { NextConfig } from "next";

const API = process.env.API_URL || "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/:path*` },
    ];
  },
};

export default nextConfig;
