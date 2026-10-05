import { contextBridge, ipcRenderer } from 'electron';

/**
 * 暴露给渲染进程的桌面能力。
 *
 * 逐个方法转发，**不暴露 ipcRenderer 本身**：暴露了它就等于让渲染进程能调任意
 * 通道，contextIsolation 也就白开了。
 *
 * 原来还有一个 `pickFolder`（弹系统对话框选一个授权给本机文件工具的文件夹）。
 * 那一层随对话工作台一起删了：工单的文件进出都走 HTTP（上传 / 下载），
 * 桌面端不需要碰本地文件系统，也就没有需要主进程代弹的对话框。
 */
contextBridge.exposeInMainWorld('electronAPI', {
  getVersion: () => ipcRenderer.invoke('app:get-version'),
  ping: () => ipcRenderer.invoke('app:ping'),
});
