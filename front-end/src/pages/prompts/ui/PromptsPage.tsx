import React from 'react';
import { Sparkles, Terminal, Copy } from 'lucide-react';

export const PromptsPage: React.FC = () => {
  const prompts = [
    { title: 'Code Refactoring Expert', category: 'Engineering', desc: 'Review code for performance, readability, and TypeScript best practices.' },
    { title: 'Monorepo Architecture Planner', category: 'Architecture', desc: 'Design scalable pnpm workspaces with Turborepo and NestJS backends.' },
    { title: 'SQL & Prisma Query Optimizer', category: 'Database', desc: 'Optimize sluggish MySQL queries and design efficient Prisma relations.' },
  ];

  return (
    <div className="p-8 h-full overflow-y-auto space-y-6">
      <div>
        <h3 className="text-lg font-semibold text-slate-100">Prompt Engineering Hub</h3>
        <p className="text-xs text-slate-400 mt-1">Preset system prompts and specialized AI roles.</p>
      </div>

      <div className="grid grid-cols-3 gap-6">
        {prompts.map((p, idx) => (
          <div key={idx} className="p-5 rounded-xl bg-slate-900/60 border border-slate-800 space-y-3 flex flex-col justify-between hover:border-indigo-500/50 transition-all group">
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 font-medium">
                  {p.category}
                </span>
                <Sparkles className="w-4 h-4 text-indigo-400 opacity-60 group-hover:opacity-100 transition-opacity" />
              </div>
              <h4 className="text-sm font-semibold text-slate-100">{p.title}</h4>
              <p className="text-xs text-slate-400 leading-relaxed">{p.desc}</p>
            </div>
            <button className="w-full py-2 bg-slate-800 hover:bg-indigo-600 text-slate-300 hover:text-white text-xs font-medium rounded-lg transition-all flex items-center justify-center gap-2">
              <Terminal className="w-3.5 h-3.5" />
              Use Prompt
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};
