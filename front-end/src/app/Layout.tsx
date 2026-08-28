import React from 'react'
import { Outlet } from 'react-router-dom'
import { Sidebar } from '@/widgets/sidebar/ui/Sidebar'
import { Header } from '@/widgets/header/ui/Header'

export const Layout: React.FC = () => {
  return (
    <div className="flex h-screen w-screen overflow-hidden app-atmosphere text-[#1f1e1d] dark:text-[#edece8] font-sans transition-colors duration-200">
      <Sidebar />

      <div className="flex-1 flex flex-col h-full min-w-0 relative z-10">
        <Header />
        <main className="flex-1 overflow-hidden relative">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
