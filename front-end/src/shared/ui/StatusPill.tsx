import React from "react";
import type { StatusTone } from "@/shared/lib/format";

interface StatusPillProps {
  tone: StatusTone;
  children: React.ReactNode;
  /**
   * 呼吸点。只给"人在等这件事"的两档（等你批 / 已转人工）用。
   *
   * 一个持续动的元素在一个台面上只能有一层含义："现在有人该动手了"。
   * 把它用在别处（比如新工单）就等于稀释掉那两档的信号。
   */
  beacon?: boolean;
  title?: string;
}

export const StatusPill: React.FC<StatusPillProps> = ({
  tone,
  children,
  beacon,
  title,
}) => (
  <span className="status-pill" data-tone={tone} title={title}>
    {beacon && <i className="beacon" aria-hidden="true" />}
    {children}
  </span>
);
