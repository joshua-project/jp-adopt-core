import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx}",
    "./components/**/*.{js,ts,jsx,tsx}",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        heading: ["var(--font-heading)", "Inter Tight", "Arial", "sans-serif"],
        body: ["var(--font-body)", "Open Sans", "Arial", "sans-serif"],
      },
    },
  },
  plugins: [],
};

export default config;
