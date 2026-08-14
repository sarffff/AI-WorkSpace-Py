import { createSlice, PayloadAction } from "@reduxjs/toolkit";
import type {
    UIMessage,
    ChatSession,
    NavTab,
    Citation,
    GuardrailNotice,
} from "@/shared/types/api.types";

interface ChatState {
    activeTab: NavTab;
    selectedModel: string;
    isGenerating: boolean;
    serverStatus: "checking" | "online" | "offline";
    currentChatId: string | null;
    sessions: ChatSession[];
    messagesBySession: Record<string, UIMessage[]>;
    pendingInput: string | null;
    /**
     * 提示词实验台里选中的系统提示词版本。null = 用服务端默认版本。
     * 存在这里而不是每次从后端读:它是"这次实验用哪版"的本地意图,
     * 不是服务端状态——后端那边的默认版本并不会因为你在界面上试了一版而改变。
     */
    promptVersion: string | null;
}

// 刷新页面不该悄悄把实验组切回对照组,所以选择要落盘
const PROMPT_VERSION_KEY = "prompt_version";

const readStoredPromptVersion = (): string | null => {
    try {
        return localStorage.getItem(PROMPT_VERSION_KEY);
    } catch {
        return null;
    }
};

const initialState: ChatState = {
    activeTab: "chat",
    selectedModel: "glm-4.5-air",
    isGenerating: false,
    serverStatus: "checking",
    currentChatId: null,
    sessions: [],
    messagesBySession: {},
    pendingInput: null,
    promptVersion: readStoredPromptVersion(),
};

let _nextId = 1;
const genId = () => `chat_${Date.now()}_${_nextId++}`;

const now = () =>
    new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export const chatSlice = createSlice({
    name: "chat",
    initialState,
    reducers: {
        setActiveTab: (state, action: PayloadAction<NavTab>) => {
            state.activeTab = action.payload;
        },
        setSelectedModel: (state, action: PayloadAction<string>) => {
            state.selectedModel = action.payload;
        },
        setIsGenerating: (state, action: PayloadAction<boolean>) => {
            state.isGenerating = action.payload;
        },
        setServerStatus: (
            state,
            action: PayloadAction<"checking" | "online" | "offline">,
        ) => {
            state.serverStatus = action.payload;
        },

        // ===== 会话管理 =====

        createChat: {
            reducer: (state, action: PayloadAction<string | undefined>) => {
                const id = action.payload ?? genId();
                const session: ChatSession = {
                    id,
                    title: "新对话",
                    date: now(),
                    pinned: false,
                };
                state.sessions.unshift(session);
                state.currentChatId = id;
                state.messagesBySession[id] = [];
                state.activeTab = "chat";
            },
            prepare: (id?: string) => ({ payload: id }),
        },

        setCurrentChat: (state, action: PayloadAction<string | null>) => {
            state.currentChatId = action.payload;
            if (action.payload) state.activeTab = "chat";
        },

        renameChat: (
            state,
            action: PayloadAction<{ id: string; title: string }>,
        ) => {
            const s = state.sessions.find((s) => s.id === action.payload.id);
            if (s) s.title = action.payload.title;
        },

        deleteChat: (state, action: PayloadAction<string>) => {
            const id = action.payload;
            state.sessions = state.sessions.filter((s) => s.id !== id);
            delete state.messagesBySession[id];
            if (state.currentChatId === id) {
                state.currentChatId = state.sessions[0]?.id ?? null;
            }
        },

        togglePinChat: (state, action: PayloadAction<string>) => {
            const s = state.sessions.find((s) => s.id === action.payload);
            if (s) s.pinned = !s.pinned;
        },

        setSessions: (state, action: PayloadAction<ChatSession[]>) => {
            state.sessions = action.payload;
        },

        // ===== 消息管理 =====

        addMessage: (state, action: PayloadAction<UIMessage>) => {
            const sid = action.payload.sessionId;
            if (!state.messagesBySession[sid]) {
                state.messagesBySession[sid] = [];
            }
            state.messagesBySession[sid].push(action.payload);
        },

        appendToMessage: (
            state,
            action: PayloadAction<{
                id: string;
                sessionId: string;
                content: string;
            }>,
        ) => {
            const msgs = state.messagesBySession[action.payload.sessionId];
            if (!msgs) return;
            const msg = msgs.find((m) => m.id === action.payload.id);
            if (msg) msg.content += action.payload.content;
        },

        updateMessageContent: (
            state,
            action: PayloadAction<{
                id: string;
                sessionId: string;
                content: string;
            }>,
        ) => {
            const msgs = state.messagesBySession[action.payload.sessionId];
            if (!msgs) return;
            const msg = msgs.find((m) => m.id === action.payload.id);
            if (msg) msg.content = action.payload.content;
        },

        removeMessage: (
            state,
            action: PayloadAction<{ sessionId: string; messageId: string }>,
        ) => {
            const msgs = state.messagesBySession[action.payload.sessionId];
            if (!msgs) return;
            state.messagesBySession[action.payload.sessionId] = msgs.filter(
                (message) => message.id !== action.payload.messageId,
            );
        },

        /** 覆盖某条消息的 RAG 引用来源（流式期间会多次到达） */
        setMessageCitations: (
            state,
            action: PayloadAction<{
                sessionId: string;
                messageId: string;
                citations: Citation[];
            }>,
        ) => {
            const msgs = state.messagesBySession[action.payload.sessionId];
            if (!msgs) return;
            const msg = msgs.find((m) => m.id === action.payload.messageId);
            if (msg) msg.citations = action.payload.citations;
        },

        /** 覆盖某条消息的护栏提示（检索资料里发现可疑指令时） */
        setMessageGuardrail: (
            state,
            action: PayloadAction<{
                sessionId: string;
                messageId: string;
                notice: GuardrailNotice;
            }>,
        ) => {
            const msgs = state.messagesBySession[action.payload.sessionId];
            if (!msgs) return;
            const msg = msgs.find((m) => m.id === action.payload.messageId);
            if (msg) msg.guardrail = action.payload.notice;
        },

        /** 流式 done 后回写服务端消息 id,用于关联运行轨迹 */
        setMessageServerId: (
            state,
            action: PayloadAction<{
                sessionId: string;
                localId: string;
                serverId: string;
            }>,
        ) => {
            const msgs = state.messagesBySession[action.payload.sessionId];
            if (!msgs) return;
            const msg = msgs.find((m) => m.id === action.payload.localId);
            if (msg) msg.messageId = action.payload.serverId;
        },

        setMessages: (
            state,
            action: PayloadAction<{ sessionId: string; messages: UIMessage[] }>,
        ) => {
            state.messagesBySession[action.payload.sessionId] =
                action.payload.messages;
        },

        /** 从某个消息开始（含）往后截断，用于重新生成 / 编辑后重发 */
        truncateMessagesAfter: (
            state,
            action: PayloadAction<{ sessionId: string; messageId: string }>,
        ) => {
            const msgs = state.messagesBySession[action.payload.sessionId];
            if (!msgs) return;
            const idx = msgs.findIndex((m) => m.id === action.payload.messageId);
            if (idx < 0) return;
            state.messagesBySession[action.payload.sessionId] = msgs.slice(0, idx);
        },

        setPendingInput: (state, action: PayloadAction<string | null>) => {
            state.pendingInput = action.payload;
        },

        /** 选中要试的系统提示词版本;null 表示回到服务端默认版本 */
        setPromptVersion: (state, action: PayloadAction<string | null>) => {
            state.promptVersion = action.payload;
            try {
                if (action.payload) {
                    localStorage.setItem(PROMPT_VERSION_KEY, action.payload);
                } else {
                    localStorage.removeItem(PROMPT_VERSION_KEY);
                }
            } catch {
                // 隐私模式下 localStorage 会抛异常，选择只在本次会话内有效
            }
        },
    },
});

export const {
    setActiveTab,
    setSelectedModel,
    setIsGenerating,
    setServerStatus,
    createChat,
    setCurrentChat,
    renameChat,
    deleteChat,
    togglePinChat,
    setSessions,
    addMessage,
    appendToMessage,
    updateMessageContent,
    removeMessage,
    setMessages,
    truncateMessagesAfter,
    setPendingInput,
    setMessageCitations,
    setMessageServerId,
    setMessageGuardrail,
    setPromptVersion,
} = chatSlice.actions;

export default chatSlice.reducer;
