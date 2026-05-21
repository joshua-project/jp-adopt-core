"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS: ReadonlyArray<{ href: string; label: string }> = [
  { href: "/matches", label: "Matches" },
  { href: "/contacts", label: "Contacts" },
  { href: "/contacts/new", label: "Add contact" },
  { href: "/facilitator", label: "Facilitator" },
];

/**
 * Top navigation. Active route is highlighted with the JP orange underline so
 * staff always know which workspace they're in. Layout is shared by every
 * page via app/layout.tsx.
 */
export function SiteHeader() {
  const pathname = usePathname() ?? "/";
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex w-full max-w-6xl items-center justify-between gap-6 px-4 py-3 sm:px-6 lg:px-8">
        <Link
          href="/"
          className="font-heading text-lg font-semibold tracking-tight text-slate-900"
        >
          <span className="text-orange-600">JP</span> Adopt
        </Link>
        <nav className="flex items-center gap-1 text-sm">
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
                className={[
                  "rounded-md px-3 py-2 font-medium transition-colors",
                  active
                    ? "text-orange-700"
                    : "text-slate-600 hover:bg-slate-100 hover:text-slate-900",
                ].join(" ")}
              >
                <span
                  className={
                    active
                      ? "border-b-2 border-orange-500 pb-0.5"
                      : "border-b-2 border-transparent pb-0.5"
                  }
                >
                  {item.label}
                </span>
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
