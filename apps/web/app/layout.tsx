import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

import { MsalClientProvider } from "../src/components/MsalClientProvider";

export const metadata: Metadata = {
  title: "JP ADOPT (spike)",
  description: "Phase 0 vertical spike — staff web shell + API + worker",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <MsalClientProvider>
          <header className="border-b border-slate-200 bg-white">
            <div className="mx-auto flex max-w-3xl items-center justify-between gap-4 px-4 py-3">
              <Link href="/" className="font-semibold text-slate-800">
                JP ADOPT
              </Link>
              <nav className="flex gap-4 text-sm text-slate-600">
                <Link href="/contacts" className="hover:text-slate-900">
                  Contacts
                </Link>
              </nav>
            </div>
          </header>
          <main className="mx-auto max-w-3xl px-4 py-8">{children}</main>
        </MsalClientProvider>
      </body>
    </html>
  );
}
