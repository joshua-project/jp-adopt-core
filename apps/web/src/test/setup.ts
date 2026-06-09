/**
 * Vitest global setup — loaded before every test file.
 *
 * Imports jest-dom matchers (`toBeInTheDocument`, `toHaveTextContent`, etc.)
 * onto vitest's `expect`, and resets URL/storage between tests so a flaky
 * test in file A can't leak state into file B.
 */
import "@testing-library/jest-dom/vitest";

import type { ReactNode } from "react";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// #106: MSAL pins the Node event loop when loaded under jsdom — its
// constructor schedules background work that the vitest worker can't
// reclaim, so a test file that transitively imports api-client (which
// imports msalConfig which imports @azure/msal-browser) hangs at
// worker teardown. Globally mocking both MSAL packages here prevents
// the real ones from loading in any test, no matter how they get
// pulled in.
vi.mock("@azure/msal-browser", () => ({
  InteractionRequiredAuthError: class InteractionRequiredAuthError extends Error {},
  PublicClientApplication: class {
    initialize = async () => {};
    getActiveAccount = () => null;
    setActiveAccount = () => {};
    addEventCallback = () => "";
    removeEventCallback = () => {};
    acquireTokenSilent = async () => ({ accessToken: "test-token" });
    acquireTokenRedirect = async () => {};
    logoutRedirect = async () => {};
    getAllAccounts = () => [];
  },
  EventType: {},
  LogLevel: {},
}));
vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({ instance: null, accounts: [] }),
  MsalProvider: ({ children }: { children: ReactNode }) => children,
}));

afterEach(() => {
  cleanup();
  // Reset localStorage so a test that stashes a dev-token doesn't leak.
  window.localStorage.clear();
  window.sessionStorage.clear();
});
