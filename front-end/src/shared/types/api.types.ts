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

// ========== 系统提示词注册表 ==========

/** archived 的版本只作对照，不建议启用 */
export type PromptStatus = "active" | "candidate" | "archived";

export interface PromptLibraryVersion {
  version: string;
  label: string;
  status: PromptStatus;
  notes: string;
  body: string;
  chars: number;
  isActive: boolean;
}

/** 一类系统提示词及其全部版本。只读——版本是仓库里的文件，不能在线改 */
export interface PromptLibraryEntry {
  key: string;
  purpose: string;
  activeVersion: string;
  /** 为 false 说明这类提示词没接版本开关，界面上不提供切换 */
  switchable: boolean;
  /** 为 true 才能通过 ChatRequest.prompt_version 单次覆盖（只有对话系统提示词开放） */
  requestOverridable: boolean;
  setting: string | null;
  placeholders: string[];
  flags: string[];
  versions: PromptLibraryVersion[];
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

/**
 * 后端实际注册了哪些能力。不是"开关值"而是"能不能用"——
 * web_search 开关打开但没配 API key 时那个工具根本不注册，这里报 false。
 *
 * 前端据此改变行为而不只是显示状态：`readAttachment` 为 false 时文本附件必须
 * 继续内联全文，否则模型只会拿到一个它读不了的路径。
 */
export interface ServerCapabilities {
  calculate: boolean;
  readAttachment: boolean;
  webSearch: boolean;
  writeKnowledge: boolean;
  toolHistory: boolean;
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
  /** 旧版后端不返回这一块，调用方必须容忍它缺失 */
  capabilities?: ServerCapabilities;
}

// ========== API请求类型 ==========

export interface ChatRequest {
  prompt: string;
  model?: string;
  chat_id?: string;
  use_rag?: boolean;
  message_id?: string;
  /** 只对这一次请求生效的系统提示词版本；不传则用服务端默认版本 */
  prompt_version?: string;
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
    | "guardrail"
    | "cache_hit"
    | "done"
    | "error";
  content?: string;
  chat_id?: string;
  tool?: string;
  input?: Record<string, unknown>;
  /** 工具调用所在的 Agent 轮次，从 1 开始 */
  round?: number;
  /**
   * tool_result 的执行结果分级：ok / invalid_arguments / unavailable。
   * round 0 的预检索走的是另一套取值，检索失败时报 error。
   */
  status?: "ok" | "invalid_arguments" | "unavailable" | "error";
  /** tool_rounds_ended 携带：已经用掉的工具轮次数 */
  rounds?: number;
  /** citations 携带：本次检索命中的引用 */
  items?: Citation[];
  /** context_compacted 携带：被摘要压缩 / 原样保留的历史条数 */
  summarized?: number;
  kept?: number;
  /** guardrail 携带：命中的注入规则名（只有规则名，不含命中原文） */
  findings?: string[];
  /** guardrail 携带：命中规则的累计分数 */
  score?: number;
  /** guardrail 携带：被改写掉的协议标记数量 */
  masked?: number;
  /** guardrail 携带：该段资料是否因分数超阈值而未注入 */
  blocked?: boolean;
  /** cache_hit 携带：与缓存问题的相似度、是否精确匹配、省下的 token */
  similarity?: number;
  exact?: boolean;
  tokensSaved?: number;
  done?: boolean;
  error?: string;
  message_id?: string;
}

/** 检索资料命中提示注入规则时的提示信息 */
export interface GuardrailNotice {
  findings: string[];
  score: number;
  masked: number;
  blocked: boolean;
}

/**
 * 一步工具执行。两条来源共用这个形状（后端 tool_history.serialize 的字段名
 * 就是照 SSE 事件起的）：
 *
 * - 流式期间由 tool_start / tool_result 实时拼出来，没有 id 与结果摘要；
 * - 刷新之后从 GET /chats/:id/tool-steps 取回，字段齐全。
 *
 * 所以除了 round / callIndex / tool 之外都是可选的，渲染时要按"有就显示"处理。
 */
export interface ToolStep {
  /** 落库后的步骤 id；流式期间实时拼出来的步骤没有 */
  id?: string;
  /** 触发这一回合的**用户**消息 id，不是回答那条的 id */
  messageId?: string | null;
  /** Agent 轮次。0 = 系统预检索，不是模型自己决定要查的 */
  round: number;
  callIndex: number;
  tool: string;
  /** 流式期间 tool_start 先到、tool_result 才带回状态，所以会短暂为空 */
  status?: "ok" | "invalid_arguments" | "unavailable" | "error";
  input?: Record<string, unknown>;
  citations?: Citation[];
  /** 结果原文长度。只有落库的轨迹有 */
  resultChars?: number;
  /** 结果摘要，长度受后端 TOOL_HISTORY_STEP_CHARS 约束。只有落库的轨迹有 */
  resultPreview?: string;
  createdAt?: string | null;
}

// ========== 消息反馈 ==========

/** 差评原因标签，与后端 services/feedback_service.py 的 REASONS 对齐 */
export type FeedbackReason =
  | "inaccurate"
  | "no_citation"
  | "off_topic"
  | "bad_format"
  | "other";

export interface MessageFeedback {
  messageId: string;
  rating: "up" | "down";
  reason?: FeedbackReason | null;
  comment?: string | null;
  expectedAnswer?: string | null;
}

export interface FeedbackSummary {
  up: number;
  down: number;
  rated: number;
  /** 没有任何反馈时为 null——区分「没人评价」和「评价全是差评」 */
  satisfaction: number | null;
  downReasons: { reason: string; count: number }[];
  /** 还没导出成回归用例的差评数 */
  pendingExport: number;
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
  /** 语义缓存统计。存在后端进程内存里，重启归零，也不受 rangeDays 约束 */
  cache: CacheStats;
}

export interface CacheStats {
  enabled: boolean;
  hits: number;
  misses: number;
  /** 没查过时为 null——不要显示成 0% 命中率 */
  hitRate: number | null;
  tokensSaved: number;
  threshold: number;
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

export type NavTab =
  | "dashboard"
  | "chat"
  | "traces"
  | "knowledge"
  | "prompts"
  | "settings";

export interface UIMessage extends Omit<Message, "chatId" | "createdAt"> {
  sessionId: string;
  timestamp: string;
  /** RAG 引用来源，仅 assistant 消息可能有；本轮生成结束后不落库 */
  citations?: Citation[];
  /** 本轮检索资料命中的注入规则（护栏已中和，仅作提示） */
  guardrail?: GuardrailNotice;
  /** 服务端落库后的消息 id(流式 done 时回写),用于关联运行轨迹 */
  messageId?: string;
}

/** 知识库检索调试结果(对齐后端 RetrievedChunk.as_dict) */
export interface KnowledgeQueryChunk {
  document_id: string;
  document_name: string;
  chunk_index: number;
  chunk_range?: [number, number] | null;
  content: string;
  /** 稠密通道得分;null 表示仅稀疏命中 */
  score: number | null;
  /** RRF 融合得分 */
  fusion_score?: number | null;
  /** 命中通道,如 ["dense","sparse"] */
  channels?: string[];
}

export interface KnowledgeQueryResult {
  query: string;
  results: KnowledgeQueryChunk[];
  total: number;
}
