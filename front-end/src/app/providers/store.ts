import { configureStore } from "@reduxjs/toolkit";
import authReducer from "@/entities/auth/model/authSlice";

/**
 * 全局状态只有认证。
 *
 * 这里曾经还有一个 `chat` slice（会话列表、消息流、流式增量、审批卡片状态）。
 * 那些状态的生命周期都属于"一次对话"，而工单台没有这个概念：工单在数据库里，
 * 界面上每一次读取都是 `/tickets` 的一次请求，刷新之后该看到的还在。
 *
 * 留下的判据因此只有一条：**跨路由存活、且后端不替我们记的状态**才进 store。
 * 认证符合（token 与当前用户），工单队列的筛选条件不符合（跳走再回来重置反而
 * 更符合直觉，而且它该能出现在 URL 里）。
 */
export const store = configureStore({
  reducer: {
    auth: authReducer,
  },
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
