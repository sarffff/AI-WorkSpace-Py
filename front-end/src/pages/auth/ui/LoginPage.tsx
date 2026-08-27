import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDispatch } from 'react-redux'
import { apiClient } from '@/shared/api/client'
import { setUser, setToken } from '@/entities/auth/model/authSlice'
import { useTheme } from '@/shared/lib/ThemeContext'
import { BrandMark } from '@/shared/ui/BrandMark'
import { Mail, Lock, Github, Chrome, AlertCircle, Eye, EyeOff, Sun, Moon } from 'lucide-react'

const STORIES = [
  { label: '混合检索', body: 'dense + sparse，RRF 融合后再引用。' },
  { label: '工具轨迹', body: '每一步落库，刷新之后还能核对。' },
  { label: '语义缓存', body: '按提示词版本分桶，试一版不会脏另一版。' },
  { label: '安全护栏', body: '检索资料里的注入指令会被中和，不是静默吞掉。' },
]

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
            <span className="font-display text-lg font-semibold">有据工作台</span>
          </div>
          <p className="label-eyebrow mb-3">工作台</p>
          <h1 className="font-display text-[40px] leading-[1.15] font-semibold text-[#1f1e1d] dark:text-[#edece8] text-balance">
            看见模型<br />怎么想
          </h1>
          <p className="mt-4 text-sm text-[#6e6b63] dark:text-[#a19f96] max-w-sm leading-relaxed">
            不是又一个聊天框。检索、工具、缓存、护栏和轨迹摊在台面上，方便核对每一次回答是怎么来的。
          </p>
        </div>
        <ul className="relative z-10 space-y-4 mt-12">
          {STORIES.map((item, i) => (
            <li
              key={item.label}
              className="flex items-start gap-3 anim-fade-up"
              style={{ animationDelay: `${0.12 + i * 0.07}s` }}
            >
              <span className="mt-1.5 capability-dot shrink-0" />
              <div>
                <div className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                  {item.label}
                </div>
                <div className="text-xs text-[#6e6b63] dark:text-[#a19f96] mt-0.5">
                  {item.body}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </aside>

      <div className="flex-1 flex items-center justify-center p-6 relative">
        <div className="relative w-full max-w-md">
          <div className="text-center mb-8 lg:hidden">
            <BrandMark size={48} className="mx-auto mb-3 !rounded-[16px]" />
            <h1 className="font-display text-[26px] font-semibold">欢迎回来</h1>
          </div>
          <div className="hidden lg:block mb-8 anim-fade-up">
            <h2 className="font-display text-[28px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">
              欢迎回来
            </h2>
            <p className="text-sm text-[#6e6b63] dark:text-[#a19f96] mt-1">
              登录到工作台，继续核对下一次回答。
            </p>
          </div>

          <div className="card-surface rounded-2xl p-8 relative z-10 anim-fade-up stagger-1">
            <form onSubmit={handleLogin} className="space-y-5">
              {error && (
                <div className="flex items-center gap-2 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-sm">
                  <AlertCircle className="w-4 h-4 shrink-0" />
                  <span>{error}</span>
                </div>
              )}

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

              <div className="flex items-center justify-between text-xs">
                <label className="flex items-center gap-2 text-[#6e6b63] dark:text-[#a19f96] cursor-pointer">
                  <input type="checkbox" className="w-4 h-4 rounded border-[#e3dfd5] text-[#da7756] focus:ring-[#da7756]" />
                  <span>记住我</span>
                </label>
                <button type="button" className="text-[#da7756] hover:underline font-medium">
                  忘记密码?
                </button>
              </div>

              <button
                type="submit"
                disabled={loading}
                className="btn-accent w-full py-3 px-4 disabled:bg-none disabled:bg-[#918d83] disabled:shadow-none text-white font-medium rounded-xl disabled:cursor-not-allowed text-sm"
              >
                {loading ? '登录中...' : '进入工作台'}
              </button>
            </form>

            <div className="relative my-6">
              <div className="absolute inset-0 flex items-center">
                <div className="w-full border-t border-[#e6e2d8] dark:border-[#282724]"></div>
              </div>
              <div className="relative flex justify-center text-xs">
                <span className="px-3 bg-white dark:bg-[#1a1917] text-[#918d83]">或使用第三方账号</span>
              </div>
            </div>

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
        </div>
      </div>
    </div>
  )
}
