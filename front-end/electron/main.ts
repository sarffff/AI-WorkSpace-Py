import { app, BrowserWindow, ipcMain, nativeTheme } from 'electron';
import path from 'path';

process.env.DIST = path.join(__dirname, '../dist');
process.env.VITE_PUBLIC = app.isPackaged
  ? process.env.DIST
  : path.join(process.env.DIST, '../public');

let win: BrowserWindow | null = null;

const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL'];

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 620,
    titleBarStyle: 'hiddenInset',
    /**
     * 窗口底色跟着系统深浅走。
     *
     * 原来这里写死 `#090d16`（一个已经不存在了的靛蓝深色）。窗口底色是 CSS 加载之前
     * 唯一能被看到的一层颜色，写死一种就意味着另一种主题下启动时会先闪一块错的底色。
     * 渲染进程的"跟随系统"以同一个 `nativeTheme` 为准，两边才不会出现两个答案。
     */
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#141413' : '#fbf9f5',
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
 *
 * 原来这里还有一个 `fs:pick-folder`（弹系统对话框选一个授权给文件工具的文件夹）。
 * 本机文件那一层随对话工作台一起删了，对话框也就没有了调用方。
 */
function registerIpc() {
  ipcMain.handle('app:get-version', () => app.getVersion());
  ipcMain.handle('app:ping', () => 'pong from Electron main process');
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
