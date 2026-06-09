import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CodeChip, StatusBadge } from "../StatusBadge";

describe("StatusBadge", () => {
  it("renders the humanized adopter status by default", () => {
    render(<StatusBadge status="matched" />);
    expect(screen.getByText("Matched")).toBeInTheDocument();
  });

  it("uses the kind-specific label when kind is passed", () => {
    render(<StatusBadge status="paused" kind="campaign" />);
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });

  it("falls back to slate tone for an unknown status", () => {
    render(<StatusBadge status="brand_new_value" />);
    const el = screen.getByText("Brand new value");
    // Slate variant uses bg-slate-100; presence of that class proves the
    // fallback path was taken.
    expect(el.className).toContain("bg-slate-100");
  });

  it("uses the rose tone for do_not_engage", () => {
    render(<StatusBadge status="do_not_engage" />);
    const el = screen.getByText("Opted out");
    expect(el.className).toContain("bg-rose-50");
  });

  it("uses the green tone for active campaigns", () => {
    render(<StatusBadge status="active" kind="campaign" />);
    const el = screen.getByText("Active");
    expect(el.className).toContain("bg-emerald-50");
  });

  it("renders children verbatim when provided (skipping label lookup)", () => {
    render(<StatusBadge status="active">Custom Label</StatusBadge>);
    expect(screen.getByText("Custom Label")).toBeInTheDocument();
  });

  it("respects an explicit tone override", () => {
    render(<StatusBadge status="active" tone="teal" />);
    const el = screen.getByText("Active adoption");
    expect(el.className).toContain("bg-teal-50");
  });
});

describe("CodeChip", () => {
  it("renders the child content in a mono-styled chip", () => {
    render(<CodeChip>AAA01</CodeChip>);
    const el = screen.getByText("AAA01");
    expect(el.className).toContain("font-mono");
  });
});
