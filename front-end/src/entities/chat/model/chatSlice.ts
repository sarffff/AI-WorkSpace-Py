import { createSlice, PayloadAction } from "@reduxjs/toolkit";
import type { UIMessage, ChatSession, NavTab } from "@/shared/types/api.types";

interface ChatState {
    activeTab: NavTab;
    selectedModel: string;
    isGenerating: boolean;
    serverStatus: "checking" | "online" | "offline";
    currentChatId: string | null;
    sessions: ChatSession[];
    messagesBySession: Record<string, UIMessage[]>;
}

const initialState: ChatState = {
    activeTab: "chat",
    selectedModel: "glm-4.5-air",
    isGenerating: false,
    serverStatus: "checking",
    currentChatId: null,
    sessions: [],
    messagesBySession: {},
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

        setMessages: (
            state,
            action: PayloadAction<{ sessionId: string; messages: UIMessage[] }>,
        ) => {
            state.messagesBySession[action.payload.sessionId] =
                action.payload.messages;
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
    setMessages,
} = chatSlice.actions;

export default chatSlice.reducer;
