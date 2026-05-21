import type { Config } from "tailwindcss";

/**
 * Tailwind theme extension for the JP Adopt staff console.
 *
 * The `jp` color palette mirrors the brand tokens declared in
 * app/globals.css (which in turn mirror the WordPress D.T. theme). Where
 * possible, code references the semantic color name (`jp-accent`,
 * `jp-accent-teal`) rather than the literal hex, so the brand can move
 * without touching components.
 */
const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx}",
    "./components/**/*.{js,ts,jsx,tsx}",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        jp: {
          bg: "#ffffff",
          surface: "#f3f4f6",
          border: "#e5e7eb",
          fg: "#1f2937",
          muted: "#6b7280",
          subtle: "#9ca3af",
          accent: "#eb5f1e",
          "accent-hover": "#d4521a",
          "accent-teal": "#2c474b",
          "accent-gold": "#f4b435",
          "accent-green": "#10b981",
          cream: "#fff7ed",
          error: "#ef4444",
          nav: "#303030",
          "nav-hover": "#636363",
        },
      },
      fontFamily: {
        heading: [
          "var(--font-heading)",
          "Inter Tight",
          "Segoe UI",
          "Arial",
          "sans-serif",
        ],
        body: [
          "var(--font-body)",
          "Open Sans",
          "Segoe UI",
          "Arial",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};

export default config;
