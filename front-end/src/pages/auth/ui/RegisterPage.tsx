import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDispatch } from "react-redux";
import { apiClient } from "@/shared/api/client";
import { setUser, setToken } from "@/entities/auth/model/authSlice";
import { useTheme } from "@/shared/lib/ThemeContext";
import {
  Bot,
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
      });

      dispatch(setToken(response.access_token));
      dispatch(setUser(response.user));
      localStorage.setItem("user", JSON.stringify(response.user));
      navigate("/chat");
    } catch (err: any) {
      const detail = err.response?.data?.detail;
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
    <div className="min-h-screen bg-[#fbf9f5] dark:bg-[#141413] flex items-center justify-center p-4 relative transition-colors duration-200">
      {/* Top right theme toggle */}
      <div className="absolute top-6 right-6">
        <button
          onClick={toggleTheme}
          className="p-2.5 rounded-full bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#1e1d1b] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#1f1e1d] dark:text-[#edece8] transition-all shadow-md"
        >
          {theme === "dark" ? <Sun className="w-4 h-4 text-amber-400" /> : <Moon className="w-4 h-4 text-slate-700" />}
        </button>
      </div>

      <div className="relative w-full max-w-md my-8">
        {/* Logo 和标题 */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-[#da7756] shadow-lg shadow-[#da7756]/25 mb-4">
            <Bot className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-[#1f1e1d] dark:text-[#edece8] mb-2">创建账号</h1>
          <p className="text-[#6e6b63] dark:text-[#a19f96] text-sm">
            开始使用 AI Workspace 体验智能对话
          </p>
        </div>

        {/* 注册表单卡片 */}
        <div className="bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] rounded-2xl shadow-xl p-8 transition-colors duration-200">
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
              className="w-full py-3 px-4 bg-[#da7756] hover:bg-[#c86544] disabled:bg-[#918d83] text-white font-medium rounded-xl shadow-lg shadow-[#da7756]/25 transition-all disabled:cursor-not-allowed text-sm"
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
  );
};
