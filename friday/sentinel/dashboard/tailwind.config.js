/* Tailwind 3.4 configuration for the sentinel dashboard. Build with deploy/build_css.sh;
   the output (tailwind.css) is committed so nothing is compiled at deploy time. */
module.exports = {
  content: ["./*.html", "./*.js", "./views/*.js"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["ui-monospace", "SF Mono", "JetBrains Mono", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
