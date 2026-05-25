import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["@jp-adopt/contracts"],
  // Standalone build → .next/standalone/ contains a self-contained
  // server bundle the Dockerfile copies into a slim runtime image.
  // Runs as an Azure Container App (NOT Static Web Apps — SWA cannot run
  // a standalone Node server). See
  // docs/superpowers/plans/2026-05-24-web-on-container-app.md.
  output: "standalone",
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
  },
  // The browser calls same-origin /api/* ; this Node server proxies those
  // requests to the API's INTERNAL container-app FQDN. API_PROXY_TARGET is
  // a runtime env var set by Terraform on the web container — never baked
  // into the client bundle. The /api prefix is stripped here so the
  // FastAPI app keeps serving /healthz, /readyz, /v1/* unprefixed.
  async rewrites() {
    const target = process.env.API_PROXY_TARGET;
    if (!target) return [];
    return [{ source: "/api/:path*", destination: `${target}/:path*` }];
  },
};

export default nextConfig;
