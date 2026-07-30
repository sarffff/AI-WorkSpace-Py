import type {
  Message,
  Chat,
  ChatRequest,
  CreateChatRequest,
  CompletionResponse,
  StreamChunk,
  KnowledgeDocument,
} from '../types/api.types'

/**
 * FastAPI后端API客户端
 * 对接Python后端的所有端点
 */
export class ApiClient {
  private baseUrl: string

  constructor(baseUrl: string = 'http://localhost:3000') {
    this.baseUrl = baseUrl
  }

  // ========== Chat API ==========

  /**
   * 获取所有对话列表
   * GET /chats
   */
  async getChats(): Promise<Chat[]> {
    const response = await fetch(`${this.baseUrl}/chats`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to fetch chats: ${response.statusText}`)
    }

    return response.json()
  }

  /**
   * 获取指定对话的消息列表
   * GET /chats/{chat_id}/messages
   */
  async getMessages(chatId: string): Promise<Message[]> {
    const response = await fetch(`${this.baseUrl}/chats/${chatId}/messages`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to fetch messages: ${response.statusText}`)
    }

    return response.json()
  }

  /**
   * 创建新对话
   * POST /chats
   */
  async createChat(request: CreateChatRequest = {}): Promise<{ id: string; title: string }> {
    const response = await fetch(`${this.baseUrl}/chats`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(request),
    })

    if (!response.ok) {
      throw new Error(`Failed to create chat: ${response.statusText}`)
    }

    return response.json()
  }

  /**
   * 非流式对话
   * POST /chats/completions
   */
  async sendMessage(request: ChatRequest): Promise<CompletionResponse> {
    const response = await fetch(`${this.baseUrl}/chats/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(request),
    })

    if (!response.ok) {
      throw new Error(`Failed to send message: ${response.statusText}`)
    }

    return response.json()
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
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(request),
      signal,
    })

    if (!response.ok) {
      throw new Error(`Failed to stream message: ${response.statusText}`)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      throw new Error('Response body is null')
    }

    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (true) {
        const { done, value } = await reader.read()

        if (done) {
          break
        }

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed || !trimmed.startsWith('data:')) {
            continue
          }

          const data = trimmed.slice(5).trim()
          if (!data) {
            continue
          }

          try {
            const chunk: StreamChunk = JSON.parse(data)
            yield chunk
          } catch (e) {
            console.error('Failed to parse SSE data:', data, e)
          }
        }
      }
    } finally {
      reader.releaseLock()
    }
  }

  /**
   * 重命名对话 (如果后端支持)
   * 注意: 当前Python后端没有这个端点,这里预留接口
   */
  async renameChat(chatId: string, title: string): Promise<void> {
    // 如果后端添加了这个端点,取消注释:
    // const response = await fetch(`${this.baseUrl}/chats/${chatId}`, {
    //   method: 'PATCH',
    //   headers: {
    //     'Content-Type': 'application/json',
    //   },
    //   body: JSON.stringify({ title }),
    // })
    //
    // if (!response.ok) {
    //   throw new Error(`Failed to rename chat: ${response.statusText}`)
    // }
    
    // 暂时只在本地处理
    console.log(`Rename chat ${chatId} to "${title}" (local only)`)
  }

  /**
   * 删除对话 (如果后端支持)
   * 注意: 当前Python后端没有这个端点,这里预留接口
   */
  async deleteChat(chatId: string): Promise<void> {
    // 如果后端添加了这个端点,取消注释:
    // const response = await fetch(`${this.baseUrl}/chats/${chatId}`, {
    //   method: 'DELETE',
    // })
    //
    // if (!response.ok) {
    //   throw new Error(`Failed to delete chat: ${response.statusText}`)
    // }
    
    // 暂时只在本地处理
    console.log(`Delete chat ${chatId} (local only)`)
  }

  // ========== Knowledge API ==========

  /**
   * 获取知识库文档列表
   * GET /knowledge/documents
   */
  async getDocuments(): Promise<KnowledgeDocument[]> {
    const response = await fetch(`${this.baseUrl}/knowledge/documents`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to fetch documents: ${response.statusText}`)
    }

    return response.json()
  }

  // ========== 健康检查 ==========

  /**
   * 检查服务器状态
   */
  async ping(): Promise<boolean> {
    try {
      const response = await fetch(`${this.baseUrl}/chats`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
        },
      })
      return response.ok
    } catch {
      return false
    }
  }
}

// 导出默认实例
export const apiClient = new ApiClient()
