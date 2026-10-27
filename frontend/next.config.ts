import type { NextConfig } from "next";

const upstream = (process.env.API_PROXY_TARGET || "http://127.0.0.1:8000").replace(/\/$/, "");
const nextConfig: NextConfig = {
  reactStrictMode: true,
  trailingSlash: true,
  async rewrites() {
    return [{ source: "/api/v1/:path*/", destination: `${upstream}/api/v1/:path*/` }];
  },
};
export default nextConfig;
