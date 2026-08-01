import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// 控制 trace 检测信号：getTurnStatus 恒 "done"（不睡眠），peekTurnMeta 按 runId 返回 meta。
type TurnMeta = { success: boolean; errorMsg: string | null } | undefined;
const registerTraceLinkMock = vi.fn<(runId: string, traceId: string) => void>();
const getTurnStatusMock = vi.fn<() => string>(() => "done");
const peekTurnMetaMock = vi.fn<(runId: string) => TurnMeta>();

vi.mock("../src/hooks/trace.js", () => ({
  registerTraceLink: (runId: string, traceId: string) =>
    registerTraceLinkMock(runId, traceId),
  getTurnStatus: () => getTurnStatusMock(),
  peekTurnMeta: (runId: string) => peekTurnMetaMock(runId),
}));

const writeOnboardingInviteStateMock = vi.fn();

vi.mock("../src/home-profile/onboarding_state.js", () => ({
  writeOnboardingInviteState: (...args: unknown[]) =>
    writeOnboardingInviteStateMock(...args),
}));

// owner-channel 解析：按测试用例切换"解析到车主会话 / 无可用 channel"。
type ResolveResultLike = {
  target: { sessionKey: string } | null;
  targets: { sessionKey: string }[];
};
const resolveNotifyTargetMock = vi.fn<() => ResolveResultLike>(() => ({
  target: null,
  targets: [],
}));

vi.mock("../src/tools/notify.js", () => ({
  resolveNotifyTarget: () => resolveNotifyTargetMock(),
}));

const runShellMock = vi.fn();

vi.mock("../src/utils/shell.js", () => ({
  runShell: (...args: unknown[]) => runShellMock(...args),
}));

const loadSharedConfigMock = vi.fn((_api: unknown) => ({
  server: { url: "http://127.0.0.1:1810", token: "miloco-token" },
}));

vi.mock("../src/miloco/config.js", () => ({
  loadSharedConfig: (api: unknown) => loadSharedConfigMock(api),
}));

import { kAgentWebhook } from "../src/webhooks/agent.js";

const OVERFLOW = "Context overflow: prompt too large for the model (precheck).";
const SESSION = "agent:main:miloco-rule";

type Wait = { status: string; error?: string };

function makeApi(opts: {
  waitByRunId?: Record<string, Wait>;
  deleteSession?: ReturnType<typeof vi.fn>;
}) {
  // 平台实测：runId == 传入的 idempotencyKey，这里照此模拟以区分首次/重试。
  const run = vi.fn(async (p: { idempotencyKey: string }) => ({
    runId: p.idempotencyKey,
  }));
  const waitForRun = vi.fn(
    async (p: { runId: string }) =>
      opts.waitByRunId?.[p.runId] ?? { status: "ok" },
  );
  const deleteSession = opts.deleteSession ?? vi.fn(async () => {});
  const api = {
    runtime: { subagent: { run, waitForRun, deleteSession } },
  } as never;
  return { api, run, waitForRun, deleteSession };
}

function invoke(api: unknown, idempotencyKey = "t1") {
  return kAgentWebhook.action({
    api,
    payload: {
      message: "m",
      sessionKey: SESSION,
      idempotencyKey,
      traceId: "tr",
      timeoutMs: 1000,
    },
  } as never);
}

beforeEach(() => {
  runShellMock.mockResolvedValue({
    status: 0,
    stdout: "",
    stderr: "",
    signal: null,
    error: null,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, text: async () => "ok" })),
  );
});

afterEach(() => {
  vi.clearAllMocks();
  getTurnStatusMock.mockReturnValue("done");
  vi.unstubAllGlobals();
});

describe("kAgentWebhook 上下文溢出自愈", () => {
  it("doorbellReplyAudio：OpenClaw 回复后先唤醒门锁，再播放回复音频", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
      responseText: "请稍等，我马上来。",
    }));
    const { api } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    const res = (await kAgentWebhook.action({
      api,
      payload: {
        message: "门铃被按下",
        sessionKey: SESSION,
        idempotencyKey: "t1",
        traceId: "tr",
        timeoutMs: 1000,
        doorbellReplyAudio: {
          conversationId: "conv-1",
          did: "1179479632",
          siid: 7,
          eiid: 1006,
          wakeActionIid: "action.17.3",
          audioCommand: [
            "/tmp/play-doorlock-audio",
            "{text}",
            "{did}",
            "{siid}.{eiid}",
          ],
        },
      },
    } as never)) as { responseText?: string };

    expect(res.responseText).toBe("请稍等，我马上来。");
    expect(runShellMock).toHaveBeenCalledTimes(2);
    expect(runShellMock).toHaveBeenNthCalledWith(1, "miloco-cli", [
      "device",
      "action",
      "1179479632",
      "action.17.3",
    ]);
    expect(runShellMock).toHaveBeenNthCalledWith(2, "/tmp/play-doorlock-audio", [
      "请稍等，我马上来。",
      "1179479632",
      "7.1006",
    ]);
    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:1810/api/miot/doorbell/reply-audio-result",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer miloco-token" }),
        body: JSON.stringify({
          conversationId: "conv-1",
          did: "1179479632",
          success: true,
        }),
      }),
    );
  });

  it("doorbellReplyAudio：播放失败后回调 failure", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
      responseText: "请稍等。",
    }));
    runShellMock
      .mockResolvedValueOnce({ status: 0, stdout: "", stderr: "", signal: null, error: null })
      .mockResolvedValueOnce({ status: 1, stdout: "", stderr: "boom", signal: null, error: null });
    const { api } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    await kAgentWebhook.action({
      api,
      payload: {
        message: "门铃被按下",
        sessionKey: SESSION,
        idempotencyKey: "t1",
        traceId: "tr",
        timeoutMs: 1000,
        doorbellReplyAudio: {
          conversationId: "conv-2",
          did: "1179479632",
          wakeActionIid: "action.17.3",
          audioCommand: ["/tmp/play-doorlock-audio", "{text}"],
        },
      },
    } as never);

    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:1810/api/miot/doorbell/reply-audio-result",
      expect.objectContaining({
        body: JSON.stringify({
          conversationId: "conv-2",
          did: "1179479632",
          success: false,
          error: "audio command failed: boom",
        }),
      }),
    );
  });

  it("溢出 → deleteSession 一次 → 重试成功 → recovered=true", async () => {
    peekTurnMetaMock.mockImplementation((runId: string) =>
      runId === "t1"
        ? { success: false, errorMsg: OVERFLOW }
        : { success: true, errorMsg: null },
    );
    const { api, run, waitForRun, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" }, "t1:retry": { status: "ok" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      error?: string;
      recovered?: boolean;
    };

    expect(deleteSession).toHaveBeenCalledTimes(1);
    expect(deleteSession).toHaveBeenCalledWith({
      sessionKey: SESSION,
      deleteTranscript: true,
    });
    expect(run).toHaveBeenCalledTimes(2);
    expect(res.runId).toBe("t1:retry");
    expect(res.status).toBe("ok");
    expect(res.recovered).toBe(true);
    // 即便已恢复，也把触发自愈的溢出原因带回后端
    expect(res.error).toContain("Context overflow");
    // 重试等待预算由 timeoutMs 推算而非固定 60s：payload timeoutMs=1000 < 下限 → 取 10s 地板
    expect(waitForRun).toHaveBeenNthCalledWith(2, {
      runId: "t1:retry",
      timeoutMs: 10_000,
    });
  });

  it("非溢出失败 → 不删除、不重试", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: "tool blew up",
    }));
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "error", error: "tool blew up" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      recovered?: boolean;
    };

    expect(deleteSession).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledTimes(1);
    expect(res.runId).toBe("t1");
    expect(res.status).toBe("error");
    expect(res.recovered).toBeUndefined();
  });

  it("deleteSession 抛错（如主会话保护）→ 返回首个结果、不崩", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: OVERFLOW,
    }));
    const deleteSession = vi.fn(async () => {
      throw new Error("Cannot delete the main session");
    });
    const { api, run } = makeApi({
      waitByRunId: { t1: { status: "ok" } },
      deleteSession,
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      recovered?: boolean;
    };

    expect(deleteSession).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledTimes(1); // 抛错发生在重试前 → 不重试
    expect(res.runId).toBe("t1");
    expect(res.recovered).toBeUndefined();
  });

  it("重试后仍溢出（系统提示型不可恢复）→ recovered=false、不死循环", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: OVERFLOW,
    }));
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" }, "t1:retry": { status: "ok" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      error?: string;
      recovered?: boolean;
    };

    expect(deleteSession).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledTimes(2); // 恰好两次：首次 + 一次重试，不再继续
    expect(res.runId).toBe("t1:retry");
    expect(res.recovered).toBe(false);
    expect(res.error).toContain("Context overflow"); // 不可恢复时带回溢出原因
  });

  it("未溢出（success=true）→ 行为不变，不触发自愈", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" } },
    });

    const res = (await invoke(api)) as {
      runId: string;
      status: string;
      recovered?: boolean;
    };

    expect(deleteSession).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledTimes(1);
    expect(res.runId).toBe("t1");
    expect(res.status).toBe("ok");
    expect(res.recovered).toBeUndefined();
  });
});

describe("kAgentWebhook owner-channel 投递", () => {
  function invokeWith(api: unknown, extra: Record<string, unknown>) {
    return kAgentWebhook.action({
      api,
      payload: {
        message: "m",
        sessionKey: SESSION,
        idempotencyKey: "t1",
        traceId: "tr",
        timeoutMs: 1000,
        ...extra,
      },
    } as never);
  }

  it("默认路径不变：deliver:false、用 payload sessionKey，且不做 channel 解析", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    const { api, run } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    await invoke(api);

    expect(resolveNotifyTargetMock).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledWith(
      expect.objectContaining({ sessionKey: SESSION, deliver: false }),
    );
  });

  it("resolveTarget=owner-channel：turn 跑在解析出的车主会话且 deliver:true", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
      targets: [{ sessionKey: "wechat:dm:owner-1" }],
    });
    const { api, run } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string; status: string };

    expect(run).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionKey: "wechat:dm:owner-1",
        deliver: true,
      }),
    );
    expect(res.status).toBe("ok");
    expect(res.runId).toBe("t1");
  });

  it("owner-channel 显式 deliver:false 时尊重调用方", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
      targets: [{ sessionKey: "wechat:dm:owner-1" }],
    });
    const { api, run } = makeApi({ waitByRunId: { t1: { status: "ok" } } });

    await invokeWith(api, { resolveTarget: "owner-channel", deliver: false });

    expect(run).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionKey: "wechat:dm:owner-1",
        deliver: false,
      }),
    );
  });

  it("无可用 IM channel → 结构化 no-channel（非抛错），不起 turn", async () => {
    resolveNotifyTargetMock.mockReturnValue({ target: null, targets: [] });
    const { api, run } = makeApi({});

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string | null; status: string; error?: string };

    expect(run).not.toHaveBeenCalled();
    expect(res.runId).toBeNull();
    expect(res.status).toBe("no-channel");
    expect(res.error).toContain("no available IM channel");
  });

  it("owner-channel 溢出不做 deleteSession 自愈（不删用户真实会话）", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: false,
      errorMsg: OVERFLOW,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
      targets: [{ sessionKey: "wechat:dm:owner-1" }],
    });
    const { api, run, deleteSession } = makeApi({
      waitByRunId: { t1: { status: "ok" } },
    });

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string };

    expect(deleteSession).not.toHaveBeenCalled();
    expect(run).toHaveBeenCalledTimes(1); // 无重试 turn
    expect(res.runId).toBe("t1");
  });

  it("owner-channel 多绑定时广播邀请到全部会话", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
      targets: [
        { sessionKey: "wechat:dm:owner-1" },
        { sessionKey: "telegram:dm:owner-2" },
      ],
    });
    const { api, run } = makeApi({
      waitByRunId: {
        "t1:broadcast:0": { status: "ok" },
        "t1:broadcast:1": { status: "ok" },
      },
    });

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string; status: string };

    expect(run).toHaveBeenCalledTimes(2);
    expect(writeOnboardingInviteStateMock).toHaveBeenCalledWith([
      "wechat:dm:owner-1",
      "telegram:dm:owner-2",
    ]);
    expect(res.runId).toBe("t1:broadcast:0");
    expect(res.status).toBe("ok");
  });

  it("owner-channel 广播部分失败时，只要有一条成功仍按送达回报", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
      targets: [
        { sessionKey: "wechat:dm:owner-1" },
        { sessionKey: "telegram:dm:owner-2" },
      ],
    });
    const { api, run } = makeApi({
      waitByRunId: {
        "t1:broadcast:0": { status: "error", error: "boom" },
        "t1:broadcast:1": { status: "ok" },
      },
    });

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string; status: string };

    expect(run).toHaveBeenCalledTimes(2);
    expect(writeOnboardingInviteStateMock).toHaveBeenCalledWith([
      "telegram:dm:owner-2",
    ]);
    expect(res.runId).toBe("t1:broadcast:1");
    expect(res.status).toBe("ok");
  });

  it("owner-channel 广播全部失败时不记录 onboarding 邀请状态", async () => {
    peekTurnMetaMock.mockImplementation(() => ({
      success: true,
      errorMsg: null,
    }));
    resolveNotifyTargetMock.mockReturnValue({
      target: { sessionKey: "wechat:dm:owner-1" },
      targets: [
        { sessionKey: "wechat:dm:owner-1" },
        { sessionKey: "telegram:dm:owner-2" },
      ],
    });
    const { api, run } = makeApi({
      waitByRunId: {
        "t1:broadcast:0": { status: "error", error: "boom" },
        "t1:broadcast:1": { status: "error", error: "boom" },
      },
    });

    const res = (await invokeWith(api, {
      resolveTarget: "owner-channel",
    })) as { runId: string; status: string };

    expect(run).toHaveBeenCalledTimes(2);
    expect(writeOnboardingInviteStateMock).not.toHaveBeenCalled();
    expect(res.runId).toBe("t1:broadcast:0");
    expect(res.status).toBe("error");
  });
});
