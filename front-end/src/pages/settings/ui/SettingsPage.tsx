import React from 'react';
import { Settings as SettingsIcon, Cpu, Key, Database, Bell } from 'lucide-react';

export const SettingsPage: React.FC = () => {
  return (
    <div className="p-8 h-full overflow-y-auto space-y-6 max-w-4xl">
      <div>
        <h3 className="text-lg font-semibold text-slate-100">Application Settings</h3>
        <p className="text-xs text-slate-400 mt-1">Configure AI providers, database connections, and desktop preferences.</p>
      </div>

      <div className="space-y-4">
        {/* API Settings */}
        <div className="p-5 rounded-xl bg-slate-900/60 border border-slate-800 space-y-4">
          <div className="flex items-center gap-3">
            <Key className="w-5 h-5 text-indigo-400" />
            <div>
              <h4 className="text-sm font-medium text-slate-200">OpenAI & AI Providers</h4>
              <span className="text-xs text-slate-500">Manage API keys and base endpoints</span>
            </div>
          </div>
          <div className="space-y-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">OpenAI API Key</label>
              <input
                type="password"
                value="sk-proj-****************************************"
                readOnly
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-300 font-mono"
              />
            </div>
          </div>
        </div>

        {/* Database Settings */}
        <div className="p-5 rounded-xl bg-slate-900/60 border border-slate-800 space-y-4">
          <div className="flex items-center gap-3">
            <Database className="w-5 h-5 text-emerald-400" />
            <div>
              <h4 className="text-sm font-medium text-slate-200">Database & Redis Connection</h4>
              <span className="text-xs text-slate-500">MySQL connection string and Redis cache settings</span>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-slate-400 mb-1">MySQL Connection URL</label>
              <input
                type="text"
                value="mysql://root:root@localhost:3306/ai_workspace"
                readOnly
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-300 font-mono"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">Redis Host</label>
              <input
                type="text"
                value="redis://localhost:6379"
                readOnly
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-300 font-mono"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
