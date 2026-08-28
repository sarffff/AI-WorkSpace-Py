import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

/**
 * 测试用的独立配置，不复用 vite.config.ts。
 *
 * 原因是 vite.config.ts 里挂了 vite-plugin-electron：它在配置加载阶段就会去
 * 启动 Electron 主进程。跑测试时那个进程既没用又不会自己退出，表现为
 * `vitest run` 卡住不返回（CI 里就是任务超时，而日志里看不出原因）。
 *
 * 别名要跟 vite.config.ts 保持一致——不一致的话 `@/` 开头的 import 在测试里
 * 解析不到，报错是 "Failed to resolve import"，看起来像源码写错了。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    // jsdom 只给需要 DOM 的用例用；纯逻辑用例本来不需要，但统一环境比
    // 每个文件顶部写 @vitest-environment 更少踩坑。
    environment: "jsdom",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // electron/ 与 dist/ 下没有可测的东西，且 electron 目录 import 了 node 侧模块
    exclude: ["node_modules/**", "dist/**", "dist-electron/**", "release/**"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary"],
      include: ["src/**/*.{ts,tsx}"],
      // 类型声明与入口文件没有可执行分支，计进覆盖率只会稀释真实数字
      exclude: [
        "src/**/*.d.ts",
        "src/main.tsx",
        "src/**/index.ts",
        "src/shared/types/**",
      ],
    },
  },
});
