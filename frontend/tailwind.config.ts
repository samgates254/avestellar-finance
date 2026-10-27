import type { Config } from "tailwindcss";
const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: { extend: { colors: { ink: "#17251f", muted: "#718078", canvas: "#f5f7f4", line: "#e5ebe6", forest: "#176b4b", mint: "#e8f4ed" }, boxShadow: { card: "0 2px 12px rgba(20, 45, 32, .045)" } } },
  plugins: []
};
export default config;
