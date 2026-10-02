import React from 'react'
import { Navigate } from 'react-router-dom'
import { useSelector } from 'react-redux'
import { RootState } from '@/app/providers/store'

interface ProtectedRouteProps {
  children: React.ReactNode
}

/**
 * 路由守卫组件
 * 
 * 保护需要认证的路由,未登录用户会被重定向到登录页
 */
export const ProtectedRoute: React.FC<ProtectedRouteProps> = ({ children }) => {
  const { isAuthenticated, isLoading } = useSelector((state: RootState) => state.auth)

  // 加载中显示 loading（跟随暖纸主题，避免启动瞬间闪一屏旧的靛蓝渐变）
  if (isLoading) {
    return (
      <div className="min-h-screen app-atmosphere flex items-center justify-center">
        <div className="relative z-10 text-center">
          <div className="inline-block w-10 h-10 border-[3px] border-[#da7756] border-t-transparent rounded-full animate-spin mb-4"></div>
          <p className="text-sm text-[#6e6b63] dark:text-[#a19f96]">加载中...</p>
        </div>
      </div>
    )
  }

  // 未认证跳转到登录页
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }

  return <>{children}</>
}
