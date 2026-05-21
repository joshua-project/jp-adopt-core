import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["@jp-adopt/contracts"],
  // Standalone build → .next/standalone/ contains a self-contained
  // server bundle the Dockerfile copies into a slim runtime image.
  // Required for the multi-stage container build; harmless for
  // `pnpm dev` and `pnpm build` invocations that don't read it.
  output: "standalone",
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
  },
};

export default nextConfig;
