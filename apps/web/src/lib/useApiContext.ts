"use client";

import { useMemo } from "react";
import { useMsal } from "@azure/msal-react";

import type { ApiClientContext } from "./api-client";

/**
 * Hook that returns a snapshot ApiClientContext usable by ``apiFetch`` and the
 * typed wrappers. Memoizes on (instance identity, account count) so client
 * components don't re-render every render.
 */
export function useApiContext(): ApiClientContext {
  const { instance, accounts } = useMsal();
  return useMemo(
    () => ({ instance, accounts }),
    // ``instance`` is stable across renders (MsalClientProvider owns it);
    // ``accounts`` is rebuilt by MSAL's internal state but the length+username
    // is enough signal for downstream callers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [instance, accounts.length, accounts[0]?.homeAccountId],
  );
}
