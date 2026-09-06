import { app, BrowserWindow, dialog, ipcMain } from 'electron';
import path from 'path';

process.env.DIST = path.join(__dirname, '../dist');
process.env.VITE_PUBLIC = app.isPackaged
  ? process.env.DIST
  : path.join(process.env.DIST, '../public');

let win: BrowserWindow | null = null;

const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL'];

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#090d16',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  if (VITE_DEV_SERVER_URL) {
    win.loadURL(VITE_DEV_SERVER_URL);
  } else {
    win.loadFile(path.join(process.env.DIST as string, 'index.html'));
  }
}

/**
 * IPC handler 在这里注册，**不在 createWindow 里**。
 *
 * 原来它们在 createWindow 内部。macOS 上关掉窗口不退出进程，再点 dock 图标会走
 * `activate` 重新 createWindow——那时 `ipcMain.handle` 第二次注册同名通道，
 * Electron 直接抛 `Attempted to register a second handler for 'app:get-version'`。
 * 表现是重新打开窗口时白屏，而报错在主进程日志里，渲染进程什么都看不到。
 */
function registerIpc() {
  ipcMain.handle('app:get-version', () => app.getVersion());
  ipcMain.handle('app:ping', () => 'pong from Electron main process');

  /**
   * 让用户选一个文件夹，授权给 agent 的文件系统工具。
   *
   * 这个对话框必须在**主进程**里弹：渲染进程没有文件系统访问权
   * （`nodeIntegration: false` + `contextIsolation: true`），而后端在另一个进程里，
   * 它既弹不出对话框也不该有这个能力。
   *
   * 只返回路径，不做任何登记——登记走 HTTP（POST /fs/roots），因为授权要落到
   * 那个用户的数据库记录上，而主进程手里没有他的 JWT。
   *
   * 取消时返回 null 而不是空字符串：空字符串会被下游当成"选了一个空路径"，
   * 而那是一个要报错的输入；取消不是错误。
   */
  ipcMain.handle('fs:pick-folder', async () => {
    const result = await dialog.showOpenDialog({
      // createDirectory 让 macOS 用户能在对话框里新建一个空文件夹再授权它，
      // 而不必先切到 Finder
      properties: ['openDirectory', 'createDirectory'],
      title: '选择一个文件夹授权给 AI 助手',
      buttonLabel: '授权此文件夹',
    });
    if (result.canceled || result.filePaths.length === 0) return null;
    return result.filePaths[0];
  });
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
    win = null;
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

app.whenReady().then(() => {
  registerIpc();
  createWindow();
});
