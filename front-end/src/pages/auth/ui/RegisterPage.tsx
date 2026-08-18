import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDispatch } from "react-redux";
import { apiClient } from "@/shared/api/client";
import { setUser, setToken } from "@/entities/auth/model/authSlice";
import { useTheme } from "@/shared/lib/ThemeContext";
import { BrandMark } from "@/shared/ui/BrandMark";
import {
  Mail,
  Lock,
  User as UserIcon,
  AlertCircle,
  Eye,
  EyeOff,
  Github,
  Chrome,
  Sun,
  Moon,
  KeyRound,
} from "lucide-react";

export const RegisterPage: React.FC = () => {
  const navigate = useNavigate();
  const dispatch = useDispatch();
  const { theme, toggleTheme } = useTheme();

  const [formData, setFormData] = useState({
    email: "",
    username: "",
    password: "",
    name: "",
    inviteCode: "",
  });
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [passwordStrength, setPasswordStrength] = useState({
    score: 0,
    label: "",
    color: "",
  });

  const checkPasswordStrength = (password: string) => {
    let score = 0;
    if (password.length >= 6) score++;
    if (password.length >= 8) score++;
    if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++;
    if (/\d/.test(password)) score++;
    if (/[^a-zA-Z\d]/.test(password)) score++;

    if (score <= 1) return { score, label: "弱", color: "bg-rose-500" };
    if (score <= 3) return { score, label: "中等", color: "bg-amber-500" };
    return { score, label: "强", color: "bg-emerald-500" };
  };

  const handlePasswordChange = (password: string) => {
    setFormData({ ...formData, password });
    setPasswordStrength(checkPasswordStrength(password));
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (formData.password.length < 8) {
      setError("密码至少需要8个字符");
      return;
    }
    if (!/[a-z]/.test(formData.password)) {
      setError("密码必须包含至少一个小写字母");
      return;
    }
    if (!/[A-Z]/.test(formData.password)) {
      setError("密码必须包含至少一个大写字母");
      return;
    }
    if (!/\d/.test(formData.password)) {
      setError("密码必须包含至少一个数字");
      return;
    }

    if (!/^[a-zA-Z0-9_-]+$/.test(formData.username)) {
      setError("用户名只能包含字母、数字、下划线和横线");
      return;
    }

    setLoading(true);

    try {
      const response = await apiClient.register({
        email: formData.email,
        username: formData.username,
        password: formData.password,
        name: formData.name || formData.username,
        inviteCode: formData.inviteCode.trim() || undefined,
      });

      dispatch(setToken(response.access_token));
      dispatch(setUser(response.user));
      localStorage.setItem("user", JSON.stringify(response.user));
      navigate("/chat");
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail;
      if (typeof detail === "string") {
        setError(detail);
      } else {
        setError("注册失败,请稍后重试");
      }
    } finally {
      setLoading(false);
    }
  };

  const handleOAuthRegister = (provider: "github" | "google") => {
    alert(
      `${provider === "github" ? "GitHub" : "Google"} 注册功能展示\n\n实际项目中需要配置 OAuth 应用`,
    );
  };

  return (
    <div className="min-h-screen app-atmosphere flex transition-colors duration-200">
      <div className="absolute top-6 right-6 z-20">
        <button
          onClick={toggleTheme}
          className="p-2.5 rounded-full bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#1e1d1b] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#1f1e1d] dark:text-[#edece8] transition-all shadow-md"
        >
          {theme === "dark" ? <Sun className="w-4 h-4 text-amber-400" /> : <Moon className="w-4 h-4 text-slate-700" />}
        </button>
      </div>

      <aside className="hidden lg:flex w-[44%] relative flex-col justify-between p-12 border-r border-[#e6e2d8] dark:border-[#282724]">
        <div className="absolute inset-0 pointer-events-none overflow-hidden">
          <div className="absolute inset-0 lab-grid" />
        </div>
        <div className="relative z-10">
          <div className="flex items-center gap-3 mb-10">
            <BrandMark size={40} />
            <span className="font-display text-lg font-semibold">AI Workspace</span>
          </div>
          <p className="label-eyebrow mb-3">New bench</p>
          <h1 className="font-display text-[40px] leading-[1.15] font-semibold text-[#1f1e1d] dark:text-[#edece8] text-balance">
            搭一张<br />能核对的台子
          </h1>
          <p className="mt-4 text-sm text-[#6e6b63] dark:text-[#a19f96] max-w-sm leading-relaxed">
            注册之后就能上传文档、挂提示词版本、回放每一次回答。这里练的是工程，不是聊天。
          </p>
        </div>
        <div className="relative z-10 space-y-3">
          {["文档进库即可引用", "工具轨迹跨回合回灌", "差评能变成回归用例"].map(
            (line, i) => (
              <div
                key={line}
                className="flex items-center gap-2.5 text-sm text-[#1f1e1d] dark:text-[#edece8] anim-fade-up"
                style={{ animationDelay: `${0.12 + i * 0.07}s` }}
              >
                <span className="capability-dot" />
                {line}
              </div>
            ),
          )}
        </div>
      </aside>

      <div className="flex-1 flex items-center justify-center p-6 relative overflow-y-auto">
        <div className="relative w-full max-w-md my-8">
          <div className="lg:hidden text-center mb-8">
            <BrandMark size={48} className="mx-auto mb-3 !rounded-[16px]" />
            <h1 className="font-display text-[26px] font-semibold">创建账号</h1>
          </div>
          <div className="hidden lg:block mb-6 anim-fade-up">
            <h2 className="font-display text-[28px] font-semibold">创建账号</h2>
            <p className="text-sm text-[#6e6b63] dark:text-[#a19f96] mt-1">
              开始使用工作台，体验可核对的智能对话。
            </p>
          </div>

          <div className="card-surface rounded-2xl p-8 relative z-10 anim-fade-up stagger-1">
          <form onSubmit={handleRegister} className="space-y-4">
            {/* 错误提示 */}
            {error && (
              <div className="flex items-center gap-2 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-sm">
                <AlertCircle className="w-4 h-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {/* 邮箱输入 */}
            <div>
              <label className="block text-xs font-semibold text-[#6e6b63] dark:text-[#a19f96] uppercase tracking-wider mb-2">
                邮箱地址 <span className="text-[#da7756]">*</span>
              </label>
              <div className="relative">
                <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#918d83] dark:text-[#78756d]" />
                <input
                  type="email"
                  value={formData.email}
                  onChange={(e) =>
                    setFormData({ ...formData, email: e.target.value })
                  }
                  placeholder="your@email.com"
                  required
                  className="w-full pl-10 pr-4 py-2.5 bg-[#fbf9f5] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] dark:placeholder-[#78756d] focus:outline-none focus:ring-2 focus:ring-[#da7756] focus:border-transparent transition-all text-sm"
                />
              </div>
            </div>

            {/* 用户名输入 */}
            <div>
              <label className="block text-xs font-semibold text-[#6e6b63] dark:text-[#a19f96] uppercase tracking-wider mb-2">
                用户名 <span className="text-[#da7756]">*</span>
              </label>
              <div className="relative">
                <UserIcon className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#918d83] dark:text-[#78756d]" />
                <input
                  type="text"
                  value={formData.username}
                  onChange={(e) =>
                    setFormData({ ...formData, username: e.target.value })
                  }
                  placeholder="username"
                  required
                  minLength={3}
                  maxLength={50}
                  pattern="^[a-zA-Z0-9_-]+$"
                  className="w-full pl-10 pr-4 py-2.5 bg-[#fbf9f5] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] dark:placeholder-[#78756d] focus:outline-none focus:ring-2 focus:ring-[#da7756] focus:border-transparent transition-all text-sm"
                />
              </div>
              <p className="mt-1 text-[11px] text-[#918d83]">
                3-50个字符,只能包含字母、数字、下划线和横线
              </p>
            </div>

            {/* 密码输入 */}
            <div>
              <label className="block text-xs font-semibold text-[#6e6b63] dark:text-[#a19f96] uppercase tracking-wider mb-2">
                密码 <span className="text-[#da7756]">*</span>
              </label>
              <div className="relative">
                <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#918d83] dark:text-[#78756d]" />
                <input
                  type={showPassword ? "text" : "password"}
                  value={formData.password}
                  onChange={(e) => handlePasswordChange(e.target.value)}
                  placeholder="至少8个字符,须含大小写字母和数字"
                  required
                  minLength={8}
                  className="w-full pl-10 pr-12 py-2.5 bg-[#fbf9f5] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] dark:placeholder-[#78756d] focus:outline-none focus:ring-2 focus:ring-[#da7756] focus:border-transparent transition-all text-sm"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3.5 top-1/2 -translate-y-1/2 text-[#918d83] dark:text-[#78756d] hover:text-[#1f1e1d] dark:hover:text-[#edece8] transition-colors"
                >
                  {showPassword ? (
                    <EyeOff className="w-4 h-4" />
                  ) : (
                    <Eye className="w-4 h-4" />
                  )}
                </button>
              </div>
              {/* 密码强度指示器 */}
              {formData.password && (
                <div className="mt-2">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="flex-1 h-1.5 bg-[#f3f0e6] dark:bg-[#201f1c] rounded-full overflow-hidden">
                      <div
                        className={`h-full ${passwordStrength.color} transition-all duration-300`}
                        style={{
                          width: `${(passwordStrength.score / 5) * 100}%`,
                        }}
                      />
                    </div>
                    <span className="text-xs text-[#6e6b63] dark:text-[#a19f96]">
                      {passwordStrength.label}
                    </span>
                  </div>
                </div>
              )}
            </div>

            {/* 邀请码(可选):填了加入团队工作区共享知识库 */}
            <div>
              <label className="block text-xs font-semibold text-[#6e6b63] dark:text-[#a19f96] uppercase tracking-wider mb-2">
                邀请码 <span className="normal-case tracking-normal font-normal">(可选)</span>
              </label>
              <div className="relative">
                <KeyRound className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#918d83] dark:text-[#78756d]" />
                <input
                  type="text"
                  value={formData.inviteCode}
                  onChange={(e) =>
                    setFormData({ ...formData, inviteCode: e.target.value })
                  }
                  placeholder="有团队的邀请码?填上即可加入共享知识库"
                  maxLength={16}
                  aria-label="工作区邀请码"
                  className="w-full pl-10 pr-4 py-2.5 bg-[#fbf9f5] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] dark:placeholder-[#78756d] focus:outline-none focus:ring-2 focus:ring-[#da7756] focus:border-transparent transition-all text-sm uppercase"
                />
              </div>
              <p className="mt-1 text-[11px] text-[#918d83]">
                不填则创建你的个人空间;填了则以成员身份加入对应团队,共享其知识库
              </p>
            </div>

            {/* 服务条款 */}
            <div className="flex items-start gap-2 text-xs pt-1">
              <input
                type="checkbox"
                required
                className="mt-0.5 w-4 h-4 rounded border-[#e3dfd5] text-[#da7756] focus:ring-[#da7756]"
              />
              <label className="text-[#6e6b63] dark:text-[#a19f96]">
                我已阅读并同意{" "}
                <a href="#" className="text-[#da7756] hover:underline">
                  服务条款
                </a>{" "}
                和{" "}
                <a href="#" className="text-[#da7756] hover:underline">
                  隐私政策
                </a>
              </label>
            </div>

            {/* 注册按钮 */}
            <button
              type="submit"
              disabled={loading}
              className="btn-accent w-full py-3 px-4 disabled:bg-none disabled:bg-[#918d83] disabled:shadow-none text-white font-medium rounded-xl disabled:cursor-not-allowed text-sm"
            >
              {loading ? "注册中..." : "创建账号"}
            </button>
          </form>

          {/* 分割线 */}
          <div className="relative my-6">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-[#e6e2d8] dark:border-[#282724]"></div>
            </div>
            <div className="relative flex justify-center text-xs">
              <span className="px-3 bg-white dark:bg-[#1a1917] text-[#918d83]">
                或使用第三方账号注册
              </span>
            </div>
          </div>

          {/* 第三方注册按钮 */}
          <div className="grid grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => handleOAuthRegister("github")}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] text-xs font-medium transition-colors"
            >
              <Github className="w-4 h-4" />
              <span>GitHub</span>
            </button>
            <button
              type="button"
              onClick={() => handleOAuthRegister("google")}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] text-xs font-medium transition-colors"
            >
              <Chrome className="w-4 h-4" />
              <span>Google</span>
            </button>
          </div>

          {/* 登录链接 */}
          <div className="mt-6 text-center text-xs text-[#6e6b63] dark:text-[#a19f96]">
            已经有账号了?{" "}
            <button
              type="button"
              onClick={() => navigate("/login")}
              className="text-[#da7756] hover:underline font-semibold"
            >
              立即登录
            </button>
          </div>
          </div>
        </div>
      </div>
    </div>
  );
};
