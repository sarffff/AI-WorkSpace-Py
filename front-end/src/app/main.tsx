import React from 'react';
import ReactDOM from 'react-dom/client';
import { Provider } from 'react-redux';
import { store } from './providers/store';
import { ToastProvider } from '@/shared/ui/Toast';
import { applyStoredThemeBeforeMount } from '@/shared/lib/ThemeContext';
import App from './App';
import './styles/index.css';

// 必须在第一次绘制之前：否则每次启动都会先用浅色画一帧再翻成深色，
// 桌面端整窗闪一下白，比配色不好看更像一个坏掉的程序。
applyStoredThemeBeforeMount();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Provider store={store}>
      <ToastProvider>
        <App />
      </ToastProvider>
    </Provider>
  </React.StrictMode>
);
