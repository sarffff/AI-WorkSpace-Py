import { contextBridge, ipcRenderer } from 'electron';

/**
 * 暴露给渲染进程的桌面能力。
 *
 * 逐个方法转发，**不暴露 ipcRenderer 本身**：暴露了它就等于让渲染进程能调任意
 * 通道，contextIsolation 也就白开了。
 *
 * `pickFolder` 只把用户选的路径拿回来，登记授权走 HTTP（POST /fs/roots）——
 * 那件事要落到当前登录用户的记录上，而主进程手里没有他的 JWT。
 */
contextBridge.exposeInMainWorld('electronAPI', {
  getVersion: () => ipcRenderer.invoke('app:get-version'),
  ping: () => ipcRenderer.invoke('app:ping'),
  /** 打开系统文件夹选择对话框。用户取消时返回 null。 */
  pickFolder: (): Promise<string | null> => ipcRenderer.invoke('fs:pick-folder'),
});
