export interface ElectronAPI {
  getVersion: () => Promise<string>;
  ping: () => Promise<string>;
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

/**
 * 桌面端能力的唯一入口。
 *
 * 浏览器里跑的时候整个 `electronAPI` 是 undefined，所以每个用到它的地方都必须
 * 能没有它地工作——开发时用 `vite` 直接开浏览器看界面就是靠这条。
 */
export const electronAPI = window.electronAPI;
