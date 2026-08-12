// 基于Python后端的数据模型定义TypeScript类型

// ========== Auth认证相关类型 ==========

export interface User {
  id: string;
  email: string;
  username: string | null;
  name: string | null;
  avatar: string | null;
  provider: string | null;
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RegisterRequest {
  email: string;
  username: string;
  password: string;
  name?: string;
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface TokenResponse {
  access_token: string;
  refresh_token?: string;
  token_type: string;
  expires_in: number;
}

// ========== Chat相关类型 ==========

export interface Message {
  id: string;
  content: string;
  role: "user" | "assistant" | "system";
  model?: string;
  chatId: string;
  createdAt: string;
}

export interface Chat {
  id: string;
  title: string;
  userId: string;
  createdAt: string;
  updatedAt: string;
}

export interface ChatSession {
  id: string;
  title: string;
  date: string;
  pinned: boolean;
}

// ========== Knowledge相关类型 ==========

export interface KnowledgeDocument {
  id: string;
  name: string;
  size: number;
  chunks: number;
  status: "indexed" | "processing" | "failed";
  createdAt: string;
}

// ========== Prompt相关类型 ==========

export interface Prompt {
  id: string;
  title: string;
  description: string | null;
  category: string;
  content: string;
  isPublic: boolean;
  userId: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface PromptCreateRequest {
  title: string;
  description?: string;
  category?: string;
  content: string;
  isPublic?: boolean;
}

// ========== Settings相关类型 ==========

export interface AvailableModel {
  id: string;
  label: string;
  provider: string;
}

export interface ServerSettings {
  llmBaseUrl: string;
  configuredModel: string;
  embeddingModel: string;
  redisEnabled: boolean;
  databaseUrl: string;
}

export interface UserPreferences {
  defaultModel: string;
  temperature: number;
  maxTokens: number;
  topP: number;
}

export interface AppSettings {
  server: ServerSettings;
  preferences: UserPreferences;
  availableModels: AvailableModel[];
}

// ========== API请求类型 ==========

export interface ChatRequest {
  prompt: string;
  model?: string;
  chat_id?: string;
  use_rag?: boolean;
  message_id?: string;
}

export interface CreateChatRequest {
  title?: string;
}

export interface CompletionResponse {
  success: boolean;
  data: string;
  chat_id: string;
  message_id: string;
}

// ========== SSE流式响应类型 ==========

/** 一条 RAG 引用。chunk_range 覆盖邻域扩展后的分块区间 */
export interface Citation {
  document_id: string;
  document_name: string;
  chunk_index: number;
  chunk_range?: [number, number] | number[];
  content?: string;
  /** 稠密通道的余弦相似度；仅稀疏命中时为 null */
  score?: number | null;
  fusion_score?: number;
  /** 命中来源通道：dense / sparse */
  channels?: string[];
}

export interface StreamChunk {
  type?:
    | "message_delta"
    | "tool_start"
    | "tool_result"
    | "tool_rounds_ended"
    | "citations"
    | "context_compacted"
    | "done"
    | "error";
  content?: string;
  chat_id?: string;
  tool?: string;
  input?: Record<string, unknown>;
  /** 工具调用所在的 Agent 轮次，从 1 开始 */
  round?: number;
  /** tool_result 的执行结果分级：ok / invalid_arguments / unavailable */
  status?: "ok" | "invalid_arguments" | "unavailable";
  /** tool_rounds_ended 携带：已经用掉的工具轮次数 */
  rounds?: number;
  /** citations 携带：本次检索命中的引用 */
  items?: Citation[];
  /** context_compacted 携带：被摘要压缩 / 原样保留的历史条数 */
  summarized?: number;
  kept?: number;
  done?: boolean;
  error?: string;
  message_id?: string;
}

// ========== 用量与追踪 ==========

/** 按某个维度（span 名 / 模型 / kind）聚合出的一行用量 */
export interface UsageGroup {
  name?: string | null;
  model?: string | null;
  kind?: string | null;
  calls: number;
  promptTokens: number;
  completionTokens: number;
  avgMs: number | null;
  totalMs: number;
  /** null 表示价目表里没有这个模型，即成本未知（不是零成本） */
  cost: number | null;
  currency: string | null;
  failures: number;
}

export interface UsageSummary {
  rangeDays: number;
  pricingConfigured: boolean;
  totals: {
    spans: number;
    turns: number;
    promptTokens: number;
    completionTokens: number;
    failures: number;
    /** 本地估算的 token 占比，越高说明成本数字越只能当量级参考 */
    estimatedTokenShare: number | null;
  };
  costs: { currency: string | null; amount: number | null }[];
  byName: UsageGroup[];
  byModel: UsageGroup[];
  byKind: UsageGroup[];
}

export interface TraceSummary {
  traceId: string;
  chatId: string | null;
  messageId: string | null;
  startedAt: string | null;
  durationMs: number;
  spans: number;
  promptTokens: number;
  completionTokens: number;
  cost: number | null;
  currency: string | null;
  failures: number;
}

export interface TraceSpanNode {
  id: string;
  parentId: string | null;
  name: string;
  kind: string;
  startedAt: string | null;
  durationMs: number | null;
  status: string;
  errorType: string | null;
  model: string | null;
  promptTokens: number | null;
  completionTokens: number | null;
  tokenSource: string | null;
  cost: number | null;
  currency: string | null;
  attributes: string | null;
  children: TraceSpanNode[];
}

export interface TraceDetail {
  traceId: string;
  roots: TraceSpanNode[];
}

// ========== UI相关类型 ==========

export type NavTab = "chat" | "knowledge" | "prompts" | "settings";

export interface UIMessage extends Omit<Message, "chatId" | "createdAt"> {
  sessionId: string;
  timestamp: string;
  /** RAG 引用来源，仅 assistant 消息可能有；本轮生成结束后不落库 */
  citations?: Citation[];
}
