/**
 * Tailwind 只做一件事：把 src/app/styles/index.css 里的 token 映射成类名。
 *
 * 这里**不写颜色字面值**。上一版在这个文件里硬编码了 `background:#090d16`、
 * `card:#111827`、`border:#1f2937` 三个深色值，而设计系统的 token 早就换成了
 * 暖纸色系——两套数字同时有效，谁赢取决于组件写的是 `bg-background` 还是
 * `bg-surface`，表现是同一个界面里两种底色，而且深色 token 一改它就不同步。
 *
 * 也不要用 `bg-ink/10` 这类透明度修饰：token 变量是完整的色值（hex/rgba），
 * Tailwind 没法算它的 alpha，写出来的类会静默失效。需要半透明底时用
 * `--c-*-faint` 那几个专为这个用途准备的 token。
 */
/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        crust: "var(--c-crust)",
        mantle: "var(--c-mantle)",
        surface: "var(--c-surface)",
        overlay: "var(--c-overlay)",
        ink: {
          DEFAULT: "var(--c-ink)",
          soft: "var(--c-ink-soft)",
          faint: "var(--c-ink-faint)",
        },
        line: {
          DEFAULT: "var(--c-line)",
          strong: "var(--c-line-strong)",
        },
        accent: {
          DEFAULT: "var(--c-accent)",
          deep: "var(--c-accent-deep)",
          faint: "var(--c-accent-faint)",
        },
        // 工单状态与权限档位。名字刻意分两组：state 说"这件事进行到哪"，
        // tier 说"这一步有多大权力"，两者可以同屏出现而不互相混淆
        state: {
          idle: "var(--c-idle)",
          active: "var(--c-active)",
          hold: "var(--c-hold)",
          done: "var(--c-done)",
          bad: "var(--c-bad)",
        },
        tier: {
          read: "var(--tier-read)",
          mutate: "var(--tier-mutate)",
          fund: "var(--tier-fund)",
        },
      },
      fontFamily: {
        display: ["var(--font-display)"],
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      boxShadow: {
        card: "var(--card-shadow)",
        "card-hover": "var(--card-shadow-hover)",
        accent: "var(--accent-glow)",
      },
    },
  },
  plugins: [],
};
