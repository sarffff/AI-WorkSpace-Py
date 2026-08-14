import type {
  Message,
  Chat,
  ChatRequest,
  CreateChatRequest,
  CompletionResponse,
  StreamChunk,
  KnowledgeDocument,
  LoginRequest,
  RegisterRequest,
  AuthResponse,
  User,
  TokenResponse,
  Prompt,
  PromptCreateRequest,
  PromptLibraryEntry,
  AppSettings,
  UserPreferences,
  UsageSummary,
  TraceSummary,
  TraceDetail,
  KnowledgeQueryResult,
  MessageFeedback,
  FeedbackSummary,
  ToolStep,
} from "../types/api.types";

/**
 * FastAPI后端API客户端
 * 对接Python后端的所有端点
 */
export class ApiClient {
  private baseUrl: string;

  /** 后端基础 URL（用于拼接附件等静态资源地址） */
  getBaseUrl(): string {
    return this.baseUrl;
  }
  private token: string | null = null;
  private refreshToken: string | null = null;
  private refreshing: Promise<string | null> | null = null;

  constructor(baseUrl: string = "http://localhost:3000") {
    this.baseUrl = baseUrl;
    // 从 localStorage 读取 token
    this.token = localStorage.getItem("access_token");
    this.refreshToken = localStorage.getItem("refresh_token");
  }

  /**
   * 设置认证 token
   */
  setToken(token: string | null) {
    this.token = token;
    if (token) {
      localStorage.setItem("access_token", token);
    } else {
      localStorage.removeItem("access_token");
    }
  }

  /**
   * 设置 refresh token
   */
  setRefreshToken(token: string | null) {
    this.refreshToken = token;
    if (token) {
      localStorage.setItem("refresh_token", token);
    } else {
      localStorage.removeItem("refresh_token");
    }
  }

  /**
   * 获取当前 token
   */
  getToken(): string | null {
    return this.token;
  }

  /**
   * 获取请求 headers
   */
  private getHeaders(): HeadersInit {
    const headers: HeadersInit = {
      "Content-Type": "application/json",
    };

    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    return headers;
  }

  /**
   * 尝试用 refresh token 获取新的 access token
   * 使用单例模式避免并发刷新
   */
  private async tryRefreshToken(): Promise<string | null> {
    // 如果已经在刷新中,复用同一个 Promise
    if (this.refreshing) {
      return this.refreshing;
    }

    if (!this.refreshToken) {
      return null;
    }

    this.refreshing = (async () => {
      try {
        const response = await fetch(`${this.baseUrl}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: this.refreshToken }),
        });

        if (!response.ok) {
          // refresh token 也失效了,清除认证
          this.setToken(null);
          this.setRefreshToken(null);
          localStorage.removeItem("user");
          return null;
        }

        const data: TokenResponse = await response.json();
        this.setToken(data.access_token);
        return data.access_token;
      } catch {
        return null;
      } finally {
        this.refreshing = null;
      }
    })();

    return this.refreshing;
  }

  /**
   * 带自动刷新的 fetch 封装
   * 当收到 401 时自动尝试刷新 token 并重试一次
   */
  private async authedFetch(
    url: string,
    options: RequestInit = {},
  ): Promise<Response> {
    const response = await fetch(url, {
      ...options,
      headers: { ...this.getHeaders(), ...options.headers },
    });

    if (response.status === 401 && this.refreshToken) {
      // 尝试刷新 token
      const newToken = await this.tryRefreshToken();
      if (newToken) {
        // 用新 token 重试
        const retryHeaders: Record<string, string> =
          typeof options.headers === "object"
            ? { ...(options.headers as Record<string, string>) }
            : {};
        retryHeaders["Authorization"] = `Bearer ${newToken}`;
        return fetch(url, { ...options, headers: retryHeaders });
      }
    }

    return response;
  }

  // ========== Auth API ==========

  /**
   * 用户注册
   * POST /auth/register
   */
  async register(request: RegisterRequest): Promise<AuthResponse> {
    const response = await fetch(`${this.baseUrl}/auth/register`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const error = await response.json();
      throw { response: { data: error } };
    }

    const data = await response.json();
    this.setToken(data.access_token);
    this.setRefreshToken(data.refresh_token);
    return data;
  }

  /**
   * 用户登录
   * POST /auth/login
   */
  async login(request: LoginRequest): Promise<AuthResponse> {
    const response = await fetch(`${this.baseUrl}/auth/login`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const error = await response.json();
      throw { response: { data: error } };
    }

    const data = await response.json();
    this.setToken(data.access_token);
    this.setRefreshToken(data.refresh_token);
    return data;
  }

  /**
   * 获取当前用户信息
   * GET /auth/me
   */
  async getCurrentUser(): Promise<User> {
    const response = await this.authedFetch(`${this.baseUrl}/auth/me`, {
      method: "GET",
    });

    if (!response.ok) {
      throw new Error(`Failed to fetch current user: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 用户登出
   * POST /auth/logout
   */
  async logout(): Promise<{ success: boolean; message: string }> {
    const response = await this.authedFetch(`${this.baseUrl}/auth/logout`, {
      method: "POST",
    });

    // 即使服务端登出失败也清除本地 token
    this.setToken(null);
    this.setRefreshToken(null);
    localStorage.removeItem("user");

    if (!response.ok) {
      throw new Error(`Failed to logout: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 刷新 token
   * POST /auth/refresh
   */
  async getRefreshToken(): Promise<TokenResponse> {
    if (!this.refreshToken) {
      throw new Error("No refresh token available");
    }

    const response = await fetch(`${this.baseUrl}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: this.refreshToken }),
    });

    if (!response.ok) {
      this.setToken(null);
      this.setRefreshToken(null);
      localStorage.removeItem("user");
      throw new Error(`Failed to refresh token: ${response.statusText}`);
    }

    const data = await response.json();
    this.setToken(data.access_token);
    return data;
  }

  // ========== Chat API ==========

  /**
   * 获取所有对话列表
   * GET /chats
   */
  async getChats(): Promise<Chat[]> {
    const response = await this.authedFetch(`${this.baseUrl}/chats`, {
      method: "GET",
    });

    if (!response.ok) {
      throw new Error(`Failed to fetch chats: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 获取指定对话的消息列表
   * GET /chats/{chat_id}/messages
   */
  async getMessages(chatId: string): Promise<Message[]> {
    const response = await this.authedFetch(
      `${this.baseUrl}/chats/${chatId}/messages`,
      { method: "GET" },
    );

    if (!response.ok) {
      throw new Error(`Failed to fetch messages: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 取回一个对话已落库的工具执行轨迹。
   *
   * SSE 里的 tool_start / tool_result 是瞬时事件，刷新页面就没了；这是把那条
   * 时间线找回来的唯一入口。返回的步骤按时间升序，`messageId` 指向触发那一回合的
   * **用户**消息，不是回答。
   */
  async getToolSteps(chatId: string): Promise<ToolStep[]> {
    const response = await this.authedFetch(
      `${this.baseUrl}/chats/${chatId}/tool-steps`,
      { method: "GET" },
    );
    if (!response.ok) {
      throw new Error(`Failed to fetch tool steps: ${response.statusText}`);
    }
    const payload = await response.json();
    return payload.steps ?? [];
  }

  /**
   * 创建新对话
   * POST /chats
   */
  async createChat(
    request: CreateChatRequest = {},
  ): Promise<{ id: string; title: string }> {
    const response = await this.authedFetch(`${this.baseUrl}/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      throw new Error(`Failed to create chat: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 非流式对话
   * POST /chats/completions
   */
  async sendMessage(request: ChatRequest): Promise<CompletionResponse> {
    const response = await this.authedFetch(
      `${this.baseUrl}/chats/completions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      },
    );

    if (!response.ok) {
      throw new Error(`Failed to send message: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 流式对话 (SSE)
   * POST /chats/completions/stream
   * @returns AsyncGenerator yielding StreamChunk objects
   */
  async *streamMessage(
    request: ChatRequest,
    signal?: AbortSignal,
  ): AsyncGenerator<StreamChunk, void, undefined> {
    // 流式请求需要先确保 token 有效,如果 401 则先刷新
    let headers = this.getHeaders();

    let response = await fetch(`${this.baseUrl}/chats/completions/stream`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal,
    });

    // 如果 401 且有 refresh token,尝试刷新后重试
    if (response.status === 401 && this.refreshToken) {
      const newToken = await this.tryRefreshToken();
      if (newToken) {
        headers = { ...headers, Authorization: `Bearer ${newToken}` };
        response = await fetch(`${this.baseUrl}/chats/completions/stream`, {
          method: "POST",
          headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify(request),
          signal,
        });
      }
    }

    if (!response.ok) {
      throw new Error(`Failed to stream message: ${response.statusText}`);
    }

    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error("Response body is null");
    }

    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.startsWith("data:")) {
            continue;
          }

          const data = trimmed.slice(5).trim();
          if (!data) {
            continue;
          }

          try {
            const chunk: StreamChunk = JSON.parse(data);
            yield chunk;
          } catch {
            // 跳过无法解析的 SSE 行
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  /**
   * 重命名对话
   * PATCH /chats/{chat_id}
   */
  async renameChat(
    chatId: string,
    title: string,
  ): Promise<{ id: string; title: string }> {
    const response = await this.authedFetch(`${this.baseUrl}/chats/${chatId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });

    if (!response.ok) {
      throw new Error(`Failed to rename chat: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 删除对话
   * DELETE /chats/{chat_id}
   */
  async deleteChat(chatId: string): Promise<{ success: boolean }> {
    const response = await this.authedFetch(`${this.baseUrl}/chats/${chatId}`, {
      method: "DELETE",
    });

    if (!response.ok) {
      throw new Error(`Failed to delete chat: ${response.statusText}`);
    }

    return response.json();
  }

  /** 截断用户消息之后的旧分支，并可选地更新该用户消息。 */
  async reviseMessage(
    chatId: string,
    messageId: string,
    content?: string,
  ): Promise<{ success: boolean; message_id: string; content: string }> {
    const response = await this.authedFetch(
      `${this.baseUrl}/chats/${chatId}/messages/${messageId}/revise`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(content === undefined ? {} : { content }),
      },
    );
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "Failed to revise message");
    }
    return response.json();
  }

  // ========== Knowledge API ==========

  /**
   * 获取知识库文档列表
   * GET /knowledge/documents
   */
  async getDocuments(): Promise<KnowledgeDocument[]> {
    const response = await this.authedFetch(
      `${this.baseUrl}/knowledge/documents`,
      { method: "GET" },
    );

    if (!response.ok) {
      throw new Error(`Failed to fetch documents: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 上传文档到知识库
   * POST /knowledge/documents/upload
   */
  async uploadDocument(file: File): Promise<KnowledgeDocument> {
    const formData = new FormData();
    formData.append("file", file);

    const headers: HeadersInit = {};
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    let response = await fetch(`${this.baseUrl}/knowledge/documents/upload`, {
      method: "POST",
      headers,
      body: formData,
    });

    // 401 自动刷新重试
    if (response.status === 401 && this.refreshToken) {
      const newToken = await this.tryRefreshToken();
      if (newToken) {
        headers["Authorization"] = `Bearer ${newToken}`;
        response = await fetch(`${this.baseUrl}/knowledge/documents/upload`, {
          method: "POST",
          headers,
          body: formData,
        });
      }
    }

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(
        error.detail || `Failed to upload document: ${response.statusText}`,
      );
    }

    return response.json();
  }

  /**
   * 删除知识库文档
   * DELETE /knowledge/documents/{id}
   */
  async deleteDocument(docId: string): Promise<{ success: boolean }> {
    const response = await this.authedFetch(
      `${this.baseUrl}/knowledge/documents/${docId}`,
      { method: "DELETE" },
    );

    if (!response.ok) {
      throw new Error(`Failed to delete document: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 知识库检索
   * POST /knowledge/query
   */
  async queryKnowledge(
    query: string,
    topK: number = 5,
  ): Promise<KnowledgeQueryResult> {
    const response = await this.authedFetch(`${this.baseUrl}/knowledge/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, top_k: topK }),
    });

    if (!response.ok) {
      throw new Error(`Failed to query knowledge: ${response.statusText}`);
    }

    return response.json();
  }

  // ========== 健康检查 ==========

  /**
   * 检查服务器状态
   */
  async ping(): Promise<boolean> {
    try {
      const response = await fetch(`${this.baseUrl}/`, {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  // ========== Prompt API ==========

  /** 获取提示词列表 */
  async getPrompts(): Promise<Prompt[]> {
    const response = await this.authedFetch(`${this.baseUrl}/prompts`, {
      method: "GET",
    });
    if (!response.ok) {
      throw new Error(`Failed to fetch prompts: ${response.statusText}`);
    }
    return response.json();
  }

  /**
   * 获取系统提示词注册表（只读）。
   * 与 getPrompts 不是一回事：那个是用户自己攒的提示词片段（数据库里的行），
   * 这个是驱动对话与评估的系统提示词版本（仓库里的文件）。
   */
  async getPromptLibrary(): Promise<PromptLibraryEntry[]> {
    const response = await this.authedFetch(`${this.baseUrl}/prompts/library`, {
      method: "GET",
    });
    if (!response.ok) {
      throw new Error(`Failed to fetch prompt library: ${response.statusText}`);
    }
    const data = await response.json();
    return data.entries ?? [];
  }

  /** 创建提示词 */
  async createPrompt(body: PromptCreateRequest): Promise<Prompt> {
    const response = await this.authedFetch(`${this.baseUrl}/prompts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(
        err.detail || `Failed to create prompt: ${response.statusText}`,
      );
    }
    return response.json();
  }

  /** 更新提示词 */
  async updatePrompt(
    id: string,
    body: Partial<PromptCreateRequest>,
  ): Promise<Prompt> {
    const response = await this.authedFetch(`${this.baseUrl}/prompts/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(
        err.detail || `Failed to update prompt: ${response.statusText}`,
      );
    }
    return response.json();
  }

  /** 删除提示词 */
  async deletePrompt(id: string): Promise<{ success: boolean }> {
    const response = await this.authedFetch(`${this.baseUrl}/prompts/${id}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      throw new Error(`Failed to delete prompt: ${response.statusText}`);
    }
    return response.json();
  }

  // ========== Settings API ==========

  /** 获取应用设置 */
  async getSettings(): Promise<AppSettings> {
    const response = await this.authedFetch(`${this.baseUrl}/settings`, {
      method: "GET",
    });
    if (!response.ok) {
      throw new Error(`Failed to fetch settings: ${response.statusText}`);
    }
    return response.json();
  }

  /** 更新用户偏好 */
  async updatePreferences(
    body: Partial<UserPreferences>,
  ): Promise<{ success: boolean; preferences: UserPreferences }> {
    const response = await this.authedFetch(
      `${this.baseUrl}/settings/preferences`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) {
      throw new Error(`Failed to update preferences: ${response.statusText}`);
    }
    return response.json();
  }

  // ========== Attachment API ==========

  /**
   * 上传对话附件（图片/文件），返回相对 URL 和元信息。
   * 返回的 url 为 /uploads/... 相对路径，调用方需自行拼接 baseUrl。
   */
  async uploadAttachment(file: File): Promise<{
    url: string;
    filename: string;
    size: number;
    contentType: string;
    isImage: boolean;
  }> {
    const formData = new FormData();
    formData.append("file", file);

    const headers: HeadersInit = {};
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    let response = await fetch(`${this.baseUrl}/chats/attachments/upload`, {
      method: "POST",
      headers,
      body: formData,
    });

    // 401 自动刷新重试
    if (response.status === 401 && this.refreshToken) {
      const newToken = await this.tryRefreshToken();
      if (newToken) {
        headers["Authorization"] = `Bearer ${newToken}`;
        response = await fetch(`${this.baseUrl}/chats/attachments/upload`, {
          method: "POST",
          headers,
          body: formData,
        });
      }
    }

    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(
        err.detail || `Failed to upload attachment: ${response.statusText}`,
      );
    }

    return response.json();
  }

  // ========== 用量与追踪 ==========

  /**
   * 统计窗口内的用量、成本与失败情况
   * GET /metrics/usage
   */
  async getUsage(days?: number): Promise<UsageSummary> {
    const query = days ? `?days=${days}` : "";
    const response = await this.authedFetch(
      `${this.baseUrl}/metrics/usage${query}`,
      { method: "GET" },
    );
    if (!response.ok) {
      throw new Error(`Failed to fetch usage: ${response.statusText}`);
    }
    return response.json();
  }

  /**
   * 最近若干次回答的 trace 概览
   * GET /metrics/traces
   */
  async getTraces(chatId?: string, limit = 20): Promise<TraceSummary[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (chatId) params.set("chat_id", chatId);
    const response = await this.authedFetch(
      `${this.baseUrl}/metrics/traces?${params.toString()}`,
      { method: "GET" },
    );
    if (!response.ok) {
      throw new Error(`Failed to fetch traces: ${response.statusText}`);
    }
    return response.json();
  }

  /**
   * 单棵 trace 的 span 树
   * GET /metrics/traces/{traceId}
   */
  async getTrace(traceId: string): Promise<TraceDetail> {
    const response = await this.authedFetch(
      `${this.baseUrl}/metrics/traces/${traceId}`,
      { method: "GET" },
    );
    if (!response.ok) {
      throw new Error(`Failed to fetch trace: ${response.statusText}`);
    }
    return response.json();
  }

  /**
   * 提交或更新一条消息反馈（同一条消息只保留一份）
   * POST /feedback
   */
  async submitFeedback(body: {
    messageId: string;
    rating: "up" | "down";
    reason?: string;
    comment?: string;
    expectedAnswer?: string;
  }): Promise<MessageFeedback> {
    const response = await this.authedFetch(`${this.baseUrl}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`Failed to submit feedback: ${response.statusText}`);
    }
    return response.json();
  }

  /**
   * 撤销反馈（再次点击同一个按钮）
   * DELETE /feedback/{messageId}
   */
  async revokeFeedback(messageId: string): Promise<{ success: boolean }> {
    const response = await this.authedFetch(
      `${this.baseUrl}/feedback/${messageId}`,
      { method: "DELETE" },
    );
    if (!response.ok) {
      throw new Error(`Failed to revoke feedback: ${response.statusText}`);
    }
    return response.json();
  }

  /**
   * 批量取回某些消息的反馈状态，用于切换会话后点亮按钮
   * GET /feedback?messageIds=a,b,c
   */
  async getFeedback(messageIds: string[]): Promise<MessageFeedback[]> {
    if (!messageIds.length) return [];
    const params = new URLSearchParams({ messageIds: messageIds.join(",") });
    const response = await this.authedFetch(
      `${this.baseUrl}/feedback?${params.toString()}`,
      { method: "GET" },
    );
    if (!response.ok) {
      throw new Error(`Failed to fetch feedback: ${response.statusText}`);
    }
    const payload = await response.json();
    return payload.items ?? [];
  }

  /**
   * 满意度概览
   * GET /feedback/summary
   */
  async getFeedbackSummary(): Promise<FeedbackSummary> {
    const response = await this.authedFetch(
      `${this.baseUrl}/feedback/summary`,
      {
        method: "GET",
      },
    );
    if (!response.ok) {
      throw new Error(
        `Failed to fetch feedback summary: ${response.statusText}`,
      );
    }
    return response.json();
  }
}

// 导出默认实例
export const apiClient = new ApiClient();
