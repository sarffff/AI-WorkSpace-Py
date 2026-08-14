import React, {
  useState,
  useRef,
  useEffect,
  useCallback,
  useMemo,
} from "react";
import { useSelector, useDispatch } from "react-redux";
import { RootState } from "@/app/providers/store";
import {
  addMessage,
  updateMessageContent,
  removeMessage,
  setIsGenerating,
  setCurrentChat,
  setMessages,
  setSessions,
  renameChat,
  truncateMessagesAfter,
  setPendingInput,
  setMessageCitations,
  setMessageServerId,
  setMessageGuardrail,
  setPromptVersion,
} from "@/entities/chat/model/chatSlice";
import { apiClient } from "@/shared/api/client";
import type {
  Citation,
  GuardrailNotice,
  MessageFeedback,
  ServerCapabilities,
  ToolStep,
} from "@/shared/types/api.types";
import { MessageContent } from "./MessageContent";
import { FeedbackButtons } from "@/features/message-feedback/ui/FeedbackButtons";
import { ToolTrace } from "@/widgets/tool-trace/ui/ToolTrace";
import {
  ChatInsightPanel,
  type InsightEvent,
} from "@/widgets/chat-insight/ui/ChatInsightPanel";
import { useNavigate } from "react-router-dom";
import {
  Send,
  Bot,
  FlaskConical,
  User,
  Sparkles,
  Paperclip,
  Square,
  BookOpen,
  X,
  Copy,
  RefreshCw,
  Pencil,
  Check,
  FileText,
  ImageIcon,
  ShieldAlert,
  Route as RouteIcon,
} from "lucide-react";

const FLUSH_INTERVAL = 60;

/** 后端工具名 -> 用户可读标签。未登记的工具名直接原样展示。 */
const TOOL_LABELS: Record<string, string> = {
  search_knowledge_base: "检索知识库",
  list_knowledge_documents: "查看知识库文档",
  read_document_chunk: "读取文档分块",
};

const toolLabel = (tool?: string) =>
  (TOOL_LABELS[tool ?? ""] ?? tool) || "工具";

/** 同一段内容可能在多轮检索里重复命中，按文档 + 分块区间去重 */
const dedupeCitations = (citations: Citation[]): Citation[] => {
  const seen = new Set<string>();
  const unique: Citation[] = [];
  for (const citation of citations) {
    const range =
      citation.chunk_range?.join("-") ?? String(citation.chunk_index);
    const key = `${citation.document_id}:${range}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(citation);
  }
  return unique;
};

const citationSpan = (citation: Citation): string => {
  const range = citation.chunk_range;
  if (!range || range.length < 2 || range[0] === range[1]) {
    return `分块 ${citation.chunk_index}`;
  }
  return `分块 ${range[0]}-${range[1]}`;
};

/** 后端只回传规则名，这里翻译成人能看懂的一句话 */
const GUARDRAIL_LABELS: Record<string, string> = {
  override_instructions: "试图覆盖系统指令",
  override_instructions_en: "试图覆盖系统指令",
  role_reassignment: "试图改写助手角色",
  role_reassignment_en: "试图改写助手角色",
  system_prompt_exfiltration: "试图套取系统提示词",
  developer_mode: "越狱话术",
  protocol_markup: "伪造工具调用标记",
  fake_role_turn: "伪造对话角色",
  fake_reference_header: "伪造参考资料表头",
  tool_directive: "指使调用工具",
  exfiltration_channel: "试图外发数据",
  secrecy: "要求隐瞒用户",
  echo_request: "要求逐字复述",
};

const guardrailSummary = (notice: GuardrailNotice): string => {
  const names = notice.findings.map((name) => GUARDRAIL_LABELS[name] ?? name);
  const unique = Array.from(new Set(names));
  if (!unique.length) {
    return `已中和 ${notice.masked} 处可被误认为协议标记的内容`;
  }
  return unique.join("、");
};

interface PendingAttachment {
  id: string;
  name: string;
  type: "text" | "image" | "pdf";
  size: number;
  content?: string;
  url?: string;
  chunks?: number;
  /**
   * 服务器上的相对路径（``/uploads/...``）。文本附件在后端开了 read_attachment 时
   * 只传这个路径，让模型按需去读；``url`` 那个字段是给 <img> 用的完整地址。
   * 两者分开是因为进提示词的应该是路径，而不是带 host 的链接。
   */
  path?: string;
}

export const ChatPage: React.FC = () => {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const {
    currentChatId,
    messagesBySession,
    sessions,
    selectedModel,
    isGenerating,
    pendingInput,
    promptVersion,
  } = useSelector((state: RootState) => state.chat);
  const messages = currentChatId
    ? (messagesBySession[currentChatId] ?? [])
    : [];
  const [input, setInput] = useState("");
  const [useRag, setUseRag] = useState(false);
  const [showThinking, setShowThinking] = useState(false);
  const [toolStatus, setToolStatus] = useState<string | null>(null);
  // 回复是否已经开始流出:一旦开始,状态条不再复活,避免看起来像新增了一条消息
  const [replyStarted, setReplyStarted] = useState(false);
  const [insightEvents, setInsightEvents] = useState<InsightEvent[]>([]);
  const [insightOpen, setInsightOpen] = useState(true);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [attaching, setAttaching] = useState(false);
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [editingMsgId, setEditingMsgId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  // 已有的赞踩状态，切换会话后要把按钮点亮回去
  const [feedbackByMessage, setFeedbackByMessage] = useState<
    Record<string, MessageFeedback>
  >({});
  /**
   * 工具轨迹。键是触发那一回合的**用户**消息 id——后端就是按它归属的。
   *
   * 放组件状态而不是 Redux：它只参与消息气泡的渲染，切走再回来重新拉一次比让它
   * 常驻 store 更简单，和 feedbackByMessage 一致。两条来源都写这里：
   * 流式期间由 SSE 事件实时追加，切换会话时从 /tool-steps 整批覆盖。
   */
  const [toolStepsByMessage, setToolStepsByMessage] = useState<
    Record<string, ToolStep[]>
  >({});
  /**
   * 后端实际开了哪些工具。附件怎么传取决于 read_attachment 在不在——
   * 前端猜不出来，猜错的后果是把附件内容彻底丢掉。
   */
  const [capabilities, setCapabilities] = useState<ServerCapabilities | null>(
    null,
  );
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const attachInputRef = useRef<HTMLInputElement>(null);

  const adjustTextareaHeight = () => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = "20px";
      const scrollHeight = textarea.scrollHeight;
      textarea.style.height = `${Math.min(scrollHeight, 192)}px`;
    }
  };

  // 从 PromptsPage 跳转过来时，把 pendingInput 填入输入框
  useEffect(() => {
    if (pendingInput) {
      setInput(pendingInput);
      dispatch(setPendingInput(null));
      setTimeout(() => {
        adjustTextareaHeight();
        textareaRef.current?.focus();
      }, 0);
    }
  }, [pendingInput, dispatch]);

  // 可在浏览器直接以文本读取的扩展名
  const TEXT_EXTENSIONS = new Set([
    "txt",
    "md",
    "py",
    "js",
    "ts",
    "tsx",
    "jsx",
    "json",
    "xml",
    "yaml",
    "yml",
    "css",
    "html",
    "csv",
    "log",
    "sh",
    "java",
    "go",
    "rs",
    "c",
    "cpp",
  ]);
  const IMAGE_EXTENSIONS = new Set([
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "svg",
  ]);

  const handleAttachClick = () => {
    attachInputRef.current?.click();
  };

  const handleAttachFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";

    const dotIdx = file.name.lastIndexOf(".");
    const ext = dotIdx >= 0 ? file.name.slice(dotIdx + 1).toLowerCase() : "";

    // 图片类：上传后端,存为附件 chip
    if (IMAGE_EXTENSIONS.has(ext)) {
      setAttaching(true);
      setAttachError(null);
      try {
        const res = await apiClient.uploadAttachment(file);
        const fullUrl = `${apiClient.getBaseUrl()}${res.url}`;
        setAttachments((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            name: res.filename,
            type: "image",
            size: file.size,
            url: fullUrl,
          },
        ]);
      } catch (err) {
        setAttachError(err instanceof Error ? err.message : "图片上传失败");
      } finally {
        setAttaching(false);
      }
      return;
    }

    // 文本类：内容读到本地作为兜底，同时上传一份让后端能按路径读取。
    // 两条路的取舍在发送时按后端能力决定，见 handleSend 里拼 apiPrompt 的那段。
    if (TEXT_EXTENSIONS.has(ext)) {
      setAttaching(true);
      setAttachError(null);
      try {
        const text = await file.text();
        const MAX_LEN = 8000;
        const truncated =
          text.length > MAX_LEN
            ? text.slice(0, MAX_LEN) + "\n...(内容已截断)"
            : text;

        // 上传是"尽力而为"：后端的白名单比这里窄（.html 就不收），
        // 传不上去只是回落到内联全文，不该让整个附件失败。
        let path: string | undefined;
        try {
          const res = await apiClient.uploadAttachment(file);
          path = res.url;
        } catch {
          path = undefined;
        }

        setAttachments((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            name: file.name,
            type: "text",
            size: file.size,
            content: truncated,
            path,
          },
        ]);
      } catch {
        setAttachError("读取文件失败，请重试。");
      } finally {
        setAttaching(false);
      }
      return;
    }

    // PDF：直接进入当前用户知识库，并自动启用 RAG。
    setAttaching(true);
    setAttachError(null);
    try {
      if (ext !== "pdf") {
        throw new Error("该文件类型暂不支持直接分析");
      }
      const res = await apiClient.uploadDocument(file);
      setUseRag(true);
      setAttachments((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          name: res.name,
          type: "pdf",
          size: file.size,
          chunks: res.chunks,
        },
      ]);
    } catch (err) {
      setAttachError(err instanceof Error ? err.message : "文件上传失败");
    } finally {
      setAttaching(false);
    }
  };

  const removeAttachment = (id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  const bufferRef = useRef({ id: "", content: "", sessionId: "" });
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const isNewSessionRef = useRef(false);
  // 引用可能先于 assistant 气泡到达（预检索发生在首轮调用之前），先攒着
  const citationsRef = useRef<Citation[]>([]);
  // 护栏命中同理：预检索阶段就可能触发，那时还没有 assistant 气泡可挂
  const guardrailRef = useRef<GuardrailNotice | null>(null);

  // 切换会话时清空待发附件
  useEffect(() => {
    setAttachments([]);
    setAttachError(null);
  }, [currentChatId]);

  // 切换到某会话时从服务器加载消息
  useEffect(() => {
    if (!currentChatId) return;
    const existing = messagesBySession[currentChatId];
    if (existing && existing.length > 0) return;

    apiClient
      .getMessages(currentChatId)
      .then((msgs) => {
        dispatch(
          setMessages({
            sessionId: currentChatId,
            messages: msgs.map((m) => ({
              id: m.id,
              sessionId: m.chatId,
              role: m.role,
              content: m.content,
              timestamp: new Date(m.createdAt).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
              }),
              model: m.model,
            })),
          }),
        );
      })
      .catch(() => {});
  }, [currentChatId, dispatch, messagesBySession]);

  // 后端能力清单只在挂载时拉一次：它由 .env 决定，改了要重启后端，
  // 也就是说它在一次会话里不会变。
  useEffect(() => {
    apiClient
      .getSettings()
      .then((settings) => setCapabilities(settings.capabilities ?? null))
      .catch(() => setCapabilities(null));
  }, []);

  // 切换会话时把已落库的工具轨迹取回来。
  // SSE 里的 tool_start / tool_result 是瞬时事件，刷新页面就没了；这是唯一
  // 能把那条时间线找回来的入口，也是"这个答案到底查过什么"事后可核对的前提。
  useEffect(() => {
    if (!currentChatId) {
      setToolStepsByMessage({});
      return;
    }
    let cancelled = false;
    apiClient
      .getToolSteps(currentChatId)
      .then((steps) => {
        if (cancelled) return;
        const grouped: Record<string, ToolStep[]> = {};
        for (const step of steps) {
          // 没有归属消息的轨迹挂不到任何气泡上（编辑重发清理时的残留），跳过
          if (!step.messageId) continue;
          (grouped[step.messageId] ??= []).push(step);
        }
        setToolStepsByMessage(grouped);
      })
      .catch(() => {
        // 取不到轨迹不影响读对话本身，按"没有轨迹"渲染
      });
    return () => {
      cancelled = true;
    };
  }, [currentChatId]);

  // 拉取本会话已有的赞踩状态。一次批量请求，不给每条消息各发一个。
  const assistantIds = messages
    .filter((m) => m.role === "assistant")
    .map((m) => m.messageId ?? m.id)
    .join(",");
  useEffect(() => {
    const ids = assistantIds ? assistantIds.split(",") : [];
    if (!ids.length) {
      setFeedbackByMessage({});
      return;
    }
    apiClient
      .getFeedback(ids)
      .then((items) => {
        setFeedbackByMessage(
          Object.fromEntries(items.map((item) => [item.messageId, item])),
        );
      })
      .catch(() => {
        // 反馈状态拉不到不影响对话，按未评价渲染
      });
  }, [assistantIds]);

  /**
   * 轨迹按用户消息归属，但要显示在回答下面，所以这里做一次映射。
   *
   * 按消息顺序把"最近一条用户消息"的轨迹挂到紧随其后的那条回答上，而不是去前端
   * 复算后端那个 uuid5 推导出来的 assistant id——推导规则一改，前端会安静地
   * 显示不出轨迹，而顺序关系不会变。
   */
  const traceByAssistant = useMemo(() => {
    const result: Record<string, ToolStep[]> = {};
    let pendingKey: string | null = null;
    for (const message of messages) {
      if (message.role === "user") {
        pendingKey = message.messageId ?? message.id;
        continue;
      }
      if (message.role === "assistant" && pendingKey) {
        const steps = toolStepsByMessage[pendingKey];
        if (steps?.length) {
          result[message.id] = [...steps].sort(
            (a, b) => a.round - b.round || a.callIndex - b.callIndex,
          );
        }
        pendingKey = null;
      }
    }
    return result;
  }, [messages, toolStepsByMessage]);

  const flushBuffer = useCallback(() => {
    const { id, content, sessionId } = bufferRef.current;
    if (!id || !content || !sessionId) return;
    dispatch(
      updateMessageContent({
        id,
        sessionId,
        content,
      }),
    );
  }, [dispatch]);

  const startFlushTimer = useCallback(() => {
    if (timerRef.current) return;
    timerRef.current = setInterval(flushBuffer, FLUSH_INTERVAL);
  }, [flushBuffer]);

  const stopFlushTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    const partial = bufferRef.current;
    // 保留已生成的部分回复,而不是整条删除:先同步最新文本与引用再收尾
    if (partial.id && partial.sessionId) {
      if (partial.content) {
        dispatch(
          updateMessageContent({
            sessionId: partial.sessionId,
            id: partial.id,
            content: partial.content,
          }),
        );
      }
      if (citationsRef.current.length) {
        dispatch(
          setMessageCitations({
            sessionId: partial.sessionId,
            messageId: partial.id,
            citations: dedupeCitations(citationsRef.current),
          }),
        );
      }
    }
    bufferRef.current = { id: "", content: "", sessionId: "" };
    stopFlushTimer();
    setShowThinking(false);
    setToolStatus(null);
    setReplyStarted(false);
    dispatch(setIsGenerating(false));
  }, [dispatch, stopFlushTimer]);

  const isNearBottom = useCallback(() => {
    const el = messagesContainerRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 150;
  }, []);

  const scrollToBottom = useCallback((smooth = true) => {
    const el = messagesContainerRef.current;
    if (!el) return;
    if (smooth) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }, []);

  useEffect(() => {
    if (!isNearBottom()) scrollToBottom();
  });

  /**
   * 核心：把用户消息发给后端并流式渲染 AI 回复。
   * 调用前调用方需自行把 user 消息加入 Redux（addMessage）。
   * 这里只负责生成 assistant 消息。
   */
  const runCompletion = useCallback(
    async (
      sessionId: string,
      userMsg: string,
      userMessageId: string,
    ) => {
      const ts = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      dispatch(setIsGenerating(true));
      setShowThinking(true);
      setToolStatus(null);
      setReplyStarted(false);
      setInsightEvents([]);
      citationsRef.current = [];
      guardrailRef.current = null;
      // 重新生成 / 编辑重发会走到同一个 userMessageId：后端那边会先把这一回合的
      // 旧轨迹删掉再重新记，前端不清就会把新旧两份接在一起，看起来像它查了两倍的东西。
      setToolStepsByMessage((prev) => {
        if (!prev[userMessageId]) return prev;
        const next = { ...prev };
        delete next[userMessageId];
        return next;
      });

      const controller = new AbortController();
      abortRef.current = controller;

      let assistantMsgId = "";

      try {
        for await (const chunk of apiClient.streamMessage(
          {
            prompt: userMsg,
            model: selectedModel,
            chat_id: sessionId,
            use_rag: useRag,
            message_id: userMessageId,
            // 提示词实验台里挂上的版本；没挂就交给服务端默认版本
            ...(promptVersion ? { prompt_version: promptVersion } : {}),
          },
          controller.signal,
        )) {
          if (chunk.error) {
            stopFlushTimer();
            setShowThinking(false);
            setToolStatus(null);
            if (assistantMsgId) {
              dispatch(
                removeMessage({
                  sessionId,
                  messageId: assistantMsgId,
                }),
              );
            }
            bufferRef.current = { id: "", content: "", sessionId: "" };
            dispatch(setIsGenerating(false));
            if (isNewSessionRef.current) {
              isNewSessionRef.current = false;
              const title =
                userMsg.length > 10 ? userMsg.slice(0, 10) + "..." : userMsg;
              dispatch(renameChat({ id: sessionId, title }));
              apiClient.renameChat(sessionId, title).catch(() => {});
            }
            dispatch(
              addMessage({
                id: Date.now().toString(),
                sessionId,
                role: "assistant",
                content: `错误: ${chunk.error}`,
                timestamp: ts,
              }),
            );
            break;
          }

          if (chunk.done) {
            stopFlushTimer();
            flushBuffer();
            if (chunk.message_id && assistantMsgId) {
              dispatch(
                setMessageServerId({
                  sessionId,
                  localId: assistantMsgId,
                  serverId: chunk.message_id,
                }),
              );
            }
            bufferRef.current = { id: "", content: "", sessionId: "" };
            dispatch(setIsGenerating(false));
            setToolStatus(null);
            if (assistantMsgId && citationsRef.current.length) {
              dispatch(
                setMessageCitations({
                  sessionId,
                  messageId: assistantMsgId,
                  citations: dedupeCitations(citationsRef.current),
                }),
              );
            }
            // 流里只有工具的状态，没有结果正文。这里补一次接口，把摘要与原文长度
            // 取回来——按消息键合并而不是整批替换：后端 TOOL_HISTORY_ENABLED 关掉时
            // 接口返回空，整批替换会把刚刚实时拼出来的那份也擦掉。
            apiClient
              .getToolSteps(sessionId)
              .then((steps) => {
                const grouped: Record<string, ToolStep[]> = {};
                for (const step of steps) {
                  if (!step.messageId) continue;
                  (grouped[step.messageId] ??= []).push(step);
                }
                setToolStepsByMessage((prev) => ({ ...prev, ...grouped }));
              })
              .catch(() => {
                // 补不到就保留实时那份，它已经能看出调了哪些工具
              });
            assistantMsgId = "";
            break;
          }

          if (chunk.type === "citations") {
            citationsRef.current = [
              ...citationsRef.current,
              ...(chunk.items ?? []),
            ];
            if (assistantMsgId) {
              dispatch(
                setMessageCitations({
                  sessionId,
                  messageId: assistantMsgId,
                  citations: dedupeCitations(citationsRef.current),
                }),
              );
            }
            continue;
          }

          if (chunk.type === "cache_hit") {
            const pct = Math.round((chunk.similarity ?? 1) * 100);
            setToolStatus(
              chunk.exact
                ? "命中缓存（同一个问题），未调用模型"
                : `命中语义缓存（相似度 ${pct}%），未调用模型`,
            );
            setInsightEvents((prev) => [
              ...prev,
              {
                type: "cache_hit",
                label: "缓存命中",
                detail: chunk.exact
                  ? "精确匹配"
                  : `相似度 ${pct}% · 省 ${chunk.tokensSaved ?? 0} token`,
              },
            ]);
            continue;
          }

          if (chunk.type === "guardrail") {
            const notice: GuardrailNotice = {
              findings: chunk.findings ?? [],
              score: chunk.score ?? 0,
              masked: chunk.masked ?? 0,
              blocked: chunk.blocked ?? false,
            };
            guardrailRef.current = notice;
            if (assistantMsgId) {
              dispatch(
                setMessageGuardrail({
                  sessionId,
                  messageId: assistantMsgId,
                  notice,
                }),
              );
            }
            setInsightEvents((prev) => [
              ...prev,
              {
                type: "guardrail",
                label: notice.blocked ? "护栏拦截" : "护栏命中",
                detail: guardrailSummary(notice),
              },
            ]);
            continue;
          }

          if (chunk.type === "context_compacted") {
            setToolStatus(
              `早期对话已压缩为摘要（${chunk.summarized ?? 0} 条），正在继续...`,
            );
            setInsightEvents((prev) => [
              ...prev,
              {
                type: "context_compacted",
                label: "上下文压缩",
                detail: `${chunk.summarized ?? 0}条摘要`,
              },
            ]);
            continue;
          }

          if (chunk.type === "tool_start") {
            const prefix =
              chunk.round && chunk.round > 1 ? `第 ${chunk.round} 轮 · ` : "";
            setToolStatus(`${prefix}${toolLabel(chunk.tool)}...`);
            setInsightEvents((prev) => [
              ...prev,
              {
                type: "tool_start",
                label: toolLabel(chunk.tool),
                detail: chunk.round ? `第${chunk.round}轮` : undefined,
              },
            ]);
            // 实时轨迹：先记下这一步，状态等 tool_result 回来再补。
            // 不等流跑完再整批拉一次接口，是因为这条时间线的用处一半在"正在发生
            // 什么"，等结束了才显示就只剩事后复盘。
            setToolStepsByMessage((prev) => {
              const round = chunk.round ?? 0;
              const existing = prev[userMessageId] ?? [];
              return {
                ...prev,
                [userMessageId]: [
                  ...existing,
                  {
                    round,
                    callIndex: existing.filter((step) => step.round === round)
                      .length,
                    tool: chunk.tool ?? "",
                    input: chunk.input,
                  },
                ],
              };
            });
            continue;
          }

          if (chunk.type === "tool_result") {
            const label = toolLabel(chunk.tool);
            setToolStatus(
              chunk.status && chunk.status !== "ok"
                ? `${label}未成功，正在调整策略...`
                : `${label}完成，正在思考下一步...`,
            );
            setToolStepsByMessage((prev) => {
              const steps = prev[userMessageId];
              if (!steps?.length) return prev;
              // 从后往前找同轮同名、还没有状态的那一步：同一轮里同一个工具可能被
              // 调用多次（不同参数），只能按"最近一个待完成的"匹配。
              const round = chunk.round ?? 0;
              let target = -1;
              for (let i = steps.length - 1; i >= 0; i -= 1) {
                if (
                  steps[i].tool === chunk.tool &&
                  steps[i].round === round &&
                  !steps[i].status
                ) {
                  target = i;
                  break;
                }
              }
              if (target < 0) return prev;
              const next = [...steps];
              next[target] = { ...next[target], status: chunk.status ?? "ok" };
              return { ...prev, [userMessageId]: next };
            });
            continue;
          }

          if (chunk.type === "tool_rounds_ended") {
            setToolStatus("工具调用已结束，正在整理最终回答...");
            continue;
          }

          if (chunk.content) {
            // 模型重新开始输出正文，说明这一轮工具阶段结束了。
            setToolStatus(null);
            if (assistantMsgId) {
              bufferRef.current.content += chunk.content;
            } else {
              setShowThinking(false);
              setReplyStarted(true);
              // 新会话的首条 AI 回复到达时,用用户消息作为标题
              if (isNewSessionRef.current) {
                isNewSessionRef.current = false;
                const title =
                  userMsg.length > 10 ? userMsg.slice(0, 10) + "..." : userMsg;
                dispatch(renameChat({ id: sessionId, title }));
                apiClient.renameChat(sessionId, title).catch(() => {});
              }
              assistantMsgId = Date.now().toString();
              bufferRef.current = {
                id: assistantMsgId,
                content: chunk.content,
                sessionId,
              };
              dispatch(
                addMessage({
                  id: assistantMsgId,
                  sessionId,
                  role: "assistant",
                  content: chunk.content,
                  timestamp: ts,
                  model: selectedModel,
                  citations: citationsRef.current.length
                    ? dedupeCitations(citationsRef.current)
                    : undefined,
                  guardrail: guardrailRef.current ?? undefined,
                }),
              );
            }
            startFlushTimer();
          }
        }
      } catch (err) {
        if ((err as Error)?.name === "AbortError") return;
        stopFlushTimer();
        setShowThinking(false);
        setToolStatus(null);
        if (assistantMsgId) {
          dispatch(
            removeMessage({
              sessionId,
              messageId: assistantMsgId,
            }),
          );
        }
        bufferRef.current = { id: "", content: "", sessionId: "" };
        if (isNewSessionRef.current) {
          isNewSessionRef.current = false;
          const title =
            userMsg.length > 10 ? userMsg.slice(0, 10) + "..." : userMsg;
          dispatch(renameChat({ id: sessionId, title }));
          apiClient.renameChat(sessionId, title).catch(() => {});
        }
        dispatch(
          addMessage({
            id: crypto.randomUUID(),
            sessionId,
            role: "assistant",
            content: "连接中断，未自动重复提交。请点击重新生成继续。",
            timestamp: ts,
          }),
        );
        dispatch(setIsGenerating(false));
      } finally {
        abortRef.current = null;
      }
    },
    [
      dispatch,
      selectedModel,
      useRag,
      flushBuffer,
      stopFlushTimer,
      startFlushTimer,
    ],
  );

  const handleSend = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!input.trim() || isGenerating) return;

      let sessionId = currentChatId;
      if (!sessionId) {
        try {
          const chat = await apiClient.createChat({
            title: "新对话",
          });
          sessionId = chat.id;
          isNewSessionRef.current = true;
          dispatch(setCurrentChat(chat.id));
          dispatch(
            setSessions([
              {
                id: chat.id,
                title: chat.title,
                date: new Date().toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                }),
                pinned: false,
              },
              ...sessions,
            ]),
          );
        } catch {
          return;
        }
      }

      const userInput = input.trim();
      setInput("");
      setAttachments([]);
      if (textareaRef.current) {
        textareaRef.current.style.height = "20px";
      }

      // 拼接附件内容到 API prompt(用户消息气泡只显示纯文本)
      let apiPrompt = userInput;
      for (const att of attachments) {
        if (att.type === "text" && att.path && capabilities?.readAttachment) {
          // 只给路径，让模型自己决定要不要读。内联全文的老做法有两个问题：
          // 这份 token 每一轮都要重付一次，而且不管模型是不是真的需要它；
          // 另外前端为了控制体积会先截到 8000 字，后端按路径读能读到更完整的内容。
          apiPrompt += `\n\n[附件: ${att.name}](${att.path})\n`;
        } else if (att.type === "text" && att.content) {
          // read_attachment 没开（或上传失败）时必须回落到内联，否则模型拿到的
          // 是一个它读不了的路径，附件内容等于凭空消失。
          apiPrompt += `\n\n----- 附件: ${att.name} -----\n${att.content}\n----- 附件结束 -----\n`;
        } else if (att.type === "image" && att.url) {
          apiPrompt += `\n\n![${att.name}](${att.url})\n`;
        } else if (att.type === "pdf") {
          apiPrompt += `\n\n[知识库文档: ${att.name}，已索引 ${att.chunks} 个分块，请基于该文档回答]\n`;
        }
      }

      const ts = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      const userMessageId = crypto.randomUUID();
      dispatch(
        addMessage({
          id: userMessageId,
          sessionId,
          role: "user",
          content: userInput,
          timestamp: ts,
        }),
      );

      await runCompletion(sessionId, apiPrompt, userMessageId);
    },
    [
      dispatch,
      input,
      isGenerating,
      currentChatId,
      sessions,
      attachments,
      // 附件走路径还是内联全文由它决定，漏了这一项会一直用挂载时那份能力清单
      capabilities,
      runCompletion,
    ],
  );

  /** 复制单条消息到剪贴板 */
  const handleCopyMessage = useCallback(async (content: string) => {
    try {
      await navigator.clipboard.writeText(content);
    } catch {
      // 忽略
    }
  }, []);

  /** 重新生成：截断指定 assistant 消息，重新发上一条用户消息 */
  const handleRegenerate = useCallback(
    async (assistantMsgId: string) => {
      if (!currentChatId || isGenerating) return;
      const msgs = messages;
      const idx = msgs.findIndex((m) => m.id === assistantMsgId);
      if (idx < 0) return;
      // 找到该 assistant 之前最近一条 user 消息
      let userIdx = idx - 1;
      while (userIdx >= 0 && msgs[userIdx].role !== "user") userIdx--;
      if (userIdx < 0) return;
      const userMsg = msgs[userIdx].content;

      try {
        await apiClient.reviseMessage(currentChatId, msgs[userIdx].id);
      } catch {
        return;
      }

      // 截断到该 assistant 之前（保留 user 消息）
      dispatch(
        truncateMessagesAfter({
          sessionId: currentChatId,
          messageId: assistantMsgId,
        }),
      );
      await runCompletion(currentChatId, userMsg, msgs[userIdx].id);
    },
    [currentChatId, isGenerating, messages, dispatch, runCompletion],
  );

  /** 编辑用户消息并重发：截断该 user 消息开始的所有消息，用新内容重发 */
  const handleEditUserMessage = useCallback(
    async (userMsgId: string, newContent: string) => {
      if (!currentChatId || isGenerating) return;
      if (!newContent.trim()) return;
      try {
        await apiClient.reviseMessage(
          currentChatId,
          userMsgId,
          newContent.trim(),
        );
      } catch {
        return;
      }
      dispatch(
        truncateMessagesAfter({
          sessionId: currentChatId,
          messageId: userMsgId,
        }),
      );
      const ts = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      dispatch(
        addMessage({
          id: userMsgId,
          sessionId: currentChatId,
          role: "user",
          content: newContent.trim(),
          timestamp: ts,
        }),
      );
      await runCompletion(currentChatId, newContent.trim(), userMsgId);
    },
    [currentChatId, isGenerating, dispatch, runCompletion],
  );

  return (
    <div className="flex h-full bg-[#fbf9f5] dark:bg-[#141413] transition-colors duration-200">
      <div className="flex flex-col flex-1 min-w-0">
        <div
          ref={messagesContainerRef}
          className="flex-1 overflow-y-auto p-6 space-y-6"
        >
          {!currentChatId || messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-center max-w-lg mx-auto">
              <div className="w-14 h-14 rounded-2xl bg-[#da7756] text-white flex items-center justify-center shadow-lg shadow-[#da7756]/25 mb-5">
                <Bot className="w-7 h-7" />
              </div>
              <h2 className="text-xl font-semibold text-[#1f1e1d] dark:text-[#edece8] mb-2">
                下午好，我是你的 AI 智能助手
              </h2>
              <p className="text-sm text-[#6e6b63] dark:text-[#a19f96] max-w-md mb-8 leading-relaxed">
                随时与我探讨问题、编写代码或检索本地知识库，我将为你提供高质量的深度解答。
              </p>
              <div className="grid grid-cols-2 gap-3 w-full">
                <button
                  onClick={() => {
                    setInput("请帮我分析当前项目的代码架构并给出重构建议");
                    if (textareaRef.current) textareaRef.current.focus();
                  }}
                  className="p-3 rounded-xl bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#1e1d1b] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] text-xs text-[#1f1e1d] dark:text-[#edece8] text-left transition-all"
                >
                  <div className="font-semibold mb-0.5">💻 代码重构分析</div>
                  <div className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] truncate">
                    审查代码质量与性能瓶颈
                  </div>
                </button>
                <button
                  onClick={() => {
                    setInput("请帮我从知识库中检索并总结公司相关制度");
                    setUseRag(true);
                    if (textareaRef.current) textareaRef.current.focus();
                  }}
                  className={`p-3 rounded-xl border text-left transition-all ${
                    useRag
                      ? "bg-[#da7756]/10 border-[#da7756]/40 text-[#1f1e1d] dark:text-[#edece8]"
                      : "bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#1e1d1b] dark:hover:bg-[#262522] border-[#e3dfd5] dark:border-[#2e2d2a] text-[#1f1e1d] dark:text-[#edece8]"
                  }`}
                >
                  <div className="font-semibold mb-0.5">📚 知识库 RAG 检索</div>
                  <div className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] truncate">
                    基于本地文档的精准问答
                  </div>
                </button>
              </div>
            </div>
          ) : (
            messages.map((msg) => (
              <div
                key={msg.id}
                className={`group flex items-start gap-4 ${
                  msg.role === "user"
                    ? "ml-auto flex-row-reverse max-w-2xl"
                    : "max-w-xl"
                }`}
              >
                <div
                  className={`w-8 h-8 rounded-xl flex items-center justify-center shrink-0 shadow-sm ${
                    msg.role === "user"
                      ? "bg-[#da7756] text-white"
                      : "bg-[#282724] dark:bg-[#2e2d2a] text-[#edece8]"
                  }`}
                >
                  {msg.role === "user" ? (
                    <User className="w-4 h-4" />
                  ) : (
                    <Bot className="w-4 h-4 text-[#da7756]" />
                  )}
                </div>
                <div
                  className={`flex flex-col gap-1 ${msg.role === "user" ? "w-fit" : "min-w-0 flex-1"}`}
                >
                  <div
                    className={`p-4 rounded-2xl text-sm leading-relaxed shadow-sm ${
                      msg.role === "user"
                        ? "bg-[#da7756] text-white rounded-tr-xs max-w-none"
                        : "bg-white dark:bg-[#1e1d1b] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#1f1e1d] dark:text-[#edece8] rounded-tl-xs"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-4 mb-1 text-[11px] opacity-75">
                      <span className="font-semibold">
                        {msg.role === "user" ? "我" : msg.model || "AI 助手"}
                      </span>
                      <span>{msg.timestamp}</span>
                    </div>

                    {editingMsgId === msg.id ? (
                      <div className="space-y-2">
                        <textarea
                          value={editingText}
                          onChange={(e) => setEditingText(e.target.value)}
                          rows={3}
                          autoFocus
                          className="w-full bg-white/10 border border-white/20 rounded-lg px-2.5 py-2 text-sm text-current resize-y focus:outline-none"
                        />
                        <div className="flex gap-2 justify-end">
                          <button
                            onClick={() => setEditingMsgId(null)}
                            className="px-2.5 py-1 text-[11px] rounded-lg bg-white/10 hover:bg-white/20"
                          >
                            取消
                          </button>
                          <button
                            onClick={() => {
                              handleEditUserMessage(msg.id, editingText);
                              setEditingMsgId(null);
                            }}
                            className="px-2.5 py-1 text-[11px] rounded-lg bg-white/20 hover:bg-white/30 flex items-center gap-1"
                          >
                            <Check className="w-3 h-3" /> 发送
                          </button>
                        </div>
                      </div>
                    ) : (
                      <MessageContent content={msg.content} />
                    )}
                  </div>

                  {/* 这一回合调了哪些工具。折叠的，展开才占地方 */}
                  {msg.role === "assistant" &&
                    !!traceByAssistant[msg.id]?.length && (
                      <ToolTrace
                        steps={traceByAssistant[msg.id]}
                        historyDisabled={capabilities?.toolHistory === false}
                      />
                    )}

                  {/* 检索资料里发现可疑指令：告诉用户护栏做了什么 */}
                  {msg.role === "assistant" && !!msg.guardrail && (
                    <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2">
                      <div className="flex items-center gap-1.5 text-[11px] font-semibold text-amber-700 dark:text-amber-400">
                        <ShieldAlert className="w-3 h-3" />
                        {msg.guardrail.blocked
                          ? "已拦截可疑资料"
                          : "检索到的资料含可疑指令，已中和"}
                      </div>
                      <div className="mt-1 text-[11px] text-amber-700/80 dark:text-amber-400/80">
                        {guardrailSummary(msg.guardrail)}
                        <span className="opacity-60">
                          （风险分 {msg.guardrail.score}
                          {msg.guardrail.masked > 0
                            ? ` · 改写 ${msg.guardrail.masked} 处标记`
                            : ""}
                          ）
                        </span>
                      </div>
                    </div>
                  )}

                  {/* RAG 引用来源 */}
                  {msg.role === "assistant" && !!msg.citations?.length && (
                    <div className="rounded-xl border border-[#e3dfd5] dark:border-[#2e2d2a] bg-[#faf9f5] dark:bg-[#191817] px-3 py-2">
                      <div className="flex items-center gap-1.5 text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                        <BookOpen className="w-3 h-3" />
                        参考来源 ({msg.citations.length})
                      </div>
                      <ul className="space-y-1">
                        {msg.citations.map((citation, index) => (
                          <li
                            key={`${citation.document_id}-${index}`}
                            title={citation.content?.slice(0, 400)}
                            className="flex items-center gap-2 text-[11px] text-[#6e6b63] dark:text-[#a19f96]"
                          >
                            <span className="shrink-0 w-4 h-4 rounded bg-[#e3dfd5] dark:bg-[#2e2d2a] text-[10px] flex items-center justify-center">
                              {index + 1}
                            </span>
                            <span className="truncate text-[#1f1e1d] dark:text-[#edece8]">
                              {citation.document_name}
                            </span>
                            <span className="shrink-0">
                              {citationSpan(citation)}
                            </span>
                            {typeof citation.score === "number" && (
                              <span className="shrink-0 opacity-70">
                                {citation.score.toFixed(2)}
                              </span>
                            )}
                            {!!citation.channels?.length && (
                              <span className="shrink-0 opacity-70">
                                {citation.channels.join("+")}
                              </span>
                            )}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* hover 工具栏 */}
                  {editingMsgId !== msg.id && !isGenerating && (
                    <div
                      className={`flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity text-[#918d83] ${
                        msg.role === "user" ? "justify-end" : ""
                      }`}
                    >
                      <button
                        onClick={() => handleCopyMessage(msg.content)}
                        title="复制"
                        className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                      >
                        <Copy className="w-3.5 h-3.5" />
                      </button>
                      {msg.role === "user" && (
                        <button
                          onClick={() => {
                            setEditingMsgId(msg.id);
                            setEditingText(msg.content);
                          }}
                          title="编辑并重发"
                          className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {msg.role === "assistant" && (
                        <button
                          onClick={() => handleRegenerate(msg.id)}
                          title="重新生成"
                          className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                        >
                          <RefreshCw className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {msg.role === "assistant" && msg.messageId && (
                        <button
                          onClick={() =>
                            navigate(`/traces?message=${msg.messageId}`)
                          }
                          title="查看运行轨迹"
                          className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                        >
                          <RouteIcon className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {msg.role === "assistant" && (
                        <FeedbackButtons
                          messageId={msg.messageId ?? msg.id}
                          initial={feedbackByMessage[msg.messageId ?? msg.id]}
                        />
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}

          {isGenerating && !replyStarted && (showThinking || toolStatus) && (
            <div className="flex items-start gap-4 max-w-3xl">
              <div className="w-8 h-8 rounded-xl bg-[#282724] dark:bg-[#2e2d2a] flex items-center justify-center text-[#da7756] shadow-sm animate-pulse">
                <Bot className="w-4 h-4" />
              </div>
              <div className="flex items-center gap-3">
                <div className="p-3 rounded-2xl bg-white dark:bg-[#1e1d1b] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#6e6b63] dark:text-[#a19f96] text-sm flex items-center gap-2 shadow-sm">
                  <Sparkles className="w-4 h-4 animate-spin text-[#da7756]" />
                  <span>{toolStatus || "正在深度思考与规划..."}</span>
                </div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {isGenerating && (
          <div className="flex justify-center py-1">
            <button
              onClick={handleStop}
              className="px-4 py-1.5 rounded-xl bg-rose-500/10 hover:bg-rose-500/20 text-rose-600 dark:text-rose-400 text-xs font-medium flex items-center gap-1.5 transition-colors border border-rose-500/20 shadow-sm"
            >
              <Square className="w-3 h-3" />
              停止生成
            </button>
          </div>
        )}

        <div className="p-4 border-t border-[#e6e2d8] dark:border-[#282724] bg-[#fbf9f5]/60 dark:bg-[#141413]/60 backdrop-blur-md">
          {attachError && (
            <div className="max-w-4xl mx-auto mb-2 flex items-center justify-between gap-3 p-2.5 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
              <span>{attachError}</span>
              <button
                type="button"
                onClick={() => setAttachError(null)}
                className="p-0.5 hover:bg-rose-500/10 rounded"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
          {attachments.length > 0 && (
            <div className="max-w-4xl mx-auto mb-2 flex flex-wrap gap-2">
              {attachments.map((att) => (
                <div
                  key={att.id}
                  className="flex items-center gap-2 pl-2.5 pr-1.5 py-1.5 rounded-lg bg-[#f3f0e6] dark:bg-[#1e1d1b] border border-[#e3dfd5] dark:border-[#2e2d2a] text-xs text-[#1f1e1d] dark:text-[#edece8]"
                >
                  {att.type === "image" ? (
                    <ImageIcon className="w-3.5 h-3.5 text-[#da7756] shrink-0" />
                  ) : att.type === "pdf" ? (
                    <FileText className="w-3.5 h-3.5 text-[#da7756] shrink-0" />
                  ) : (
                    <Paperclip className="w-3.5 h-3.5 text-[#da7756] shrink-0" />
                  )}
                  <span className="max-w-[160px] truncate font-medium">
                    {att.name}
                  </span>
                  {att.chunks !== undefined && (
                    <span className="text-[10px] text-[#6e6b63] dark:text-[#a19f96]">
                      {att.chunks} 块
                    </span>
                  )}
                  {att.type === "text" && att.content && (
                    <span className="text-[10px] text-[#6e6b63] dark:text-[#a19f96]">
                      {att.content.length > 50
                        ? `${Math.round((att.content.length / 1024) * 10) / 10}KB`
                        : `${att.content.length}B`}
                    </span>
                  )}
                  {/* 这份附件到底怎么进提示词，是会影响回答质量的事，不该是隐形的 */}
                  {att.type === "text" && (
                    <span
                      className="text-[10px] px-1 rounded bg-[#e3dfd5] dark:bg-[#2e2d2a] text-[#6e6b63] dark:text-[#a19f96]"
                      title={
                        att.path && capabilities?.readAttachment
                          ? "只把路径给模型，由它调用 read_attachment 按需读取"
                          : "把全文拼进这一轮提示词（后端未开启 read_attachment，或上传失败）"
                      }
                    >
                      {att.path && capabilities?.readAttachment
                        ? "按需读取"
                        : "全文内联"}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => removeAttachment(att.id)}
                    className="p-0.5 hover:bg-[#e3dfd5] dark:hover:bg-[#2e2d2a] rounded text-[#6e6b63] dark:text-[#a19f96] hover:text-rose-500 transition-colors"
                    title="移除附件"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <form onSubmit={handleSend} className="max-w-4xl mx-auto relative">
            <div className="flex items-center rounded-2xl bg-white dark:bg-[#1e1d1b] border border-[#e3dfd5] dark:border-[#2e2d2a] focus-within:border-[#da7756] transition-all shadow-md p-2.5 gap-2">
              <button
                type="button"
                onClick={() => setUseRag((v) => !v)}
                className={`p-2 rounded-xl transition-colors flex items-center gap-1.5 text-xs font-medium ${
                  useRag
                    ? "bg-[#da7756]/10 text-[#da7756] border border-[#da7756]/30"
                    : "text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] border border-transparent"
                }`}
                title={
                  useRag ? "已开启知识库检索（点击关闭）" : "开启知识库检索"
                }
              >
                <BookOpen className="w-4 h-4" />
                {useRag && <span className="hidden sm:inline">RAG</span>}
              </button>
              <button
                type="button"
                onClick={handleAttachClick}
                disabled={attaching}
                className="p-2 text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] rounded-xl transition-colors disabled:opacity-50"
                title="附加文本文件到消息"
              >
                <Paperclip className="w-4 h-4" />
              </button>
              <input
                ref={attachInputRef}
                type="file"
                className="hidden"
                accept=".txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.xml,.yaml,.yml,.css,.html,.csv,.log,.sh,.java,.go,.rs,.c,.cpp,.png,.jpg,.jpeg,.gif,.webp,.svg,.pdf"
                onChange={handleAttachFile}
              />
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => {
                  setInput(e.target.value);
                  adjustTextareaHeight();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSend(e);
                  }
                }}
                placeholder={"给 AI 助手发送消息..."}
                className="flex-1 bg-transparent border-none text-sm text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] dark:placeholder-[#78756d] focus:outline-none px-2 resize-none overflow-y-auto"
                style={{
                  height: "20px",
                  minHeight: "20px",
                  maxHeight: "192px",
                }}
              />
              <button
                type="submit"
                disabled={!input.trim() || isGenerating}
                className="p-2.5 rounded-xl bg-[#da7756] hover:bg-[#c86544] disabled:opacity-40 disabled:hover:bg-[#da7756] text-white transition-all shadow-md shadow-[#da7756]/25"
              >
                <Send className="w-4 h-4" />
              </button>
            </div>
            <div className="flex items-center justify-between px-2 pt-2 text-[11px] text-[#918d83] dark:text-[#78756d]">
              <span>
                Enter 发送, Shift+Enter 换行
                {useRag ? " · 📚 知识库检索已开启" : ""}
              </span>
              {promptVersion && (
                // 挂着实验版提示词却看不见，是最容易误读结果的状态：
                // 过几天回来看这段对话，会以为它代表默认配置的表现。
                <button
                  type="button"
                  onClick={() => dispatch(setPromptVersion(null))}
                  className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-[#da7756]/10 text-[#da7756] hover:bg-[#da7756]/20 transition-colors"
                  title="点击恢复默认提示词版本"
                >
                  <FlaskConical className="w-3 h-3" />
                  提示词 {promptVersion}
                  <X className="w-3 h-3" />
                </button>
              )}
            </div>
          </form>
        </div>
      </div>
      {insightOpen && currentChatId && (
        <ChatInsightPanel
          sessionId={currentChatId}
          events={insightEvents}
          onClose={() => setInsightOpen(false)}
        />
      )}
      {!insightOpen && currentChatId && (
        <button
          onClick={() => setInsightOpen(true)}
          className="w-8 border-l border-[#e6e2d8] dark:border-[#282724] flex items-center justify-center text-[#918d83] hover:text-[#da7756] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] transition-colors shrink-0"
          title="展开运行洞察"
        >
          <svg
            className="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
      )}
    </div>
  );
};
