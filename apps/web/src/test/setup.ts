/**
 * Vitest global setup — loaded before every test file.
 *
 * Imports jest-dom matchers (`toBeInTheDocument`, `toHaveTextContent`, etc.)
 * onto vitest's `expect`, and resets URL/storage between tests so a flaky
 * test in file A can't leak state into file B.
 */
import "@testing-library/jest-dom/vitest";

import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  // Reset localStorage so a test that stashes a dev-token doesn't leak.
  window.localStorage.clear();
  window.sessionStorage.clear();
});
