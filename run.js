/**
 * 前后端一键启动脚本
 *
 * 用法:
 *   node run.js            - 同时启动前后端
 *   node run.js backend    - 只启动后端
 *   node run.js frontend   - 只启动前端
 *
 * 选项:
 *   --no-migrate  - 数据库 schema 落后时只报告，不执行迁移（默认会在交互终端里询问）
 */

const { spawn, spawnSync } = require("child_process");
const path = require("path");
const os = require("os");
const readline = require("readline");

// ANSI 颜色代码
const colors = {
  reset: "\x1b[0m",
  bright: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  blue: "\x1b[34m",
  magenta: "\x1b[35m",
  cyan: "\x1b[36m",
};

// 日志输出函数
function log(prefix, message, color = colors.reset) {
  const timestamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  console.log(`${color}[${timestamp}] [${prefix}]${colors.reset} ${message}`);
}

const BACKEND_DIR = path.join(__dirname, "back-end");

// 检测 Python 命令（Windows 上可能是 python 或 py）
const PYTHON_CMD = os.platform() === "win32" ? "python" : "python3";

// ---------- 数据库 schema 版本判断 ----------

function firstLine(buffer) {
  return buffer ? buffer.toString("utf8").trim().split("\n")[0] : "";
}

function checkSchemaVersion() {
  const result = spawnSync(
    PYTHON_CMD,
    [path.join("scripts", "check_schema_drift.py")],
    {
      cwd: BACKEND_DIR,
      shell: true,
      encoding: "buffer",
      timeout: 60000,
    },
  );

  let info = null;
  try {
    info = JSON.parse(result.stdout.toString("utf8").trim());
  } catch {
    // 脚本没跑起来（Python 缺失、依赖装坏），拿不到结论
  }

  if (!info) {
    return {
      status: "check-unavailable",
      detail:
        firstLine(result.stderr) || (result.error && result.error.message) || "无输出",
    };
  }
  if (info.status === "error") {
    return { status: "check-unavailable", detail: info.detail };
  }
  return info;
}

function runMigration() {
  const result = spawnSync(PYTHON_CMD, ["-m", "alembic", "upgrade", "head"], {
    cwd: BACKEND_DIR,
    shell: true,
    encoding: "buffer",
    timeout: 300000,
  });

  return {
    ok: !result.error && result.status === 0,
    output: `${result.stdout ? result.stdout.toString("utf8") : ""}${
      result.stderr ? result.stderr.toString("utf8") : ""
    }`,
  };
}

function ask(question) {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  return new Promise((resolve) =>
    rl.question(question, (answer) => {
      rl.close();
      resolve(answer.trim().toLowerCase());
    }),
  );
}

/**
 * 起后端前判断 schema 版本。
 *
 * 必须在 spawn uvicorn 之前：init_db() 见到 alembic_version 就不再建表，所以库落后于
 * 代码时后端照样启动成功，只在碰新表的路径上报 table doesn't exist。
 * 判断本身失败不阻塞启动——拿不到结论不等于库有问题。
 */
async function ensureDatabaseUpToDate(allowMigrate) {
  log("数据库", "检查 schema 版本...", colors.blue);
  const info = checkSchemaVersion();

  if (info.status === "up-to-date") {
    log("数据库", `schema 已是最新（${info.current}）`, colors.green);
    return;
  }
  if (info.status === "unmanaged") {
    log(
      "数据库",
      "库尚未纳入迁移管理，首次启动时后端会自建表并 stamp，无需迁移",
      colors.green,
    );
    return;
  }
  if (info.status === "branch") {
    const heads = info.heads || [];
    log(
      "数据库",
      `迁移链有 ${heads.length} 个 head，alembic 无法判断顺序，先合并分支再启动`,
      colors.red,
    );
    heads.forEach((head) => log("数据库", `  ${head}`, colors.red));
    return;
  }
  if (info.status === "unknown-revision") {
    log(
      "数据库",
      `库里的版本 ${info.current} 不在当前代码的迁移链里（代码回滚过，或手工 stamp 过）`,
      colors.red,
    );
    log(
      "数据库",
      "手工确认：cd back-end && python -m alembic current / heads",
      colors.yellow,
    );
    return;
  }
  if (info.status === "unreachable" || info.status === "check-unavailable") {
    const reason = info.status === "unreachable" ? "连不上数据库" : "迁移判断不可用";
    log(
      "数据库",
      `${reason}：${info.detail || "无详情"}（跳过判断，继续启动）`,
      colors.yellow,
    );
    return;
  }
  if (info.status !== "behind") {
    log("数据库", `未知的检查结果 ${info.status}，继续启动`, colors.yellow);
    return;
  }

  const pending = info.pending || [];
  log(
    "数据库",
    `schema 落后 ${pending.length} 个迁移：${info.current} → ${info.head}`,
    colors.bright + colors.yellow,
  );
  pending.forEach((item) =>
    log("数据库", `  ${item.revision}  ${item.message}`, colors.yellow),
  );
  log(
    "数据库",
    "这些迁移建的表还不存在，后端能启动，但碰它们的路径会 table doesn't exist",
    colors.yellow,
  );

  const manual = "手工执行：cd back-end && python -m alembic upgrade head";

  if (!allowMigrate) {
    log("数据库", `已按 --no-migrate 跳过执行。${manual}`, colors.yellow);
    return;
  }
  if (!process.stdin.isTTY) {
    log("数据库", `非交互终端，不自动改 schema。${manual}`, colors.yellow);
    return;
  }

  const answer = await ask("现在执行 alembic upgrade head 吗？[Y/n] ");
  if (answer && answer !== "y" && answer !== "yes") {
    log("数据库", `已取消，带着缺表启动。${manual}`, colors.yellow);
    return;
  }

  log("数据库", "执行 alembic upgrade head ...", colors.blue);
  const migrated = runMigration();
  if (!migrated.ok) {
    log(
      "数据库",
      "迁移失败，schema 可能停在中间状态，先修迁移再看接口",
      colors.red,
    );
    migrated.output
      .trim()
      .split("\n")
      .slice(-8)
      .forEach((line) => log("数据库", line, colors.red));
    return;
  }
  log("数据库", `迁移完成：${info.current} → ${info.head}`, colors.green);
}

// 启动后端服务
function startBackend() {
  return new Promise((resolve, reject) => {
    log("后端", "正在启动...", colors.blue);
    // uvicorn main:app --reload --port 3000
    const backend = spawn(
      PYTHON_CMD,
      ["-m", "uvicorn", "main:app", "--reload", "--port", "3000"],
      {
        cwd: BACKEND_DIR,
        shell: true,
        stdio: "pipe",
      },
    );

    // 监听输出
    backend.stdout.on("data", (data) => {
      const output = data.toString().trim();
      if (output) {
        log("后端", output, colors.blue);
      }
      // 检测启动成功信号
      if (
        output.includes("Uvicorn running") ||
        output.includes("Application startup complete")
      ) {
        resolve(backend);
      }
    });

    backend.stderr.on("data", (data) => {
      const output = data.toString().trim();
      if (output) {
        // 某些正常日志也会输出到 stderr
        if (output.includes("INFO") || output.includes("WARNING")) {
          log("后端", output, colors.yellow);
        } else {
          log("后端", output, colors.red);
        }
      }
    });

    backend.on("error", (error) => {
      log("后端", `启动失败: ${error.message}`, colors.red);
      reject(error);
    });

    backend.on("close", (code) => {
      if (code !== 0 && code !== null) {
        log("后端", `进程退出，代码: ${code}`, colors.red);
      }
    });

    // 超时处理
    setTimeout(() => {
      log("后端", "启动完成（可能需要几秒钟加载）", colors.green);
      resolve(backend);
    }, 3000);
  });
}

// 启动前端服务
function startFrontend() {
  return new Promise((resolve, reject) => {
    log("前端", "正在启动...", colors.cyan);

    const frontendDir = path.join(__dirname, "front-end");

    // 检测包管理器
    let packageManager = "pnpm";

    // pnpm dev
    const frontend = spawn(packageManager, ["dev"], {
      cwd: frontendDir,
      shell: true,
      stdio: "pipe",
    });

    // 监听输出
    frontend.stdout.on("data", (data) => {
      const output = data.toString().trim();
      if (output) {
        log("前端", output, colors.cyan);
      }
      // 检测启动成功信号
      if (output.includes("ready in") || output.includes("Local:")) {
        resolve(frontend);
      }
    });

    frontend.stderr.on("data", (data) => {
      const output = data.toString().trim();
      if (output) {
        // Vite 的某些日志也会输出到 stderr
        if (output.includes("error") || output.includes("Error")) {
          log("前端", output, colors.red);
        } else {
          log("前端", output, colors.yellow);
        }
      }
    });

    frontend.on("error", (error) => {
      log("前端", `启动失败: ${error.message}`, colors.red);
      reject(error);
    });

    frontend.on("close", (code) => {
      if (code !== 0 && code !== null) {
        log("前端", `进程退出，代码: ${code}`, colors.red);
      }
    });

    // 超时处理
    setTimeout(() => {
      log("前端", "启动完成", colors.green);
      resolve(frontend);
    }, 5000);
  });
}

// 主函数
async function main() {
  const args = process.argv.slice(2);
  const flags = args.filter((arg) => arg.startsWith("--"));
  const mode = args.find((arg) => !arg.startsWith("--")) || "all";
  const noMigrate = flags.includes("--no-migrate");

  const processes = [];

  console.log(
    "\n" + colors.bright + colors.green + "=".repeat(60) + colors.reset,
  );
  console.log(colors.bright + "🚀  AI Workspace 启动器" + colors.reset);
  console.log(
    colors.bright + colors.green + "=".repeat(60) + colors.reset + "\n",
  );

  try {
    if (!["all", "backend", "frontend"].includes(mode)) {
      log(
        "系统",
        `未知模式 "${mode}"，可用：all（默认）/ backend / frontend，选项 --no-migrate`,
        colors.red,
      );
      process.exit(1);
    }

    // 启动后端
    if (mode === "all" || mode === "backend") {
      await ensureDatabaseUpToDate(!noMigrate);

      const backend = await startBackend();
      processes.push(backend);
      log("系统", "后端服务已启动: http://localhost:3000", colors.green);
      log("系统", "API 文档: http://localhost:3000/docs", colors.green);
    }

    // 启动前端
    if (mode === "all" || mode === "frontend") {
      // 如果是 all 模式，等待后端先启动
      if (mode === "all") {
        log("系统", "等待后端完全启动...", colors.yellow);
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }

      const frontend = await startFrontend();
      processes.push(frontend);
      log("系统", "前端应用已启动 (Electron 窗口将自动打开)", colors.green);
    }

    console.log(
      "\n" + colors.bright + colors.green + "=".repeat(60) + colors.reset,
    );
    log("系统", "所有服务已启动完成！", colors.bright + colors.green);
    log("系统", "按 Ctrl+C 停止所有服务", colors.yellow);
    console.log(
      colors.bright + colors.green + "=".repeat(60) + colors.reset + "\n",
    );
  } catch (error) {
    log("系统", `启动失败: ${error.message}`, colors.red);
    // 清理已启动的进程
    processes.forEach((proc) => {
      if (proc && !proc.killed) {
        proc.kill();
      }
    });
    process.exit(1);
  }

  // 处理退出信号
  const cleanup = () => {
    console.log("\n");
    log("系统", "正在停止所有服务...", colors.yellow);

    processes.forEach((proc, index) => {
      if (proc && !proc.killed) {
        const serviceName = index === 0 ? "后端" : "前端";
        log(serviceName, "正在停止...", colors.yellow);
        proc.kill("SIGTERM");

        // 强制终止超时处理
        setTimeout(() => {
          if (!proc.killed) {
            proc.kill("SIGKILL");
          }
        }, 3000);
      }
    });

    setTimeout(() => {
      log("系统", "所有服务已停止", colors.green);
      process.exit(0);
    }, 1000);
  };

  // 监听退出信号
  process.on("SIGINT", cleanup); // Ctrl+C
  process.on("SIGTERM", cleanup); // kill 命令

  // Windows 特殊处理
  if (os.platform() === "win32") {
    readline
      .createInterface({
        input: process.stdin,
        output: process.stdout,
      })
      .on("SIGINT", cleanup);
  }
}

// 错误处理
process.on("uncaughtException", (error) => {
  log("系统", `未捕获的异常: ${error.message}`, colors.red);
  process.exit(1);
});

process.on("unhandledRejection", (reason, promise) => {
  log("系统", `未处理的 Promise 拒绝: ${reason}`, colors.red);
  process.exit(1);
});

// 运行
if (require.main === module) {
  main().catch((error) => {
    log("系统", `运行失败: ${error.message}`, colors.red);
    process.exit(1);
  });
}

module.exports = {
  startBackend,
  startFrontend,
  checkSchemaVersion,
  ensureDatabaseUpToDate,
};
