import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDispatch } from "react-redux";
import { apiClient } from "@/shared/api/client";
import { setUser, setToken } from "@/entities/auth/model/authSlice";
import {
  Bot,
  Mail,
  Lock,
  User as UserIcon,
  AlertCircle,
  Eye,
  EyeOff,
  CheckCircle,
  Github,
  Chrome,
} from "lucide-react";

export const RegisterPage: React.FC = () => {
  const navigate = useNavigate();
  const dispatch = useDispatch();

  const [formData, setFormData] = useState({
    email: "",
    username: "",
    password: "",
    confirmPassword: "",
    name: "",
  });
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [passwordStrength, setPasswordStrength] = useState({
    score: 0,
    label: "",
    color: "",
  });

  // 密码强度检测
  const checkPasswordStrength = (password: string) => {
    let score = 0;
    if (password.length >= 6) score++;
    if (password.length >= 8) score++;
    if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++;
    if (/\d/.test(password)) score++;
    if (/[^a-zA-Z\d]/.test(password)) score++;

    if (score <= 1) return { score, label: "弱", color: "bg-red-500" };
    if (score <= 3) return { score, label: "中等", color: "bg-yellow-500" };
    return { score, label: "强", color: "bg-green-500" };
  };

  const handlePasswordChange = (password: string) => {
    setFormData({ ...formData, password });
    setPasswordStrength(checkPasswordStrength(password));
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    // 表单验证
    // if (formData.password !== formData.confirmPassword) {
    //   setError("两次输入的密码不一致");
    //   return;
    // }

    if (formData.password.length < 6) {
      setError("密码至少需要6个字符");
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

      // 保存 token 和用户信息到 Redux
      dispatch(setToken(response.access_token));
      dispatch(setUser(response.user));

      // 保存 token 到 localStorage
      localStorage.setItem("access_token", response.access_token);
      localStorage.setItem("user", JSON.stringify(response.user));

      // 跳转到主页
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
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-blue-950 to-slate-950 flex items-center justify-center p-4">
      {/* 背景装饰 */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div className="absolute top-1/4 left-1/4 w-96 h-96 bg-blue-500/10 rounded-full blur-3xl" />
        <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-indigo-500/10 rounded-full blur-3xl" />
      </div>

      <div className="relative w-full max-w-md">
        {/* Logo 和标题 */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-tr from-indigo-600 via-blue-500 to-cyan-400 shadow-lg shadow-indigo-500/30 mb-4">
            <Bot className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-3xl font-bold text-white mb-2">创建账号</h1>
          <p className="text-slate-400 text-sm">
            开始使用 AI Workspace 体验智能对话
          </p>
        </div>

        {/* 注册表单卡片 */}
        <div className="bg-slate-900/80 backdrop-blur-xl border border-slate-800 rounded-2xl shadow-2xl p-8">
          <form onSubmit={handleRegister} className="space-y-4">
            {/* 错误提示 */}
            {error && (
              <div className="flex items-center gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
                <AlertCircle className="w-4 h-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {/* 邮箱输入 */}
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                邮箱地址 <span className="text-red-400">*</span>
              </label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500" />
                <input
                  type="email"
                  value={formData.email}
                  onChange={(e) =>
                    setFormData({ ...formData, email: e.target.value })
                  }
                  placeholder="your@email.com"
                  required
                  className="w-full pl-10 pr-4 py-2.5 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                />
              </div>
            </div>

            {/* 用户名输入 */}
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                用户名 <span className="text-red-400">*</span>
              </label>
              <div className="relative">
                <UserIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500" />
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
                  className="w-full pl-10 pr-4 py-2.5 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                />
              </div>
              <p className="mt-1 text-xs text-slate-500">
                3-50个字符,只能包含字母、数字、下划线和横线
              </p>
            </div>

            {/* 密码输入 */}
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                密码 <span className="text-red-400">*</span>
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500" />
                <input
                  type={showPassword ? "text" : "password"}
                  value={formData.password}
                  onChange={(e) => handlePasswordChange(e.target.value)}
                  placeholder="至少6个字符"
                  required
                  minLength={6}
                  className="w-full pl-10 pr-12 py-2.5 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300 transition-colors"
                >
                  {showPassword ? (
                    <EyeOff className="w-5 h-5" />
                  ) : (
                    <Eye className="w-5 h-5" />
                  )}
                </button>
              </div>
              {/* 密码强度指示器 */}
              {formData.password && (
                <div className="mt-2">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
                      <div
                        className={`h-full ${passwordStrength.color} transition-all duration-300`}
                        style={{
                          width: `${(passwordStrength.score / 5) * 100}%`,
                        }}
                      />
                    </div>
                    <span className="text-xs text-slate-400">
                      {passwordStrength.label}
                    </span>
                  </div>
                </div>
              )}
            </div>

            {/* 确认密码输入 */}
            {/* <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                确认密码 <span className="text-red-400">*</span>
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500" />
                <input
                  type={showConfirmPassword ? "text" : "password"}
                  value={formData.confirmPassword}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      confirmPassword: e.target.value,
                    })
                  }
                  placeholder="再次输入密码"
                  required
                  className="w-full pl-10 pr-12 py-2.5 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                />
                <button
                  type="button"
                  onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300 transition-colors"
                >
                  {showConfirmPassword ? (
                    <EyeOff className="w-5 h-5" />
                  ) : (
                    <Eye className="w-5 h-5" />
                  )}
                </button>
              </div>
              {formData.confirmPassword &&
                formData.password === formData.confirmPassword && (
                  <div className="flex items-center gap-1 mt-1 text-xs text-green-400">
                    <CheckCircle className="w-3 h-3" />
                    <span>密码匹配</span>
                  </div>
                )}
            </div> */}

            {/* 服务条款 */}
            <div className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                required
                className="mt-0.5 w-4 h-4 rounded border-slate-600 text-indigo-600 focus:ring-indigo-500"
              />
              <label className="text-slate-400">
                我已阅读并同意{" "}
                <a href="#" className="text-indigo-400 hover:text-indigo-300">
                  服务条款
                </a>{" "}
                和{" "}
                <a href="#" className="text-indigo-400 hover:text-indigo-300">
                  隐私政策
                </a>
              </label>
            </div>

            {/* 注册按钮 */}
            <button
              type="submit"
              disabled={loading}
              className="w-full py-3 px-4 bg-gradient-to-r from-indigo-600 to-blue-600 hover:from-indigo-500 hover:to-blue-500 disabled:from-slate-700 disabled:to-slate-700 text-white font-medium rounded-lg shadow-lg shadow-indigo-600/20 transition-all disabled:cursor-not-allowed"
            >
              {loading ? "注册中..." : "创建账号"}
            </button>
          </form>

          {/* 分割线 */}
          <div className="relative my-6">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-slate-800"></div>
            </div>
            <div className="relative flex justify-center text-sm">
              <span className="px-4 bg-slate-900/80 text-slate-500">
                或使用第三方注册
              </span>
            </div>
          </div>

          {/* 第三方注册按钮 */}
          <div className="grid grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => handleOAuthRegister("github")}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-slate-800/50 hover:bg-slate-800 border border-slate-700 rounded-lg text-slate-300 text-sm font-medium transition-colors"
            >
              <Github className="w-5 h-5" />
              <span>GitHub</span>
            </button>
            <button
              type="button"
              onClick={() => handleOAuthRegister("google")}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-slate-800/50 hover:bg-slate-800 border border-slate-700 rounded-lg text-slate-300 text-sm font-medium transition-colors"
            >
              <Chrome className="w-5 h-5" />
              <span>Google</span>
            </button>
          </div>

          {/* 登录链接 */}
          <div className="mt-6 text-center text-sm text-slate-400">
            已经有账号了?{" "}
            <button
              type="button"
              onClick={() => navigate("/login")}
              className="text-indigo-400 hover:text-indigo-300 font-medium transition-colors"
            >
              立即登录
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
