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
} from "../types/api.types";

/**
 * FastAPI后端API客户端
 * 对接Python后端的所有端点
 */
export class ApiClient {
  private baseUrl: string;
  private token: string | null = null;

  constructor(baseUrl: string = "http://localhost:3000") {
    this.baseUrl = baseUrl;
    // 从 localStorage 读取 token
    this.token = localStorage.getItem("access_token");
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
    return data;
  }

  /**
   * 获取当前用户信息
   * GET /auth/me
   */
  async getCurrentUser(): Promise<User> {
    const response = await fetch(`${this.baseUrl}/auth/me`, {
      method: "GET",
      headers: this.getHeaders(),
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
    const response = await fetch(`${this.baseUrl}/auth/logout`, {
      method: "POST",
      headers: this.getHeaders(),
    });

    if (!response.ok) {
      throw new Error(`Failed to logout: ${response.statusText}`);
    }

    this.setToken(null);
    localStorage.removeItem("user");
    return response.json();
  }

  /**
   * 刷新 token
   * POST /auth/refresh
   */
  async refreshToken(): Promise<TokenResponse> {
    const response = await fetch(`${this.baseUrl}/auth/refresh`, {
      method: "POST",
      headers: this.getHeaders(),
    });

    if (!response.ok) {
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
    const response = await fetch(`${this.baseUrl}/chats`, {
      method: "GET",
      headers: this.getHeaders(),
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
    const response = await fetch(`${this.baseUrl}/chats/${chatId}/messages`, {
      method: "GET",
      headers: this.getHeaders(),
    });

    if (!response.ok) {
      throw new Error(`Failed to fetch messages: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * 创建新对话
   * POST /chats
   */
  async createChat(
    request: CreateChatRequest = {},
  ): Promise<{ id: string; title: string }> {
    const response = await fetch(`${this.baseUrl}/chats`, {
      method: "POST",
      headers: this.getHeaders(),
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
    const response = await fetch(`${this.baseUrl}/chats/completions`, {
      method: "POST",
      headers: this.getHeaders(),
      body: JSON.stringify(request),
    });

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
    const response = await fetch(`${this.baseUrl}/chats/completions/stream`, {
      method: "POST",
      headers: this.getHeaders(),
      body: JSON.stringify(request),
      signal,
    });

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
          } catch (e) {
            console.error("Failed to parse SSE data:", data, e);
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  /**
   * 重命名对话 (如果后端支持)
   * 注意: 当前Python后端没有这个端点,这里预留接口
   */
  async renameChat(chatId: string, title: string): Promise<void> {
    // 暂时只在本地处理
    console.log(`Rename chat ${chatId} to "${title}" (local only)`);
  }

  /**
   * 删除对话 (如果后端支持)
   * 注意: 当前Python后端没有这个端点,这里预留接口
   */
  async deleteChat(chatId: string): Promise<void> {
    // 暂时只在本地处理
    console.log(`Delete chat ${chatId} (local only)`);
  }

  // ========== Knowledge API ==========

  /**
   * 获取知识库文档列表
   * GET /knowledge/documents
   */
  async getDocuments(): Promise<KnowledgeDocument[]> {
    const response = await fetch(`${this.baseUrl}/knowledge/documents`, {
      method: "GET",
      headers: this.getHeaders(),
    });

    if (!response.ok) {
      throw new Error(`Failed to fetch documents: ${response.statusText}`);
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
}

// 导出默认实例
export const apiClient = new ApiClient();
