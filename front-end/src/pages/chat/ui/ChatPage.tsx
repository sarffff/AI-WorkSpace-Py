import React, { useState, useRef, useEffect, useCallback } from "react";
import { useSelector, useDispatch } from "react-redux";
import { RootState } from "@/app/providers/store";
import {
  addMessage,
  updateMessageContent,
  setIsGenerating,
  setCurrentChat,
  setMessages,
  setSessions,
  renameChat,
} from "@/entities/chat/model/chatSlice";
import { apiClient } from "@/shared/api/client";
import { Send, Bot, User, Sparkles, Paperclip, Square } from "lucide-react";

const FLUSH_INTERVAL = 60;

export const ChatPage: React.FC = () => {
  const dispatch = useDispatch();
  const {
    currentChatId,
    messagesBySession,
    sessions,
    selectedModel,
    isGenerating,
  } = useSelector((state: RootState) => state.chat);
  const messages = currentChatId
    ? (messagesBySession[currentChatId] ?? [])
    : [];
  const [input, setInput] = useState("");
  const [showThinking, setShowThinking] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const bufferRef = useRef({ id: "", content: "", sessionId: "" });
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const isNewSessionRef = useRef(false);

  const adjustTextareaHeight = () => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = "20px";
      const scrollHeight = textarea.scrollHeight;
      textarea.style.height = `${Math.min(scrollHeight, 192)}px`;
    }
  };

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
    flushBuffer();
    stopFlushTimer();
    dispatch(setIsGenerating(false));
  }, [dispatch, flushBuffer, stopFlushTimer]);

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
    if (isNearBottom()) scrollToBottom();
  });

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

      const userMsg = input.trim();
      setInput("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "20px";
      }

      const ts = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      dispatch(
        addMessage({
          id: Date.now().toString(),
          sessionId,
          role: "user",
          content: userMsg,
          timestamp: ts,
        }),
      );

      dispatch(setIsGenerating(true));
      setShowThinking(true);

      const controller = new AbortController();
      abortRef.current = controller;

      let assistantMsgId = "";

      try {
        for await (const chunk of apiClient.streamMessage(
          {
            prompt: userMsg,
            model: selectedModel,
            chat_id: sessionId,
          },
          controller.signal,
        )) {
          if (chunk.error) {
            setShowThinking(false);
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
            bufferRef.current = { id: "", content: "", sessionId: "" };
            dispatch(setIsGenerating(false));
            assistantMsgId = "";
            break;
          }

          if (chunk.content) {
            if (assistantMsgId) {
              bufferRef.current.content += chunk.content;
            } else {
              setShowThinking(false);
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
        if (isNewSessionRef.current) {
          isNewSessionRef.current = false;
          const title =
            userMsg.length > 10 ? userMsg.slice(0, 10) + "..." : userMsg;
          dispatch(renameChat({ id: sessionId, title }));
          apiClient.renameChat(sessionId, title).catch(() => {});
        }
        try {
          const res = await apiClient.sendMessage({
            prompt: userMsg,
            model: selectedModel,
            chat_id: sessionId,
          });
          dispatch(
            addMessage({
              id: Date.now().toString(),
              sessionId,
              role: "assistant",
              content: res.data,
              timestamp: ts,
              model: selectedModel,
            }),
          );
        } catch {
          dispatch(
            addMessage({
              id: Date.now().toString(),
              sessionId,
              role: "assistant",
              content: "无法连接到服务器,请确认后端已启动。",
              timestamp: ts,
            }),
          );
        }
        dispatch(setIsGenerating(false));
      } finally {
        abortRef.current = null;
      }
    },
    [
      dispatch,
      input,
      isGenerating,
      selectedModel,
      currentChatId,
      sessions,
      flushBuffer,
      stopFlushTimer,
      startFlushTimer,
    ],
  );

  return (
    <div className="flex flex-col h-full bg-[#090d16]">
      <div
        ref={messagesContainerRef}
        className="flex-1 overflow-y-auto p-6 space-y-6"
      >
        {!currentChatId || messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center">
            <div className="w-16 h-16 rounded-2xl bg-gradient-to-tr from-indigo-600 via-blue-500 to-cyan-400 flex items-center justify-center shadow-lg shadow-indigo-500/30 mb-5">
              <Bot className="w-8 h-8 text-white" />
            </div>
            <h2 className="text-lg font-semibold text-slate-100 mb-2">
              嗨,开始和我一起聊天吧!
            </h2>
            <p className="text-sm text-slate-400 max-w-md">
              你可以问我任何问题,我会尽力帮你解答。
            </p>
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex items-start gap-4 max-w-3xl ${
                msg.role === "user" ? "ml-auto flex-row-reverse" : ""
              }`}
            >
              <div
                className={`w-8 h-8 rounded-xl flex items-center justify-center shrink-0 shadow-md ${
                  msg.role === "user"
                    ? "bg-gradient-to-tr from-cyan-600 to-blue-600 text-white"
                    : "bg-gradient-to-tr from-indigo-600 to-violet-600 text-white"
                }`}
              >
                {msg.role === "user" ? (
                  <User className="w-4 h-4" />
                ) : (
                  <Bot className="w-4 h-4" />
                )}
              </div>
              <div
                className={`p-4 rounded-2xl text-sm leading-relaxed shadow-sm ${
                  msg.role === "user"
                    ? "bg-indigo-600 text-white rounded-tr-none"
                    : "bg-slate-900/90 border border-slate-800 text-slate-200 rounded-tl-none"
                }`}
              >
                <div className="flex items-center justify-between gap-4 mb-1 text-[11px] opacity-75">
                  <span className="font-semibold">
                    {msg.role === "user" ? "我" : msg.model || "AI 助手"}
                  </span>
                  <span>{msg.timestamp}</span>
                </div>
                <p className="whitespace-pre-wrap">{msg.content}</p>
              </div>
            </div>
          ))
        )}

        {isGenerating && showThinking && (
          <div className="flex items-start gap-4 max-w-3xl">
            <div className="w-8 h-8 rounded-xl bg-indigo-600 flex items-center justify-center text-white shadow-md animate-pulse">
              <Bot className="w-4 h-4" />
            </div>
            <div className="flex items-center gap-3">
              <div className="p-3 rounded-2xl bg-slate-900/90 border border-slate-800 text-slate-400 text-sm flex items-center gap-2">
                <Sparkles className="w-4 h-4 animate-spin text-indigo-400" />
                <span>正在思考中...</span>
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
            className="px-4 py-1 rounded-lg bg-red-600/20 hover:bg-red-600/40 text-red-400 text-xs flex items-center gap-1.5 transition-colors border border-red-600/20"
          >
            <Square className="w-3 h-3" />
            停止生成
          </button>
        </div>
      )}

      <div className="p-4 border-t border-slate-800/80 bg-slate-900/30">
        <form onSubmit={handleSend} className="max-w-4xl mx-auto relative">
          <div className="flex items-center rounded-xl bg-slate-900 border border-slate-800 focus-within:border-indigo-500/80 transition-all shadow-lg p-2 gap-2">
            <button
              type="button"
              className="p-2 text-slate-400 hover:text-slate-200 hover:bg-slate-800 rounded-lg transition-colors"
              title="上传文件"
            >
              <Paperclip className="w-4 h-4" />
            </button>
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
              placeholder={"嗨,有什么我可以帮助你的吗?"}
              className="flex-1 bg-transparent border-none text-sm text-slate-100 placeholder-slate-500 focus:outline-none px-2 resize-none overflow-y-auto"
              style={{
                height: "20px",
                minHeight: "20px",
                maxHeight: "192px",
              }}
            />
            <button
              type="submit"
              disabled={!input.trim() || isGenerating}
              className="p-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:hover:bg-indigo-600 text-white transition-all shadow-md shadow-indigo-600/20"
            >
              <Send className="w-4 h-4" />
            </button>
          </div>
          <div className="flex items-center justify-between px-2 pt-2 text-[11px] text-slate-500">
            <span>Enter 发送,Shift+Enter 换行</span>
          </div>
        </form>
      </div>
    </div>
  );
};
