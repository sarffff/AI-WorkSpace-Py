import React from "react";
import { HashRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "@/entities/auth/lib/AuthProvider";
import { ThemeProvider } from "@/shared/lib/ThemeContext";
import { ProtectedRoute } from "@/shared/lib/ProtectedRoute";
import { Layout } from "./Layout";
import { LoginPage } from "@/pages/auth/ui/LoginPage";
import { RegisterPage } from "@/pages/auth/ui/RegisterPage";
import { TicketQueuePage } from "@/pages/queue/ui/TicketQueuePage";
import { TicketDetailPage } from "@/pages/queue/ui/TicketDetailPage";
import { ApprovalsPage } from "@/pages/approvals/ui/ApprovalsPage";
import { GovernancePage } from "@/pages/governance/ui/GovernancePage";
import { MetricsPage } from "@/pages/metrics/ui/MetricsPage";
import { KnowledgePage } from "@/pages/knowledge/ui/KnowledgePage";
import { SkillsPage } from "@/pages/skills/ui/SkillsPage";
import { NotificationsPage } from "@/pages/notifications/ui/NotificationsPage";
import { AuditPage } from "@/pages/audit/ui/AuditPage";
import { WorkspacePage } from "@/pages/workspace/ui/WorkspacePage";

/**
 * 路由表就是工单台的模块清单。
 *
 * 首页指向队列而不是"总览"：一个处置系统的第一屏应该是"现在有什么事没办完"，
 * 而不是几张统计卡——统计在指标看板里，看它的时候事情已经办完了。
 */
export function App() {
  return (
    <ThemeProvider>
      <HashRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />

            <Route
              path="/"
              element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }
            >
              <Route index element={<Navigate to="/queue" replace />} />
              <Route path="queue" element={<TicketQueuePage />} />
              <Route path="tickets/:ticketId" element={<TicketDetailPage />} />
              <Route path="approvals" element={<ApprovalsPage />} />
              <Route path="governance" element={<GovernancePage />} />
              <Route path="metrics" element={<MetricsPage />} />
              <Route path="knowledge" element={<KnowledgePage />} />
              <Route path="skills" element={<SkillsPage />} />
              <Route path="notifications" element={<NotificationsPage />} />
              <Route path="audit" element={<AuditPage />} />
              <Route path="workspace" element={<WorkspacePage />} />
            </Route>

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </AuthProvider>
      </HashRouter>
    </ThemeProvider>
  );
}

export default App;
