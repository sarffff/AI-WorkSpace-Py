import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDispatch } from 'react-redux'
import { apiClient } from '@/shared/api/client'
import { setUser, setToken } from '@/entities/auth/model/authSlice'
import { Bot, Mail, Lock, Github, Chrome, AlertCircle, Eye, EyeOff } from 'lucide-react'

export const LoginPage: React.FC = () => {
  const navigate = useNavigate()
  const dispatch = useDispatch()
  
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
      
      // 保存 token 和用户信息到 Redux
      dispatch(setToken(response.access_token))
      dispatch(setUser(response.user))
      
      // 保存 token 到 localStorage
      localStorage.setItem('access_token', response.access_token)
      localStorage.setItem('user', JSON.stringify(response.user))
      
      // 跳转到主页
      navigate('/chat')
    } catch (err: any) {
      setError(err.response?.data?.detail || '登录失败,请检查邮箱和密码')
    } finally {
      setLoading(false)
    }
  }

  const handleOAuthLogin = (provider: 'github' | 'google') => {
    // 展示功能 - 实际项目中会跳转到 OAuth 授权页面
    alert(`${provider === 'github' ? 'GitHub' : 'Google'} 登录功能展示\n\n实际项目中需要配置 OAuth 应用`)
  }

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
          <h1 className="text-3xl font-bold text-white mb-2">欢迎回来</h1>
          <p className="text-slate-400 text-sm">登录到 AI Workspace 继续对话</p>
        </div>

        {/* 登录表单卡片 */}
        <div className="bg-slate-900/80 backdrop-blur-xl border border-slate-800 rounded-2xl shadow-2xl p-8">
          <form onSubmit={handleLogin} className="space-y-5">
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
                邮箱地址
              </label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500" />
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="your@email.com"
                  required
                  className="w-full pl-10 pr-4 py-2.5 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                />
              </div>
            </div>

            {/* 密码输入 */}
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                密码
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500" />
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                  minLength={6}
                  className="w-full pl-10 pr-12 py-2.5 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300 transition-colors"
                >
                  {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                </button>
              </div>
            </div>

            {/* 忘记密码 */}
            <div className="flex items-center justify-between text-sm">
              <label className="flex items-center gap-2 text-slate-400 cursor-pointer">
                <input type="checkbox" className="w-4 h-4 rounded border-slate-600 text-indigo-600 focus:ring-indigo-500" />
                <span>记住我</span>
              </label>
              <button type="button" className="text-indigo-400 hover:text-indigo-300 transition-colors">
                忘记密码?
              </button>
            </div>

            {/* 登录按钮 */}
            <button
              type="submit"
              disabled={loading}
              className="w-full py-3 px-4 bg-gradient-to-r from-indigo-600 to-blue-600 hover:from-indigo-500 hover:to-blue-500 disabled:from-slate-700 disabled:to-slate-700 text-white font-medium rounded-lg shadow-lg shadow-indigo-600/20 transition-all disabled:cursor-not-allowed"
            >
              {loading ? '登录中...' : '登录'}
            </button>
          </form>

          {/* 分割线 */}
          <div className="relative my-6">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-slate-800"></div>
            </div>
            <div className="relative flex justify-center text-sm">
              <span className="px-4 bg-slate-900/80 text-slate-500">或使用第三方登录</span>
            </div>
          </div>

          {/* 第三方登录按钮 */}
          <div className="grid grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => handleOAuthLogin('github')}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-slate-800/50 hover:bg-slate-800 border border-slate-700 rounded-lg text-slate-300 text-sm font-medium transition-colors"
            >
              <Github className="w-5 h-5" />
              <span>GitHub</span>
            </button>
            <button
              type="button"
              onClick={() => handleOAuthLogin('google')}
              className="flex items-center justify-center gap-2 py-2.5 px-4 bg-slate-800/50 hover:bg-slate-800 border border-slate-700 rounded-lg text-slate-300 text-sm font-medium transition-colors"
            >
              <Chrome className="w-5 h-5" />
              <span>Google</span>
            </button>
          </div>

          {/* 注册链接 */}
          <div className="mt-6 text-center text-sm text-slate-400">
            还没有账号?{' '}
            <button
              type="button"
              onClick={() => navigate('/register')}
              className="text-indigo-400 hover:text-indigo-300 font-medium transition-colors"
            >
              立即注册
            </button>
          </div>
        </div>

        {/* 底部提示 */}
        <div className="mt-6 text-center text-xs text-slate-500">
          登录即表示你同意我们的{' '}
          <a href="#" className="text-indigo-400 hover:text-indigo-300">服务条款</a>
          {' '}和{' '}
          <a href="#" className="text-indigo-400 hover:text-indigo-300">隐私政策</a>
        </div>
      </div>
    </div>
  )
}
