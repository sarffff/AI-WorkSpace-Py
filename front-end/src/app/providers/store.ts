import { configureStore } from "@reduxjs/toolkit";
import chatReducer from "@/entities/chat/model/chatSlice";
import authReducer from "@/entities/auth/model/authSlice";

export const store = configureStore({
  reducer: {
    chat: chatReducer,
    auth: authReducer,
  },
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
