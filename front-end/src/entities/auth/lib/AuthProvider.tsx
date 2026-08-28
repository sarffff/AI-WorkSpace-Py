import React, { useEffect } from 'react'
import { useDispatch } from 'react-redux'
import { setAuth, setLoading, clearAuth } from '../model/authSlice'
import { apiClient } from '@/shared/api/client'
import type { User } from '@/shared/types/api.types'

interface AuthProviderProps {
  children: React.ReactNode
}

/**
 * 认证提供者组件
 * 
 * 负责:
 * 1. 应用启动时从localStorage恢复认证状态
 * 2. 验证token有效性
 * 3. 初始化apiClient的token
 */
export const AuthProvider: React.FC<AuthProviderProps> = ({ children }) => {
  const dispatch = useDispatch()

  useEffect(() => {
    const initAuth = async () => {
      try {
        // 从localStorage读取token和用户信息
        const token = localStorage.getItem('access_token')
        const userStr = localStorage.getItem('user')

        if (token && userStr) {
          const user: User = JSON.parse(userStr)
          
          // 设置apiClient的token
          apiClient.setToken(token)
          
          // 恢复认证状态
          dispatch(setAuth({ user, token }))
          
          // 验证token有效性 (可选)
          try {
            const currentUser = await apiClient.getCurrentUser()
            // 更新用户信息
            dispatch(setAuth({ user: currentUser, token }))
            localStorage.setItem('user', JSON.stringify(currentUser))
          } catch (error) {
            // Token无效,清除认证状态
            console.error('Token验证失败:', error)
            dispatch(clearAuth())
            apiClient.setToken(null)
            localStorage.removeItem('access_token')
            localStorage.removeItem('user')
          }
        } else {
          // 没有保存的认证信息
          dispatch(clearAuth())
        }
      } catch (error) {
        console.error('认证初始化失败:', error)
        dispatch(clearAuth())
      } finally {
        // 完成加载
        dispatch(setLoading(false))
      }
    }

    initAuth()
  }, [dispatch])

  return <>{children}</>
}
