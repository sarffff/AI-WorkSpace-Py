import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDispatch } from 'react-redux'
import { apiClient } from '@/shared/api/client'
import { setUser, setToken } from '@/entities/auth/model/authSlice'
import { useTheme } from '@/shared/lib/ThemeContext'
import { Bot, Mail, Lock, Github, Chrome, AlertCircle, Eye, EyeOff, Sun, Moon } from 'lucide-react'

export const LoginPage: React.FC = () => {
  const navigate = useNavigate()
  const dispatch = useDispatch()
  const { theme, toggleTheme } = useTheme()
  
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const response = await apiClient.login({ email, password })
      dispatch(setToken(response.access_token))
      dispatch(setUser(response.user))
      localStorage.setItem('user', JSON.stringify(response.user))
      navigate('/chat')
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      setError(detail || '登录失败,请检查邮箱和密码')
    } finally {
      setLoading(false)
    }
  }

  const handleOAuthLogin = (provider: 'github' | 'google') => {
    alert(`${provider === 'github' ? 'GitHub' : 'Google'} 登录功能展示\n\n实际项目中需要配置 OAuth 应用`)
  }

  return (
    <div className="min-h-screen app-atmosphere flex items-center justify-center p-4 relative transition-colors duration-200">
      {/* Top right theme toggle */}
      <div className="absolute top-6 right-6 z-20">
        <button
          onClick={toggleTheme}
          className="p-2.5 rounded-full bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#1e1d1b] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#1f1e1d] dark:text-[#edece8] transition-all shadow-md"
        >
          {theme === "dark" ? <Sun className="w-4 h-4 text-amber-400" /> : <Moon className="w-4 h-4 text-slate-700" />}
        </button>
      </div>

      <div className="relative w-full max-w-md">
        {/* Logo 和标题 */}
        <div className="text-center mb-8">
          <div className="relative inline-flex anim-fade-up">
            <div className="absolute inset-0 rounded-[22px] bg-[#da7756] blur-2xl opacity-30" />
            <div className="relative inline-flex items-center justify-center w-16 h-16 rounded-[22px] btn-accent mb-4">
              <Bot className="w-8 h-8 text-white" />
            </div>
          </div>
          <h1 className="font-display text-[28px] font-semibold text-[#1f1e1d] dark:text-[#edece8] mb-2 anim-fade-up stagger-1">欢迎回来</h1>
          <p className="text-[#6e6b63] dark:text-[#a19f96] text-sm anim-fade-up stagger-2">登录到 AI Workspace 继续对话</p>
        </div>

        {/* 登录表单卡片 */}
        <div className="card-surface rounded-2xl p-8 transition-colors duration-200 anim-fade-up stagger-3 relative z-10">
          <form onSubmit={handleLogin} className="space-y-5">
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
                邮箱地址
              </label>
              <div className="relative">
                <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#918d83] dark:text-[#78756d]" />
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="your@email.com"
                  required
                  className="w-full pl-10 pr-4 py-2.5 bg-[#fbf9f5] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] dark:placeholder-[#78756d] focus:outline-none focus:ring-2 focus:ring-[#da7756] focus:border-transparent transition-all text-sm"
                />
              </div>
            </div>

            {/* 密码输入 */}
            <div>
              <label className="block text-xs font-semibold text-[#6e6b63] dark:text-[#a19f96] uppercase tracking-wider mb-2">
                密码
              </label>
              <div className="relative">
                <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#918d83] dark:text-[#78756d]" />
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                  minLength={6}
                  className="w-full pl-10 pr-12 py-2.5 bg-[#fbf9f5] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] dark:placeholder-[#78756d] focus:outline-none focus:ring-2 focus:ring-[#da7756] focus:border-transparent transition-all text-sm"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3.5 top-1/2 -translate-y-1/2 text-[#918d83] dark:text-[#78756d] hover:text-[#1f1e1d] dark:hover:text-[#edece8] transition-colors"
                >
                  {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            {/* 忘记密码 */}
            <div className="flex items-center justify-between text-xs">
              <label className="flex items-center gap-2 text-[#6e6b63] dark:text-[#a19f96] cursor-pointer">
                <input type="checkbox" className="w-4 h-4 rounded border-[#e3dfd5] text-[#da7756] focus:ring-[#da7756]" />
                <span>记住我</span>
              </label>
              <button type="button" className="text-[#da7756] hover:underline font-medium">
                忘记密码?
              </button>
            </div>

            {/* 登录按钮 */}
            <button
              type="submit"
              disabled={loading}
              className="btn-accent w-full py-3 px-4 disabled:bg-none disabled:bg-[#918d83] disabled:shadow-none text-white font-medium rounded-xl disabled:cursor-not-allowed text-sm"
            >
              {loading ? '登录中...' : '登录'}
            </button>
          </form>

          {/* 分割线 */}
          <div className="relative my-6">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-[#e6e2d8] dark:border-[#282724]"></div>
            </div>
            <div className="relative flex justify-center text-xs">
              <span className="px-3 bg-white dark:bg-[#1a1917] text-[#918d83]">或使用第三方账号</span>
            </div>
          </div>

          {/* 第三方登录按钮 */}
          <div className="grid grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => handleOAuthLogin('github')}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] text-xs font-medium transition-colors"
            >
              <Github className="w-4 h-4" />
              <span>GitHub</span>
            </button>
            <button
              type="button"
              onClick={() => handleOAuthLogin('google')}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl text-[#1f1e1d] dark:text-[#edece8] text-xs font-medium transition-colors"
            >
              <Chrome className="w-4 h-4" />
              <span>Google</span>
            </button>
          </div>

          {/* 注册链接 */}
          <div className="mt-6 text-center text-xs text-[#6e6b63] dark:text-[#a19f96]">
            还没有账号?{' '}
            <button
              type="button"
              onClick={() => navigate('/register')}
              className="text-[#da7756] hover:underline font-semibold"
            >
              立即注册
            </button>
          </div>
        </div>

        {/* 底部提示 */}
        <div className="mt-6 text-center text-xs text-[#918d83]">
          登录即表示你同意我们的{' '}
          <a href="#" className="text-[#da7756] hover:underline">服务条款</a>
          {' '}和{' '}
          <a href="#" className="text-[#da7756] hover:underline">隐私政策</a>
        </div>
      </div>
    </div>
  )
}
