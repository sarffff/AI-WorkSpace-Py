import React from "react";
import { HashRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "@/entities/auth/lib/AuthProvider";
import { ThemeProvider } from "@/shared/lib/ThemeContext";
import { ProtectedRoute } from "@/shared/lib/ProtectedRoute";
import { Layout } from "./Layout";
import { LoginPage } from "@/pages/auth/ui/LoginPage";
import { RegisterPage } from "@/pages/auth/ui/RegisterPage";
import { ChatPage } from "@/pages/chat/ui/ChatPage";
import { KnowledgePage } from "@/pages/knowledge/ui/KnowledgePage";
import { PromptsPage } from "@/pages/prompts/ui/PromptsPage";
import { DashboardPage } from "@/pages/dashboard/ui/DashboardPage";
import { TracesPage } from "@/pages/traces/ui/TracesPage";
import { SettingsPage } from "@/pages/settings/ui/SettingsPage";

export function App() {
  return (
    <ThemeProvider>
      <HashRouter>
        <AuthProvider>
          <Routes>
            {/* 公开路由 */}
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />

            {/* 受保护的路由 */}
            <Route
              path="/"
              element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }
            >
              <Route index element={<Navigate to="/dashboard" replace />} />
              <Route path="dashboard" element={<DashboardPage />} />
              <Route path="chat" element={<ChatPage />} />
              <Route path="traces" element={<TracesPage />} />
              <Route path="knowledge" element={<KnowledgePage />} />
              <Route path="prompts" element={<PromptsPage />} />
              <Route path="settings" element={<SettingsPage />} />
            </Route>

            {/* 404 - 重定向到首页 */}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </AuthProvider>
      </HashRouter>
    </ThemeProvider>
  );
}

export default App;
