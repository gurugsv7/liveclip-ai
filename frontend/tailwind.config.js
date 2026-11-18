/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#eeeae0", 900: "#f4f0e7", 850: "#f8f4eb", 800: "#e7e1d5", 750: "#cfc8bb", 700: "#c4bcad", 600: "#a79e8e", 500: "#716959", 400: "#605a4d", 300: "#4a493e", 200: "#33372e", 100: "#20231f" },
        violet: { 400: "#982e22", 500: "#b53626", 600: "#972a1d", 700: "#7d2219" },
        volt: { 400: "#4e5b39", 500: "#66754d" },
        live: "#b53626",
        ok: "#4e653b",
        warn: "#825015",
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "sans-serif"],
        display: ["'Roboto Slab'", "Rockwell", "Georgia", "serif"],
        mono: ["'JetBrains Mono'", "Consolas", "monospace"],
      },
      boxShadow: {
        panel: "none",
        glow: "0 0 0 1px #b53626",
      },
      keyframes: {
        pulseDot: { "0%,100%": { opacity: 1 }, "50%": { opacity: 0.35 } },
        slideIn: { from: { opacity: 0, transform: "translateY(6px)" }, to: { opacity: 1, transform: "none" } },
      },
      animation: { pulseDot: "pulseDot 1.6s ease-in-out infinite", slideIn: "slideIn .18s ease-out" },
    },
  },
  plugins: [],
};
