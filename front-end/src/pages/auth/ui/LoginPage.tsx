import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDispatch } from 'react-redux'
import { apiClient } from '@/shared/api/client'
import { setUser, setToken } from '@/entities/auth/model/authSlice'
import { NEXT_MODE, THEME_LABELS, useTheme } from '@/shared/lib/ThemeContext'
import { BrandMark } from '@/shared/ui/BrandMark'
import { Mail, Lock, Github, Chrome, AlertCircle, Eye, EyeOff, Sun, Moon, Monitor } from 'lucide-react'

const STORIES = [
  { label: '人在回路', body: '资金类操作停下来等人批；批准后执行的就是当时看到的那份参数。' },
  { label: '完整轨迹', body: '每一步判断、调用与结果都落库，事后能回答当时查到哪儿。' },
  { label: '限额与熔断', body: '当日退款额度、单工单成本与工具次数；到线就交人，不是退少一点。' },
  { label: '注入中和', body: '客户原文与政策片段进上下文前先中和协议标记，命中会留痕。' },
]

export const LoginPage: React.FC = () => {
  const navigate = useNavigate()
  const dispatch = useDispatch()
  const { mode, theme, cycleMode } = useTheme()

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
      navigate('/queue')
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
          onClick={cycleMode}
          className="group relative p-2.5 rounded-full bg-mantle hover:bg-overlay border border-line text-ink transition-all shadow-card"
          aria-label={`主题：${THEME_LABELS[mode]}（点击切换到${THEME_LABELS[NEXT_MODE[mode]]}）`}
          title="登录前也可以先把明暗定下来"
        >
          {mode === "system" ? (
            <Monitor className="w-4 h-4" />
          ) : theme === "dark" ? (
            <Sun className="w-4 h-4 text-state-hold" />
          ) : (
            <Moon className="w-4 h-4" />
          )}
        </button>
      </div>

      <aside className="hidden lg:flex w-[44%] relative flex-col justify-between p-12 border-r border-line">
        <div className="absolute inset-0 pointer-events-none overflow-hidden">
          <div className="absolute inset-0 lab-grid" />
        </div>
        <div className="relative z-10">
          <div className="flex items-center gap-3 mb-10">
            <BrandMark size={40} />
            <span className="font-display text-lg font-semibold">客服工单台</span>
          </div>
          <p className="label-eyebrow mb-3">TICKET DESK</p>
          <h1 className="font-display text-[40px] leading-[1.15] font-semibold text-ink text-balance">
            把工单办完<br />而不是答完
          </h1>
          <p className="mt-4 text-sm text-ink-soft max-w-sm leading-relaxed">
            查订单、改地址、发起退款、关单——风险高的那几步必须有人点头，而每一步做过什么都留得下来。
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
