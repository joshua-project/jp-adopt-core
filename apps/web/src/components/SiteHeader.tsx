"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS: ReadonlyArray<{ href: string; label: string }> = [
  { href: "/matches", label: "Matches" },
  { href: "/adopters", label: "Adopters" },
  { href: "/facilitators", label: "Facilitators" },
  { href: "/contacts/new", label: "Add contact" },
  { href: "/facilitator", label: "My contacts" },
];

/**
 * Top navigation, modelled on the JpNavbar component used by the public
 * jp-adopt-forms site:
 *
 * - Dark (#303030) bar with full-width white uppercase links
 * - Orange (`var(--jp-accent)`) 3px underline on the active route
 * - Subtle gray hover background
 *
 * Implementation uses the .jp-nav / .jp-nav-link component classes declared
 * in globals.css so the look matches across both apps without forking the
 * tokens.
 */
export function SiteHeader() {
  const pathname = usePathname() ?? "/";
  return (
    <header className="jp-nav border-b border-black/30">
      <div className="mx-auto flex w-full max-w-6xl items-center justify-between gap-6 px-4 sm:px-6 lg:px-8">
        <Link
          href="/"
          className="font-heading text-lg font-semibold tracking-tight text-white"
          aria-label="JP Adopt — Staff console home"
        >
          <span className="text-[color:var(--jp-accent)]">JP</span> Adopt
          <span className="ml-2 hidden align-middle text-[10px] font-medium uppercase tracking-[0.18em] text-white/50 md:inline">
            Staff console
          </span>
        </Link>
        <nav className="flex items-stretch text-sm" aria-label="Primary">
          {NAV_ITEMS.map((item) => {
            const active =
              item.href === "/"
                ? pathname === "/"
                : pathname === item.href ||
                  pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                data-active={active ? "true" : "false"}
                className="jp-nav-link"
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
