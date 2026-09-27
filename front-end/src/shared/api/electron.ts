export interface ElectronAPI {
  getVersion: () => Promise<string>;
  ping: () => Promise<string>;
  /**
   * 打开系统文件夹选择对话框，返回用户选的绝对路径；取消时返回 null。
   *
   * 只拿路径，不做登记——授权要落到当前登录用户的记录上（POST /fs/roots），
   * 而主进程手里没有他的 JWT。
   *
   * 浏览器里跑的时候整个 `electronAPI` 是 undefined，所以调用方必须处理"没有这个
   * 能力"的情形：那时只能让用户手打路径。
   */
  pickFolder: () => Promise<string | null>;
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

export const electronAPI = window.electronAPI;
