import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0a1520",
        panel: "#102231",
        mist: "#d8e6f2",
        accent: "#8fd0ff",
        accentStrong: "#5db8ff",
        line: "rgba(255,255,255,0.08)"
      },
      boxShadow: {
        glow: "0 24px 80px rgba(0, 0, 0, 0.28)"
      },
      borderRadius: {
        xl2: "1.5rem"
      },
      backgroundImage: {
        "hero-fade":
          "radial-gradient(circle at top left, rgba(143, 208, 255, 0.24), transparent 28%), radial-gradient(circle at 80% 0%, rgba(76, 125, 255, 0.18), transparent 26%), linear-gradient(135deg, #08131d 0%, #0e2030 45%, #17324a 100%)"
      }
    }
  },
  plugins: []
} satisfies Config;

