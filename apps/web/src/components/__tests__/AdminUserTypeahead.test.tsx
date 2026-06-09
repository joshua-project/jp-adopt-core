/**
 * Tests for the admin user typeahead (#97). The vitest worker hang
 * that bit the original landing of this file is closed by
 * ``pool: 'threads'`` + ``isolate: false`` in vitest.config — see
 * #106 for the diagnostic trail.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AdminUserTypeahead } from "../AdminUserTypeahead";

type FakeHit = {
  user_subject_id: string;
  display_name: string | null;
  user_principal_name: string | null;
  mail: string | null;
};

type FakeResponse = { items: FakeHit[]; graph_configured: boolean };

const AMY: FakeHit = {
  user_subject_id: "11111111-2222-3333-4444-555555555555",
  display_name: "Amy Adopter",
  user_principal_name: "amy@globalspecifics.com",
  mail: "amy@globalspecifics.com",
};

function fake(response: FakeResponse | Error) {
  return vi.fn(async () => {
    if (response instanceof Error) throw response;
    return response;
  });
}

describe("AdminUserTypeahead", () => {
  it("does not call the search backend until the query is at least 2 characters", async () => {
    const searchFn = fake({ items: [], graph_configured: true });
    render(
      <AdminUserTypeahead
        value=""
        onChange={() => {}}
        debounceMs={0}
        searchFn={searchFn}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), "a");
    expect(searchFn).not.toHaveBeenCalled();
  });

  it("searches and surfaces hits", async () => {
    const searchFn = fake({ items: [AMY], graph_configured: true });
    render(
      <AdminUserTypeahead
        value=""
        onChange={() => {}}
        debounceMs={0}
        searchFn={searchFn}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), "amy");
    expect(await screen.findByText("Amy Adopter")).toBeInTheDocument();
    expect(screen.getByText("amy@globalspecifics.com")).toBeInTheDocument();
    expect(searchFn.mock.calls.at(-1)?.[1]).toBe("amy");
  });

  it("reports the picked OID and display name to the parent", async () => {
    const searchFn = fake({ items: [AMY], graph_configured: true });
    const onChange = vi.fn();
    const onDisplayChange = vi.fn();
    render(
      <AdminUserTypeahead
        value=""
        onChange={onChange}
        onDisplayChange={onDisplayChange}
        debounceMs={0}
        searchFn={searchFn}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), "amy");
    await userEvent.click(await screen.findByRole("option"));
    expect(onChange).toHaveBeenCalledWith(AMY.user_subject_id);
    expect(onDisplayChange).toHaveBeenCalledWith({
      name: "Amy Adopter",
      upn: "amy@globalspecifics.com",
    });
  });

  it("shows the Graph-unconfigured notice when the API reports so", async () => {
    const searchFn = fake({ items: [], graph_configured: false });
    render(
      <AdminUserTypeahead
        value=""
        onChange={() => {}}
        debounceMs={0}
        searchFn={searchFn}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), "amy");
    await waitFor(() =>
      expect(
        screen.getByText(/Graph user search isn['’]t wired/i),
      ).toBeInTheDocument(),
    );
  });

  it("captures a raw OID typed in directly", async () => {
    const onChange = vi.fn();
    render(
      <AdminUserTypeahead
        value=""
        onChange={onChange}
        debounceMs={0}
        searchFn={fake({ items: [], graph_configured: true })}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), AMY.user_subject_id);
    expect(onChange).toHaveBeenLastCalledWith(AMY.user_subject_id);
  });

  it("clears the picked selection on Clear", async () => {
    const searchFn = fake({ items: [AMY], graph_configured: true });
    const onChange = vi.fn();
    render(
      <AdminUserTypeahead
        value=""
        onChange={onChange}
        debounceMs={0}
        searchFn={searchFn}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), "amy");
    await userEvent.click(await screen.findByRole("option"));
    expect(onChange).toHaveBeenLastCalledWith(AMY.user_subject_id);
    await userEvent.click(screen.getByRole("button", { name: /clear/i }));
    expect(onChange).toHaveBeenLastCalledWith("");
  });

  it("does not propagate non-UUID free text as the OID", async () => {
    const onChange = vi.fn();
    render(
      <AdminUserTypeahead
        value=""
        onChange={onChange}
        debounceMs={0}
        searchFn={fake({ items: [], graph_configured: true })}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), "amy");
    expect(onChange).toHaveBeenLastCalledWith("");
  });

  it("surfaces a search error inline", async () => {
    const searchFn = fake(new Error("Graph offline"));
    render(
      <AdminUserTypeahead
        value=""
        onChange={() => {}}
        debounceMs={0}
        searchFn={searchFn}
      />,
    );
    await userEvent.type(screen.getByRole("combobox"), "amy");
    await waitFor(() =>
      expect(screen.getByText("Graph offline")).toBeInTheDocument(),
    );
  });
});
