/**
 * Tests for the shared API client. The MSAL path is covered separately; here
 * we focus on the parts that don't depend on a live Entra instance:
 *
 *   - ApiError shape + extractErrorMessage parsing across the three envelope
 *     formats the API actually emits.
 *   - formatApiError surface used by every component's catch block.
 *   - apiFetch happy / error / 204 paths under dev-local auth (Entra not
 *     configured), via fetch monkeypatching.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, formatApiError, resolveAccessToken } from "../api-client";
import type { ApiClientContext } from "../api-client";

// ── Mock msalConfig so isEntraClientConfigured() returns false. The default
// dev-local path is what test environments resolve against.
vi.mock("../msalConfig", () => ({
  API_ACCESS_SCOPES: ["api://test/api.access"],
  isEntraClientConfigured: () => false,
}));

const devCtx: ApiClientContext = {
  instance: null,
  accounts: [],
};

describe("ApiError + extractErrorMessage", () => {
  it("extracts message from {error:{message}} (intake-style)", () => {
    const err = new ApiError(400, { error: { message: "Body too large" } });
    expect(err.status).toBe(400);
    expect(err.message).toBe("Body too large");
  });

  it("extracts message from {detail: '<string>'} (contacts-style)", () => {
    const err = new ApiError(404, { detail: "Contact not found" });
    expect(err.message).toBe("Contact not found");
  });

  it("extracts message from {detail: {code, message}} (matches / admin)", () => {
    const err = new ApiError(409, {
      detail: { code: "campaign_has_active_enrollments", message: "Pause first" },
    });
    expect(err.message).toBe("Pause first");
  });

  it("falls back to detail.code when message is missing", () => {
    const err = new ApiError(409, { detail: { code: "no_steps" } });
    expect(err.message).toBe("no_steps");
  });

  it("falls back to HTTP <status> when body has none of the known shapes", () => {
    const err = new ApiError(500, { unrelated: "shape" });
    expect(err.message).toBe("HTTP 500");
  });

  it("falls back to HTTP <status> for a non-object body", () => {
    const err = new ApiError(502, "plain text body");
    expect(err.message).toBe("HTTP 502");
  });
});

describe("formatApiError", () => {
  it("prefixes the code for {detail: {code, message}} envelopes", () => {
    const err = new ApiError(409, {
      detail: { code: "campaign_has_active_enrollments", message: "Pause first" },
    });
    expect(formatApiError(err)).toBe(
      "campaign_has_active_enrollments: Pause first",
    );
  });

  it("returns the bare message for {detail: '<string>'}", () => {
    const err = new ApiError(404, { detail: "Contact not found" });
    expect(formatApiError(err)).toBe("Contact not found");
  });

  it("returns the bare message for {error:{message}}", () => {
    const err = new ApiError(400, { error: { message: "Body too large" } });
    expect(formatApiError(err)).toBe("Body too large");
  });

  it("returns 'Request failed' for non-Error values", () => {
    expect(formatApiError(undefined)).toBe("Request failed");
    expect(formatApiError(null)).toBe("Request failed");
    expect(formatApiError({ random: "object" })).toBe("Request failed");
  });

  it("returns the message for a plain Error", () => {
    expect(formatApiError(new Error("network down"))).toBe("network down");
  });
});

describe("resolveAccessToken (dev-local path)", () => {
  it("returns dev-local sentinel when nothing is set", async () => {
    const token = await resolveAccessToken(devCtx);
    expect(token).toBe("dev-local");
  });

  it("prefers the explicit devToken override on the context", async () => {
    const token = await resolveAccessToken({ ...devCtx, devToken: "override-token" });
    expect(token).toBe("override-token");
  });

  it("falls back to localStorage when override is empty", async () => {
    window.localStorage.setItem("jp_adopt_bearer", "stashed-token");
    const token = await resolveAccessToken(devCtx);
    expect(token).toBe("stashed-token");
  });

  it("trims whitespace from the localStorage value", async () => {
    window.localStorage.setItem("jp_adopt_bearer", "  padded-token  ");
    const token = await resolveAccessToken(devCtx);
    expect(token).toBe("padded-token");
  });
});

describe("apiFetch", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("sends Authorization header with the dev-local bearer", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await apiFetch(devCtx, "/v1/whatever");
    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.headers).toMatchObject({
      Authorization: "Bearer dev-local",
    });
  });

  it("returns the parsed JSON body on 200", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [1, 2] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const res = await apiFetch<{ items: number[] }>(devCtx, "/v1/items");
    expect(res).toEqual({ items: [1, 2] });
  });

  it("returns undefined on 204 No Content", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      new Response(null, { status: 204 }),
    );
    const res = await apiFetch(devCtx, "/v1/delete-me", { method: "DELETE" });
    expect(res).toBeUndefined();
  });

  it("throws ApiError with parsed body on a non-2xx JSON response", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({ detail: { code: "no_steps", message: "Add a step first" } }),
        { status: 409, headers: { "content-type": "application/json" } },
      ),
    );
    await expect(apiFetch(devCtx, "/v1/activate", { method: "POST" })).rejects.toMatchObject({
      status: 409,
      message: "Add a step first",
    });
  });

  it("appends query params from init.query (URL-encoding values)", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await apiFetch(devCtx, "/v1/search", {
      query: { q: "alice & bob", limit: 50 },
    });
    const [url] = fetchMock.mock.calls[0]!;
    expect(String(url)).toContain("q=alice+%26+bob");
    expect(String(url)).toContain("limit=50");
  });

  it("omits undefined query values", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await apiFetch(devCtx, "/v1/search", {
      query: { q: "alice", cursor: undefined },
    });
    const [url] = fetchMock.mock.calls[0]!;
    expect(String(url)).toContain("q=alice");
    expect(String(url)).not.toContain("cursor");
  });

  it("serializes a JSON body and sets content-type", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await apiFetch(devCtx, "/v1/things", {
      method: "POST",
      body: { name: "Alice" },
    });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.body).toBe(JSON.stringify({ name: "Alice" }));
    expect(init?.headers).toMatchObject({
      "Content-Type": "application/json",
    });
  });
});
