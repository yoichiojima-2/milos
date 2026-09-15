import type { NextConfig } from "next";

// Production builds are a static export copied into the Python package
// (src/milos/console/static/, gitignored); the Docker image builds it in a
// Node stage and the public API serves it beside /v1. `next dev` instead
// proxies /v1 to a local `milos serve api` (rewrites don't exist in a static
// export), which trusts MILOS_DEV_USER in place of IAP.
const nextConfig: NextConfig =
  process.env.NODE_ENV === "development"
    ? {
        rewrites: async () => [{ source: "/v1/:path*", destination: "http://127.0.0.1:8080/v1/:path*" }],
      }
    : { output: "export" };

export default nextConfig;
