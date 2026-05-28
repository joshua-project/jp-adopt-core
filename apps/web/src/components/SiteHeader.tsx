"use client";

import { useMsal } from "@azure/msal-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS: ReadonlyArray<{ href: string; label: string }> = [
  { href: "/matches", label: "Matches" },
  { href: "/adopters", label: "Adopters" },
  { href: "/facilitators", label: "Facilitators" },
  { href: "/contacts/new", label: "Add contact" },
  { href: "/facilitator", label: "My contacts" },
  { href: "/admin/users", label: "Admin" },
];

const AUTH_EXEMPT_PATHS = new Set(["/signin", "/auth/callback"]);

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
  const { instance, accounts } = useMsal();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;
  const showSessionChrome = !AUTH_EXEMPT_PATHS.has(pathname);
  const displayName =
    account?.name?.trim() || account?.username?.trim() || null;

  const onSignOut = () => {
    const origin =
      typeof window !== "undefined" ? window.location.origin : "";
    void instance.logoutRedirect({
      account: account ?? undefined,
      postLogoutRedirectUri: origin ? `${origin}/signin` : undefined,
    });
  };

  return (
    <header className="jp-nav border-b border-black/30">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-4 px-4 sm:px-6 lg:px-8">
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
        <div className="flex min-w-0 flex-1 flex-wrap items-stretch justify-end gap-4">
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
          {showSessionChrome && account ? (
            <div className="flex items-stretch gap-3 border-l border-white/20 pl-4 text-sm">
              {displayName ? (
                <span
                  className="hidden max-w-[12rem] truncate self-center text-white/80 sm:inline"
                  title={displayName}
                >
                  {displayName}
                </span>
              ) : null}
              <button
                type="button"
                onClick={onSignOut}
                className="jp-nav-link"
              >
                Sign out
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
