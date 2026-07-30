// 基于Python后端的数据模型定义TypeScript类型

// ========== Chat相关类型 ==========

export interface Message {
  id: string
  content: string
  role: 'user' | 'assistant' | 'system'
  model?: string
  chatId: string
  createdAt: string
}

export interface Chat {
  id: string
  title: string
  userId: string
  createdAt: string
  updatedAt: string
}

export interface ChatSession {
  id: string
  title: string
  date: string
  pinned: boolean
}

// ========== Knowledge相关类型 ==========

export interface KnowledgeDocument {
  id: string
  name: string
  size: number
  chunks: number
  status: 'indexed' | 'processing' | 'failed'
  createdAt: string
}

// ========== API请求类型 ==========

export interface ChatRequest {
  prompt: string
  model?: string
  chat_id?: string
}

export interface CreateChatRequest {
  title?: string
}

export interface CompletionResponse {
  success: boolean
  data: string
  chat_id: string
}

// ========== SSE流式响应类型 ==========

export interface StreamChunk {
  content?: string
  chat_id?: string
  done?: boolean
  error?: string
}

// ========== UI相关类型 ==========

export type NavTab = 'chat' | 'knowledge' | 'prompts' | 'settings'

export interface UIMessage extends Omit<Message, 'chatId' | 'createdAt'> {
  sessionId: string
  timestamp: string
}
