import { useMemo } from "react";

const OPENCLAW_CHAT_BASE = "http://127.0.0.1:18789/chat";

function chatUrl(sessionKey: string): string {
  const url = new URL(OPENCLAW_CHAT_BASE);
  url.searchParams.set("session", sessionKey);
  return url.toString();
}

type PaneProps = {
  title: string;
  subtitle: string;
  badge: string;
  sessionKey: string;
  tone: "owner" | "door";
};

function ChatPane({ title, subtitle, badge, sessionKey, tone }: PaneProps) {
  const src = useMemo(() => chatUrl(sessionKey), [sessionKey]);
  const toneClass =
    tone === "owner"
      ? "border-sky-200/70 bg-sky-50/60 text-sky-700 dark:border-sky-900/60 dark:bg-sky-950/25 dark:text-sky-300"
      : "border-orange-200/80 bg-orange-50/70 text-orange-700 dark:border-orange-900/60 dark:bg-orange-950/25 dark:text-orange-300";
  return (
    <section className="min-h-0 flex flex-col rounded-2xl border border-border bg-bg-secondary shadow-sm overflow-hidden">
      <header className="shrink-0 px-4 py-3 border-b border-border flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-title text-text-primary truncate">{title}</span>
            <span className={`text-caption-mono px-2 py-0.5 rounded-full border ${toneClass}`}>
              {badge}
            </span>
          </div>
          <p className="text-caption text-text-tertiary mt-1 truncate">{subtitle}</p>
        </div>
        <a
          href={src}
          target="_blank"
          rel="noreferrer"
          className="shrink-0 text-caption px-2.5 py-1.5 rounded-md border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
        >
          单独打开
        </a>
      </header>
      <iframe
        title={title}
        src={src}
        className="flex-1 min-h-[520px] w-full bg-bg-primary"
        allow="clipboard-write; microphone; camera"
      />
    </section>
  );
}

export function DoormanConsolePage() {
  return (
    <div className="h-full min-h-0 flex flex-col gap-4">
      <section className="shrink-0 rounded-2xl border border-border bg-bg-secondary px-5 py-4 shadow-sm">
        <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
          <div>
            <h2 className="text-heading text-text-primary">门房中控</h2>
            <p className="text-body text-text-secondary mt-1">
              左边和主 Agent 交代门口安排，右边观察司阍与门外访客的对话。
            </p>
          </div>
          <div className="text-caption-mono text-text-tertiary bg-bg-tertiary rounded-lg px-3 py-2">
            主人 → 主 Agent → 门房任务 → 司阍 → 门外访客
          </div>
        </div>
      </section>

      <div className="min-h-0 flex-1 grid grid-cols-1 xl:grid-cols-2 gap-4">
        <ChatPane
          title="主 Agent"
          subtitle="给主人使用：在这里下达快递、外卖、访客等门口安排。"
          badge="agent:main:main"
          sessionKey="agent:main:main"
          tone="owner"
        />
        <ChatPane
          title="司阍"
          subtitle="门外访客入口：门铃和智能门锁对话固定进入这里。"
          badge="agent:doorman:doorbell"
          sessionKey="agent:doorman:doorbell"
          tone="door"
        />
      </div>
    </div>
  );
}
