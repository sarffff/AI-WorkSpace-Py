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

/** 文档可见性。workspace = 工作区共享（仅 admin 可增删），private = 仅上传者可见 */
export type DocumentVisibility = "workspace" | "private";

export interface KnowledgeDocument {
  id: string;
  name: string;
  size: number;
  chunks: number;
  status: "indexed" | "processing" | "failed";
  createdAt: string;
  /** 旧版后端不返回，缺省按共享处理（那是这一列加上去之前的语义） */
  visibility?: DocumentVisibility;
  /**
   * 是不是当前用户自己上传的。**由后端算**而不是前端比 user_id：
   * 前端手上不一定有当前用户 id，而这个判断错了就是一个能点但会 403 的删除按钮。
   */
  isOwn?: boolean;
  /**
   * 上传者显示名。列表里出现别人的个人文档时（只有 admin 会）用来说明"该找谁"，
   * 因为那些文档 admin 看得见但删不掉。
   */
  ownerName?: string | null;
  /**
   * 原上传者的账号已被删除，这一篇是被收编成共享文档的。
   *
   * 后端判据是"共享但没有上传者"——正常上传总会带 uploader_id。
   * 界面要标出来：这不是团队有意发布的资料，而是某个离开的人留下的，
   * 值得看一眼再决定删或留。
   */
  inherited?: boolean;
  /**
   * 这一篇会不会进**当前用户**的检索。
   *
   * admin 的列表里包含全体成员的个人文档，而那些**不参与他的检索**
   * （后端 `HybridRetriever._retrievable_by` 不认角色）。所以"可见"和"会被引用"
   * 是两件事，界面必须分开说——否则 admin 看到一份文档却问不出内容，
   * 只会以为检索坏了。
   *
   * 旧版后端不返回时缺省 true，那时两者本来就是一回事。
   */
  retrievable?: boolean;
}

export interface UploadDocumentResponse extends KnowledgeDocument {
  /** true 表示内容哈希命中已有文档,本次没有重复索引 */
  duplicate: boolean;
}

// ========== Workspace相关类型 ==========

export interface WorkspaceMember {
  id: string;
  name: string;
  /** `member` 是历史值，语义等同 `user`（见 WorkspaceInfo.role） */
  role: "admin" | "user" | "member";
}

export interface WorkspaceInfo {
  id: string;
  name: string;
  /**
   * ``admin`` 管共享文档与邀请码；``user`` 只管自己的私有文档。
   * ``member`` 是历史值（语义等同 ``user``），存量账号上还可能出现，
   * 所以判断权限一律用 ``isAdmin`` 而不是比这个字段。
   */
  role: "admin" | "user" | "member";
  /** 唯一的权限判据。后端算好，前端不要自己比 role */
  isAdmin?: boolean;
  memberCount: number;
  members: WorkspaceMember[];
  /** 邀请码只发给 admin；user 拿到的是 null，界面上就不该出现它 */
  inviteCode?: string | null;
}

export interface JoinWorkspaceResponse {
  success: boolean;
  workspace: WorkspaceInfo;
  /**
   * 原空间里这个人能看到的文档数。加入是**换空间**不是多一个空间
   * （后端 ``User.workspace_id`` 是单值外键），这些文档不会被删，
   * 但加入后不再出现在任何检索里——必须提示，静默切换会让人以为资料丢了。
   */
  leftBehindDocuments: number;
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
  /** 旧版后端不返回这一块 */
  delegation?: DelegationCapability;
  /** 旧版后端不返回这一块 */
  approval?: ApprovalCapability;
  /** 旧版后端不返回这一块 */
  fileTypes?: FileTypesCapability;
}

/**
 * 能上传哪些文件。**这里是唯一来源，前端不要再各自维护扩展名清单。**
 *
 * 改动之前这份清单在前后端共有六处副本且已经互相矛盾：`.html` 三处前端都收、
 * 两处后端都不收（知识库上传直接 400）；`.svg` 前端当图片收、后端出于安全
 * 故意排除，而图片分支没有兜底，所以必然报"图片上传失败"。
 * 判据与排除理由见后端 `services/file_types.py`。
 */
export interface FileTypesCapability {
  /** 能按纯文本读取、内联进 prompt 的 */
  text: string[];
  /** 用 <img> 渲染的。不含 svg（可内嵌 script） */
  image: string[];
  /** 要专门解析器的二进制文档，走知识库链路 */
  document: string[];
  /** 知识库上传用的 accept：text + document，不含图片 */
  knowledgeAccept: string;
  /** 对话附件用的 accept：text + image + document */
  attachmentAccept: string;
}

/**
 * 人工审批的配置。``mode`` 为 "off" 时后端不会发出 approval_required，
 * 界面上就不该出现审批卡片，也不该去查待审批列表。
 */
export interface ApprovalCapability {
  /** off = 不审批；write = 写操作要人点同意；listed = 由服务端白名单决定 */
  mode: "off" | "write" | "listed";
  /** 实际受审批的工具名 */
  tools: string[];
  /** 快照是否开启。关着的话审批不可能生效（恢复要跨请求） */
  checkpoints: boolean;
}

/**
 * 多代理委派的配置。``mode`` 为 "off" 时后端根本不注册 delegate 工具，
 * 界面上不该出现任何和子代理有关的东西。
 */
export interface DelegationCapability {
  /** off = 单代理；augment = 主代理保留全部工具并多一个 delegate；supervisor = 专用工具归子代理 */
  mode: "off" | "augment" | "supervisor";
  roles: string[];
  maxDelegations: number;
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
    /**
     * 回合开始，携带 runId。在**任何东西可能断掉之前**就发出——断线之后
     * runId 是唯一的接续凭证，而 approval_required / clarification 那两类
     * 事件恰好只在"没断线"的情形下才会到。
     */
    | "run_started"
    /** 接续成功，携带从第几轮接上 */
    | "run_resumed"
    | "tool_start"
    | "tool_result"
    | "tool_rounds_ended"
    | "citations"
    | "context_compacted"
    | "guardrail"
    | "cache_hit"
    | "agent_step"
    | "agent_state"
    | "approval_required"
    | "approval_resolved"
    | "clarification"
    | "clarification_answered"
    | "done"
    | "error";
  content?: string;
  chat_id?: string;
  tool?: string;
  input?: Record<string, unknown>;
  /** 工具调用所在的 Agent 轮次，从 1 开始 */
  round?: number;
  /**
   * agent_step / agent_state 携带：哪个子代理。
   * 这两类事件都发生在主代理的一次 delegate 调用内部。
   */
  agent?: string;
  /** agent_step 携带：这一步是开始还是结束。子代理的步骤是成批发出的，两者紧邻 */
  phase?: "tool_start" | "tool_result";
  /**
   * agent_step 携带：子代理**自己**的轮次。
   * 和 ``round``（主代理轮次）分开：混用会让 researcher 的第 1 轮排到主代理
   * 第 1 轮旁边，看起来像并行调用。
   */
  agentRound?: number;
  /** agent_state / tool_rounds_ended 携带：子代理或主代理已跑的轮次数 */
  rounds?: number;
  /** agent_state 携带：子代理做过的工具步骤数，以及是否因轮次用尽而截断 */
  steps?: number;
  truncated?: boolean;
  /**
   * approval_required / approval_resolved / agent_state 携带：执行记录 id。
   * 审批要靠它调 POST /chats/runs/{runId}/resume——中断活在数据库里，
   * 不活在那条已经断掉的 SSE 连接里，所以这个 id 是唯一的接续凭证。
   */
  runId?: string;
  /**
   * approval_required 携带：给人看的参数预览。
   * 已在服务端过 mask_markup 并截断——这些值是模型写的，可能整段来自它刚抓的网页。
   */
  preview?: Record<string, unknown>;
  /** approval_required 携带：批准之后会发生什么 */
  reason?: string;
  /** approval_resolved 携带：这次裁决是同意还是拒绝 */
  approved?: boolean;
  /** approval_required 携带：这份快照的 seq，调试用 */
  checkpoint?: number | null;
  /** clarification 携带：模型抛回给用户的问题 */
  question?: string;
  /**
   * clarification 携带：这次澄清能不能**接着原来那一轮**继续。
   *
   * `true` 时该调 `POST /chats/runs/{runId}/answer` 把答案送回去——模型手里
   * 还留着它问问题之前检索到的一切。缺这个键（没开 checkpoint）时只能退回旧
   * 行为：把回答当成新一轮发出去，代价是前面几轮的工具结果全部丢掉。
   */
  resumable?: boolean;
  /** approval_required 携带：这一回合最终回答将要落在哪条 assistant 消息上 */
  message_id?: string;
  /** SSE 的子代理状态会额外出现 started / completed / failed */
  status?:
    | "ok"
    | "invalid_arguments"
    | "unavailable"
    | "error"
    | "started"
    | "completed"
    | "failed";
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
  /** 子代理名称。无此字段表示主代理自己执行的步骤 */
  agentRole?: string | null;
  /** 子代理自己的工具轮次；``round`` 始终是外层主代理轮次 */
  agentRound?: number;
  /**
   * 流式期间 tool_start 先到、tool_result 才带回状态，所以会短暂为空。
   * ``repeated`` = 同参数调用被拦下；``rejected`` = 人工审批被拒，工具没执行。
   */
  status?:
    | "ok"
    | "invalid_arguments"
    | "unavailable"
    | "error"
    | "repeated"
    | "rejected";
  /** 归属的执行记录 id。用来把一条轨迹关联回"这次执行被打断过吗" */
  runId?: string | null;
  input?: Record<string, unknown>;
  citations?: Citation[];
  /** 结果原文长度。只有落库的轨迹有 */
  resultChars?: number;
  /** 结果摘要，长度受后端 TOOL_HISTORY_STEP_CHARS 约束。只有落库的轨迹有 */
  resultPreview?: string;
  createdAt?: string | null;
}

// ========== Agent 线上指标 ==========

/**
 * 委派 / 审批 / 子代理的线上指标。
 *
 * 与离线评估（eval/reports）的分工：这里是**真实流量**上发生的事，样本是用户
 * 真的问过的问题；那边是固定数据集上的可复现对比。前者回答"线上现在什么情况"，
 * 后者回答"改这一版有没有变好"。两者都需要，但不能互相替代。
 */
export interface AgentMetrics {
  rangeDays: number;
  /**
   * 快照开关。关着的时候 agent_runs 根本不写行，所有数字都是 0——
   * 界面必须显示"未开启"而不是画一个全零面板，那两件事看起来一样、含义完全不同。
   */
  enabled: boolean;
  delegationMode: string;
  approvalMode: string;
  totals: AgentMetricsTotals;
  byRole: AgentRoleMetrics[];
  /** 空数组表示埋点关闭（成本与延迟来自 trace_spans） */
  comparison: DelegationComparison[];
}

export interface AgentMetricsTotals {
  runs: number;
  delegatedRuns: number;
  delegations: number;
  /** null = 窗口内没有任何执行。不能显示成 0%，那会被读成"从来不委派" */
  delegationRate: number | null;
  interrupts: number;
  interruptedRuns: number;
  failedRuns: number;
  waitingApproval: number;
  avgRounds: number | null;
}

export interface AgentRoleMetrics {
  role: string | null;
  runs: number;
  failed: number;
  failureRate: number | null;
  avgRounds: number | null;
}

/**
 * 委派 vs 未委派的对比。这是整个面板真正要看的东西——
 * 单看委派率什么都说明不了，得知道它多花了几倍的钱、慢了几倍。
 */
export interface DelegationComparison {
  delegated: boolean;
  currency: string | null;
  runs: number;
  avgRounds: number | null;
  cost: number | null;
  avgCost: number | null;
  promptTokens: number;
  completionTokens: number;
  /** 每次回答的平均总耗时（根 span），不是每个 span 的平均 */
  avgTurnMs: number | null;
}

// ========== 人工审批与可恢复执行 ==========

/**
 * 一个卡在审批上的执行。
 *
 * 刷新页面之后 SSE 里的 approval_required 已经不存在了，这是唯一能把审批卡片
 * 找回来的地方——中断活在数据库里，不活在那条连接里。
 */
export interface PendingApproval {
  runId: string;
  chatId: string;
  messageId?: string | null;
  round: number;
  /** 这次执行被打断过几次。1 以上说明同一回合里有多个写操作 */
  interrupts: number;
  updatedAt?: string | null;
  /** 等待批准的工具名 */
  tool?: string | null;
  /** 批准之后会发生什么 */
  reason: string;
  /** 参数预览，已在服务端脱敏截断 */
  preview: Record<string, unknown>;
}

/**
 * 断线留下、可以接着跑的执行。
 *
 * 与 `PendingApproval` 是两种东西：那个在等人做决定，这个只是连接断了。
 * 所以这里没有 tool / reason / preview——没有什么要给人看、要人裁决的。
 */
export interface ResumableRun {
  runId: string;
  chatId: string;
  messageId?: string | null;
  /** 断在第几轮。接续会从这一轮之后继续 */
  round: number;
  updatedAt?: string | null;
}

/** 一次执行的详情。子代理是它的 children */
export interface AgentRunDetail {
  runId: string;
  chatId: string;
  messageId?: string | null;
  agentRole?: string | null;
  status: "running" | "waiting_approval" | "done" | "failed" | "abandoned";
  rounds: number;
  delegations: number;
  interrupts: number;
  model?: string | null;
  promptRef?: string | null;
  traceId?: string | null;
  errorType?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  children: AgentRunChild[];
  checkpoints: AgentCheckpointInfo[];
}

export interface AgentRunChild {
  runId: string;
  agentRole?: string | null;
  status: string;
  rounds: number;
  errorType?: string | null;
}

/** 快照目录项。只有元信息——正文是整段 messages，调试接口没理由再吐一遍 */
export interface AgentCheckpointInfo {
  seq: number;
  phase: "pre_tools" | "waiting_approval" | "post_tools";
  round: number;
  bytes: number;
  interrupt?: Record<string, unknown> | null;
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

// ========== 长期记忆 ==========

/**
 * 跨会话长期记忆。每轮回答结束后由辅助模型从对话里抽取事实与偏好，
 * 注入之后所有会话的系统上下文——删除即立即停止注入。
 */
export interface UserMemory {
  id: string;
  /** fact = 客观事实；preference = 用户偏好 */
  kind: "fact" | "preference";
  content: string;
  /** 抽取来源的会话 id，用于追溯这句话是在哪次对话里说的 */
  chatId: string | null;
  createdAt: string;
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
    /** promptTokens 中被提供商上下文缓存命中的部分（子集，不是增量） */
    cachedTokens: number;
    /** 提供商侧上下文缓存命中率；没有回传缓存信息的调用时为 null */
    promptCacheHitRate: number | null;
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
