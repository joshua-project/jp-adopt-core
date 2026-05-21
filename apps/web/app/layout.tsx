import type { Metadata } from "next";
import "./globals.css";

import { MsalClientProvider } from "../src/components/MsalClientProvider";
import { SiteHeader } from "../src/components/SiteHeader";

export const metadata: Metadata = {
  title: "JP Adopt — Staff console",
  description: "Adoption program — staff console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
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
