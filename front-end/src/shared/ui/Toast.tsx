import React, {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";

type ToastKind = "success" | "error" | "info";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
  leaving: boolean;
}

export interface ToastApi {
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/** 错误 toast 的文案优先用后端 detail（client.ts 已提取进 Error.message） */
export const toastMessageFrom = (e: unknown, fallback: string): string =>
  e instanceof Error && e.message ? e.message : fallback;

// 错误类停留更久，给用户足够时间读完
const DURATION: Record<ToastKind, number> = {
  success: 2500,
  info: 3500,
  error: 5000,
};
// 同屏最多保留的条数（新 toast 挤掉最旧的）
const MAX_VISIBLE = 4;
// 离场动画时长，与 .anim-toast-out 的 keyframes 保持一致
const LEAVE_MS = 200;

const KIND_STYLES: Record<
  ToastKind,
  { icon: React.ReactNode; iconClass: string; borderClass: string }
> = {
  success: {
    icon: <CheckCircle2 className="w-4 h-4 shrink-0" />,
    iconClass: "text-emerald-500",
    borderClass: "border-emerald-500/30",
  },
  error: {
    icon: <AlertCircle className="w-4 h-4 shrink-0" />,
    iconClass: "text-rose-500",
    borderClass: "border-rose-500/30",
  },
  info: {
    icon: <Info className="w-4 h-4 shrink-0" />,
    iconClass: "text-[#da7756]",
    borderClass: "border-[#da7756]/30",
  },
};

export const useToast = (): ToastApi => {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 <ToastProvider> 内使用");
  return ctx;
};

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const idRef = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) =>
      prev.map((t) => (t.id === id ? { ...t, leaving: true } : t)),
    );
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, LEAVE_MS);
  }, []);

  const push = useCallback(
    (kind: ToastKind, message: string) => {
      const id = ++idRef.current;
      setToasts((prev) => [...prev, { id, kind, message, leaving: false }].slice(-MAX_VISIBLE));
      window.setTimeout(() => dismiss(id), DURATION[kind]);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      success: (message) => push("success", message),
      error: (message) => push("error", message),
      info: (message) => push("info", message),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* 容器常驻 DOM，插入的 toast 由 aria-live 播报；错误再叠加 role=alert 提升优先级 */}
      <div
        className="fixed bottom-5 right-5 z-[100] flex flex-col items-end gap-2 pointer-events-none"
        aria-live="polite"
        aria-label="通知"
      >
        {toasts.map((t) => {
          const style = KIND_STYLES[t.kind];
          return (
            <div
              key={t.id}
              role={t.kind === "error" ? "alert" : undefined}
              className={`pointer-events-auto flex items-start gap-2.5 min-w-[240px] max-w-[380px] rounded-xl border px-3.5 py-3 shadow-lg bg-white dark:bg-[#1e1d1b] text-[#1f1e1d] dark:text-[#edece8] ${style.borderClass} ${
                t.leaving ? "anim-toast-out" : "anim-toast-in"
              }`}
            >
              <span className={style.iconClass}>{style.icon}</span>
              <span className="text-xs leading-relaxed flex-1 pt-px break-all">
                {t.message}
              </span>
              <button
                onClick={() => dismiss(t.id)}
                aria-label="关闭通知"
                className="p-0.5 rounded text-[#918d83] hover:text-[#1f1e1d] dark:text-[#78756d] dark:hover:text-[#edece8] hover:bg-[#eae6db] dark:hover:bg-[#262522] transition-colors shrink-0"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
};
