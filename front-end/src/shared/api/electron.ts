export interface ElectronAPI {
  getVersion: () => Promise<string>;
  ping: () => Promise<string>;
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

export const electronAPI = window.electronAPI;
