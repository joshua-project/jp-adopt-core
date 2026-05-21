import type { Metadata } from "next";
import { Inter_Tight, Open_Sans } from "next/font/google";
import "./globals.css";

import { MsalClientProvider } from "../src/components/MsalClientProvider";
import { SiteHeader } from "../src/components/SiteHeader";

// JP brand fonts — Inter Tight for headings, Open Sans for body. Loaded
// via next/font so they are self-hosted, preconnected, and exposed as CSS
// variables (consumed in tailwind.config.ts via `var(--font-heading)` /
// `var(--font-body)`).
const interTight = Inter_Tight({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-heading",
  weight: ["500", "600", "700"],
});

const openSans = Open_Sans({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-body",
  weight: ["400", "500", "600", "700"],
});

export const metadata: Metadata = {
  title: "JP Adopt — Staff console",
  description: "Adoption program — staff console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${interTight.variable} ${openSans.variable}`}
    >
      <body className="font-body">
        <MsalClientProvider>
          <SiteHeader />
          <main className="mx-auto w-full max-w-6xl px-4 py-10 sm:px-6 lg:px-8">
            {children}
          </main>
        </MsalClientProvider>
      </body>
    </html>
  );
}
