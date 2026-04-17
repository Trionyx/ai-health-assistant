import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, RefObject } from "react";
import type {
  AuthUser,
  CheckInReplyResponse,
  CheckInStartResponse,
  GPReportOutput,
  MetaResponse,
  MockScenario,
  RunEventsResponse,
  WorkflowResponse
} from "./types";

const apiBase = "/api";
const DISCLAIMER_SENTENCES = [
  "This assistant supports reflection and wellness planning only.",
  "It does not provide medical diagnosis or treatment advice.",
  "For urgent or clinical concerns, consult a qualified clinician."
];

function normalizeSpaces(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function dedupeRepeatedSentences(text: string): string {
  const sentenceMatches = text.match(/[^.!?]+[.!?]?/g) ?? [];
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const sentence of sentenceMatches.map((item) => normalizeSpaces(item)).filter(Boolean)) {
    const key = sentence.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      unique.push(sentence);
    }
  }
  return unique.join(" ").trim();
}

function splitUniqueParagraphs(text: string): string[] {
  const parts = text
    .split(/\n\s*\n/)
    .map((part) => dedupeRepeatedSentences(normalizeSpaces(part)))
    .filter(Boolean);
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const part of parts) {
    const key = part.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      unique.push(part);
    }
  }
  return unique;
}

function isDisclaimerParagraph(text: string): boolean {
  const lowered = text.toLowerCase();
  return DISCLAIMER_SENTENCES.some((sentence) => lowered.includes(sentence.toLowerCase()));
}

function isNumericScaleQuestion(text: string): boolean {
  const lowered = text.toLowerCase();
  return /(1\s*[-to]+\s*10|1-10|1 to 10|scale)/.test(lowered);
}

type ReminderDraft = {
  startLocal: string;
  durationMinutes: number;
};

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function getDefaultReminderStart(): string {
  const now = new Date();
  now.setMinutes(now.getMinutes() + 60);
  now.setSeconds(0, 0);
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}T${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
}

function toGoogleDateTime(date: Date): string {
  return `${date.getUTCFullYear()}${pad2(date.getUTCMonth() + 1)}${pad2(date.getUTCDate())}T${pad2(date.getUTCHours())}${pad2(date.getUTCMinutes())}00Z`;
}

function buildGoogleCalendarUrl(taskTitle: string, taskDescription: string, startLocal: string, durationMinutes: number): string {
  const start = new Date(startLocal);
  if (Number.isNaN(start.getTime())) {
    return "https://calendar.google.com/calendar/render?action=TEMPLATE";
  }
  const clampedDuration = Math.max(5, durationMinutes || 30);
  const end = new Date(start.getTime() + clampedDuration * 60 * 1000);
  const params = new URLSearchParams({
    action: "TEMPLATE",
    text: taskTitle,
    details: taskDescription,
    dates: `${toGoogleDateTime(start)}/${toGoogleDateTime(end)}`
  });
  return `https://calendar.google.com/calendar/render?${params.toString()}`;
}

export default function App() {
  type AdminTab = "user" | "trace" | "memory" | "dataset";
  const [workflow, setWorkflow] = useState<WorkflowResponse | null>(null);
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [memoryItems, setMemoryItems] = useState<WorkflowResponse["memory"]>([]);
  const [status, setStatus] = useState("Idle");
  const [adminMode, setAdminMode] = useState(false);
  const [adminTab, setAdminTab] = useState<AdminTab>("user");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<{ role: "assistant" | "user"; content: string }[]>([
    {
      role: "assistant",
      content: "Hello. Start guided check-in to answer a few quick questions before daily synthesis."
    }
  ]);
  const [replyText, setReplyText] = useState("");
  const [checkinBusy, setCheckinBusy] = useState(false);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [authEmail, setAuthEmail] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [runningKind, setRunningKind] = useState<"daily" | "weekly" | null>(null);
  const [currentNodeElapsedMs, setCurrentNodeElapsedMs] = useState<number | null>(null);
  const [expandedTrace, setExpandedTrace] = useState<Record<string, boolean>>({});
  const [showTraceLegend, setShowTraceLegend] = useState(false);
  const [showWhySummary, setShowWhySummary] = useState(false);
  const [lastCheckinHours, setLastCheckinHours] = useState<number | null>(null);
  const [mockScenarios, setMockScenarios] = useState<MockScenario[]>([]);
  const [currentScenario, setCurrentScenario] = useState<MockScenario | null>(null);
  const [showReminderModal, setShowReminderModal] = useState(false);
  const [taskReminderDrafts, setTaskReminderDrafts] = useState<Record<string, ReminderDraft>>({});
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const traceScrollRef = useRef<HTMLDivElement | null>(null);
  const tasksSectionRef = useRef<HTMLElement | null>(null);
  const evidenceSectionRef = useRef<HTMLElement | null>(null);

  async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
    return fetch(`${apiBase}${path}`, {
      credentials: "include",
      ...init
    });
  }

  async function refreshLastCheckinHours() {
    try {
      const response = await apiFetch("/checkins/last");
      if (!response.ok) {
        return;
      }
      const payload = (await response.json()) as { hours_since_last: number | null };
      setLastCheckinHours(payload.hours_since_last);
    } catch {
      setLastCheckinHours(null);
    }
  }

  async function refreshMockScenarioState() {
    try {
      const [listResponse, currentResponse] = await Promise.all([
        apiFetch("/mock/scenarios"),
        apiFetch("/mock/scenarios/current")
      ]);
      if (listResponse.ok) {
        setMockScenarios((await listResponse.json()) as MockScenario[]);
      }
      if (currentResponse.ok) {
        setCurrentScenario((await currentResponse.json()) as MockScenario);
      }
    } catch {
      setMockScenarios([]);
      setCurrentScenario(null);
    }
  }

  async function selectScenario(scenarioId: string) {
    const response = await apiFetch("/mock/scenarios/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: scenarioId })
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail ?? "Unable to select scenario.");
    }
    const selected = (await response.json()) as MockScenario;
    setCurrentScenario(selected);
    setStatus(`Scenario selected: ${selected.title}`);
  }

  async function advanceSimulatedDay() {
    const response = await apiFetch("/mock/simulated-date/advance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days: 1 })
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail ?? "Unable to advance simulated date.");
    }
    const updated = (await response.json()) as MockScenario;
    setCurrentScenario(updated);
    setStatus(`Simulated day advanced to ${updated.current_simulated_date ?? "next day"}`);
  }

  async function hydrateLatestData() {
    try {
      const [reportResponse, traceResponse, memoryResponse] = await Promise.all([
        apiFetch("/reports/latest"),
        apiFetch("/trace/latest"),
        apiFetch("/memory")
      ]);
      let memoryPayload: WorkflowResponse["memory"] = [];
      if (!memoryResponse.ok) {
        setMemoryItems([]);
      } else {
        memoryPayload = (await memoryResponse.json()) as WorkflowResponse["memory"];
        setMemoryItems(memoryPayload);
      }
      if (reportResponse.ok && traceResponse.ok) {
        const reportPayload = (await reportResponse.json()) as GPReportOutput;
        const tracePayload = (await traceResponse.json()) as WorkflowResponse["trace"];
        setWorkflow({
          run_id: tracePayload.run_id,
          conversation_id: "",
          report: reportPayload,
          trace: tracePayload,
          memory: memoryPayload,
          error: null,
          used_fallback: false
        });
      }
    } catch {
      // keep UI usable even if hydration fails
    }
  }

  useEffect(() => {
    apiFetch("/meta")
      .then((response) => response.json())
      .then((payload: MetaResponse) => setMeta(payload))
      .catch(() => setMeta(null));
    refreshLastCheckinHours();
    apiFetch("/auth/me")
      .then(async (response) => {
        if (!response.ok) {
          throw new Error("not-authenticated");
        }
        return response.json();
      })
      .then((payload: AuthUser) => {
        setCurrentUser(payload);
        refreshLastCheckinHours();
        hydrateLatestData();
        refreshMockScenarioState();
      })
      .catch(() => setCurrentUser(null));
  }, []);

  useEffect(() => {
    const el = chatScrollRef.current;
    if (!el) {
      return;
    }
    el.scrollTop = el.scrollHeight;
  }, [chatMessages, checkinBusy]);

  useEffect(() => {
    const el = traceScrollRef.current;
    if (!el) {
      return;
    }
    // Newest events are shown first; keep viewport focused on top.
    if (el.scrollTop < 96) {
      el.scrollTop = 0;
    }
  }, [workflow?.trace.run_id, workflow?.trace.steps.length]);

  async function run(kind: "daily" | "weekly") {
    setRunError(null);
    setRunningKind(kind);
    setStatus(`Running ${kind} workflow...`);
    try {
      const startResponse = await apiFetch(`/runs/${kind}/start`, { method: "POST" });
      if (!startResponse.ok) {
        const payload = await startResponse.json();
        throw new Error(payload.detail ?? "Workflow request failed.");
      }
      const startPayload = (await startResponse.json()) as { run_id: string };
      let completed = false;
      while (!completed) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        const response = await apiFetch(`/runs/${startPayload.run_id}/events`);
        if (!response.ok) {
          const payload = await response.json();
          throw new Error(payload.detail ?? "Failed to fetch run events.");
        }
        const payload = (await response.json()) as RunEventsResponse;
        setCurrentNodeElapsedMs(payload.current_node_elapsed_ms ?? null);
        setWorkflow((current) => ({
          run_id: payload.result?.run_id ?? current?.run_id ?? payload.run_id,
          conversation_id: payload.result?.conversation_id ?? current?.conversation_id ?? "",
          report:
            payload.result?.report ??
            current?.report ?? {
              report_id: "",
              snapshot_id: "",
              kind,
              problem_list: [],
              priority_flags: [],
              next_steps: [],
              tasks: [],
              overall_confidence: "medium",
              uncertainty_notes: [],
              safety_notes: [],
              evidence_support: []
            },
          trace: {
            run_id: payload.run_id,
            current_node: payload.current_node,
            steps: payload.events
          },
          memory: payload.result?.memory ?? current?.memory ?? [],
          error: payload.result?.error ?? current?.error ?? null,
          used_fallback: payload.result?.used_fallback ?? current?.used_fallback ?? false
        }));
        if (payload.status === "failed") {
          throw new Error(payload.error ?? "Workflow failed.");
        }
        if (payload.status === "completed") {
          completed = true;
          setCurrentNodeElapsedMs(null);
          if (payload.result) {
            setWorkflow(payload.result);
            setRunError(payload.result.error ?? null);
            setStatus(payload.result.error ? `${kind} workflow finished with fallback` : `${kind} workflow complete`);
            await hydrateLatestData();
          } else {
            setStatus(`${kind} workflow complete`);
            await hydrateLatestData();
          }
        } else {
          setStatus(`Running ${kind} workflow... (${payload.current_node})`);
        }
      }
    } catch (error) {
      setRunError(error instanceof Error ? error.message : "Workflow execution failed.");
      setStatus(`${kind} workflow failed`);
    } finally {
      setRunningKind(null);
      setCurrentNodeElapsedMs(null);
    }
  }
  function formatElapsed(ms?: number | null): string {
    if (!ms || ms <= 0) {
      return "0.0s";
    }
    return `${(ms / 1000).toFixed(1)}s`;
  }

  async function startCheckin() {
    setCheckinBusy(true);
    setStatus("Starting guided check-in...");
    try {
      const response = await apiFetch("/checkins/start", { method: "POST" });
      if (!response.ok) {
        throw new Error("Unable to start guided check-in.");
      }
      const payload = (await response.json()) as CheckInStartResponse;
      setSessionId(payload.session_id);
      setChatMessages((current) => [...current, { role: "assistant", content: payload.question }]);
      setStatus("Guided check-in active");
    } finally {
      setCheckinBusy(false);
    }
  }

  async function submitCheckinReply(event?: FormEvent, directResponse?: string) {
    event?.preventDefault();
    const userText = (directResponse ?? replyText).trim();
    if (!sessionId || !userText) {
      return;
    }
    setCheckinBusy(true);
    setReplyText("");
    setChatMessages((current) => [...current, { role: "user", content: userText }]);
    try {
      const response = await apiFetch(`/checkins/${sessionId}/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ response: userText })
      });
      if (!response.ok) {
        throw new Error("Unable to submit response.");
      }
      const payload = (await response.json()) as CheckInReplyResponse;
      if (payload.next_question) {
        setChatMessages((current) => [...current, { role: "assistant", content: payload.next_question! }]);
      }
      if (payload.run) {
        setWorkflow({
          run_id: payload.run.run_id,
          conversation_id: payload.conversation_id,
          report: payload.run.report,
          trace: payload.run.trace,
          memory: payload.run.memory
        });
        setMemoryItems(payload.run.memory);
        setRunError(payload.run.error ?? null);
        setSessionId(null);
        setChatMessages((current) => [
          ...current,
          {
            role: "assistant",
            content:
              "Check-in complete. Next, review 'Today at a glance', then open 'Key findings' and 'Tasks' to choose 1-2 actions for today."
          }
        ]);
        setStatus(payload.run.error ? "Guided check-in completed with fallback" : "Guided check-in completed and daily synthesis generated");
        refreshLastCheckinHours();
      }
    } catch (error) {
      setRunError(error instanceof Error ? error.message : "Unexpected check-in error.");
    } finally {
      setCheckinBusy(false);
    }
  }

  async function submitQuickScore(value: number) {
    if (!sessionId || checkinBusy) {
      return;
    }
    await submitCheckinReply(undefined, String(value));
  }

  async function submitAuth(event: FormEvent) {
    event.preventDefault();
    setAuthError(null);
    try {
      const response = await apiFetch(`/auth/${authMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: authEmail })
      });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(payload.detail ?? "Authentication failed.");
      }
      const payload = (await response.json()) as AuthUser;
      setCurrentUser(payload);
      setStatus(`Signed in as ${payload.email}`);
      refreshMockScenarioState();
      hydrateLatestData();
      refreshLastCheckinHours();
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Authentication failed.");
    }
  }

  async function logout() {
    await apiFetch("/auth/logout", { method: "POST" });
    setCurrentUser(null);
    setWorkflow(null);
    setMemoryItems([]);
    setStatus("Logged out");
  }

  const report: GPReportOutput | null = workflow?.report ?? null;
  const latestAssistantMessage = [...chatMessages].reverse().find((message) => message.role === "assistant")?.content ?? "";
  const showQuickScoreButtons = Boolean(sessionId && isNumericScaleQuestion(latestAssistantMessage) && !checkinBusy);
  const canSubmitReply = Boolean(sessionId && replyText.trim() && !checkinBusy);
  const chatSummaryText = useMemo(
    () => report?.daily_brief ?? report?.weekly_report ?? "No synthesis yet. Start the guided check-in.",
    [report]
  );
  const summaryParagraphs = useMemo(() => splitUniqueParagraphs(chatSummaryText), [chatSummaryText]);
  const disclaimerParagraphs = useMemo(
    () => summaryParagraphs.filter((part) => isDisclaimerParagraph(part)),
    [summaryParagraphs]
  );
  const infoParagraphs = useMemo(
    () => summaryParagraphs.filter((part) => !isDisclaimerParagraph(part)),
    [summaryParagraphs]
  );
  const traceSteps = workflow?.trace.steps ?? [];
  const tracePathNodes = useMemo(
    () => {
      const orderedUniqueNodes: { id: string; label: string; status: "running" | "completed" | "warning" }[] = [];
      const seen = new Set<string>();
      const indexById = new Map<string, number>();
      for (const step of traceSteps) {
        const id = step.graph_node ?? step.name;
        if (!seen.has(id)) {
          seen.add(id);
          indexById.set(id, orderedUniqueNodes.length);
          orderedUniqueNodes.push({
            id,
            label: id.replace(/_/g, " "),
            status: step.status
          });
        } else {
          const index = indexById.get(id);
          if (index !== undefined) {
            orderedUniqueNodes[index] = { ...orderedUniqueNodes[index], status: step.status };
          }
        }
      }
      return orderedUniqueNodes;
    },
    [traceSteps]
  );
  const activeTraceNode = workflow?.trace.current_node ?? tracePathNodes.at(-1)?.id ?? null;
  const traceStepsNewest = useMemo(
    () => traceSteps.map((step, rawIndex) => ({ step, rawIndex })).reverse(),
    [traceSteps]
  );
  const memorySorted = useMemo(
    () =>
      [...memoryItems].sort((a, b) => {
        const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
        const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
        return bTime - aTime;
      }),
    [memoryItems]
  );
  const confidenceTone = {
    high: "bg-emerald-100 text-emerald-700 ring-1 ring-emerald-200",
    medium: "bg-amber-100 text-amber-700 ring-1 ring-amber-200",
    low: "bg-rose-100 text-rose-700 ring-1 ring-rose-200"
  };
  const todayStatusTone: Record<"steady" | "caution" | "recovery_focus" | "overload", string> = {
    steady: "bg-emerald-100 text-emerald-700 ring-1 ring-emerald-200",
    caution: "bg-amber-100 text-amber-700 ring-1 ring-amber-200",
    recovery_focus: "bg-indigo-100 text-indigo-700 ring-1 ring-indigo-200",
    overload: "bg-rose-100 text-rose-700 ring-1 ring-rose-200"
  };
  const todayStatusLabel: Record<"steady" | "caution" | "recovery_focus" | "overload", string> = {
    steady: "Steady",
    caution: "Caution",
    recovery_focus: "Recovery focus",
    overload: "Overload"
  };
  const topPriorityText =
    report?.top_priority ??
    report?.tasks.find((task) => task.is_recommended_first)?.title ??
    report?.tasks[0]?.title ??
    null;
  const whySummaryBullets = useMemo(() => {
    if (!report) {
      return [];
    }
    const candidates = [
      ...report.problem_list.slice(0, 2),
      ...report.uncertainty_notes.slice(0, 1),
      ...report.next_steps.slice(0, 1)
    ]
      .map((item) => dedupeRepeatedSentences(item))
      .filter(Boolean);
    const unique: string[] = [];
    for (const candidate of candidates) {
      if (!unique.some((existing) => existing.toLowerCase() === candidate.toLowerCase())) {
        unique.push(candidate);
      }
    }
    return unique.slice(0, 3);
  }, [report]);

  useEffect(() => {
    const tasks = report?.tasks ?? [];
    if (!tasks.length) {
      setTaskReminderDrafts({});
      return;
    }
    setTaskReminderDrafts((current) => {
      const next: Record<string, ReminderDraft> = {};
      for (const task of tasks) {
        next[task.task_id] = current[task.task_id] ?? {
          startLocal: getDefaultReminderStart(),
          durationMinutes: 30
        };
      }
      return next;
    });
  }, [report?.report_id, report?.tasks]);

  function openReminderModal() {
    if (!(report?.tasks?.length)) {
      window.alert("No tasks available yet.");
      return;
    }
    setShowReminderModal(true);
  }

  function updateReminderDraft(taskId: string, patch: Partial<ReminderDraft>) {
    setTaskReminderDrafts((current) => ({
      ...current,
      [taskId]: {
        startLocal: current[taskId]?.startLocal ?? getDefaultReminderStart(),
        durationMinutes: current[taskId]?.durationMinutes ?? 30,
        ...patch
      }
    }));
  }

  function createGoogleReminder(taskId: string, title: string, description: string) {
    const draft = taskReminderDrafts[taskId] ?? { startLocal: getDefaultReminderStart(), durationMinutes: 30 };
    const url = buildGoogleCalendarUrl(title, description, draft.startLocal, draft.durationMinutes);
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function scrollToSection(sectionRef: RefObject<HTMLElement | null>) {
    sectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function formatDateTime(value?: string): string {
    if (!value) {
      return "n/a";
    }
    return new Date(value).toLocaleString();
  }

  function formatDurationMs(durationMs?: number | null): string {
    if (durationMs === undefined || durationMs === null) {
      return "";
    }
    if (durationMs < 1000) {
      return `${durationMs}ms`;
    }
    return `${(durationMs / 1000).toFixed(1)}s`;
  }

  if (!currentUser) {
    return (
      <div className="min-h-screen bg-[#edf2fb] text-slate-800">
        <div className="mx-auto flex min-h-screen max-w-lg items-center px-4 py-8">
          <div className="w-full rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
            <h1 className="text-3xl font-semibold tracking-[-0.04em] text-slate-800">Health Assistant</h1>
            <p className="mt-2 text-sm text-slate-500">
              {authMode === "login" ? "Login by email to continue." : "Register with your email to continue."}
            </p>
            <form onSubmit={submitAuth} className="mt-5 space-y-3">
              <input
                value={authEmail}
                onChange={(event) => setAuthEmail(event.target.value)}
                placeholder="Email"
                className="w-full rounded-full border border-slate-200 bg-white px-4 py-3 text-sm outline-none"
              />
              <button type="submit" className="w-full rounded-full bg-[#4d86ef] px-4 py-3 text-sm font-semibold text-white">
                {authMode === "login" ? "Login" : "Register"}
              </button>
              <button
                type="button"
                onClick={() => setAuthMode((mode) => (mode === "login" ? "register" : "login"))}
                className="w-full rounded-full bg-white px-4 py-3 text-sm font-semibold text-slate-700"
              >
                {authMode === "login" ? "Need account? Register" : "Have account? Login"}
              </button>
              {authError && <div className="text-sm text-rose-600">{authError}</div>}
            </form>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#edf2fb] text-slate-800">
      <div className="mx-auto max-w-[1400px] px-4 py-5 md:px-6">
        <header className="mb-6 flex flex-wrap items-center gap-4 rounded-[30px] bg-[#f4f7fd] px-5 py-4 shadow-[0_20px_70px_rgba(153,175,218,0.12)]">
          <div className="rounded-full bg-white px-4 py-2 text-sm text-slate-700 shadow-sm">
            Signed in: <span className="font-semibold">{currentUser.email}</span>
          </div>
          <div className="ml-auto flex items-center gap-3">
            {currentUser && (
              <button
                onClick={logout}
                className="rounded-full bg-white px-4 py-3 text-sm font-semibold text-slate-700 shadow-[0_8px_30px_rgba(173,184,206,0.18)]"
              >
                Logout
              </button>
            )}
            <button
              onClick={() => setAdminMode((value) => !value)}
              className={`rounded-full px-4 py-3 text-sm font-semibold ${
                adminMode
                  ? "bg-slate-900 text-white shadow-[0_12px_30px_rgba(15,23,42,0.35)]"
                  : "bg-white text-slate-700 shadow-[0_8px_30px_rgba(173,184,206,0.18)]"
              }`}
            >
              {adminMode ? "Admin mode on" : "Admin mode"}
            </button>
          </div>
        </header>

        {adminMode && (
          <section className="mb-6 rounded-[28px] bg-slate-900 p-4 text-slate-100 shadow-[0_20px_70px_rgba(15,23,42,0.35)]">
            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={() => setAdminTab("user")}
                className={`rounded-full px-4 py-2 text-sm font-semibold ${
                  adminTab === "user" ? "bg-white text-slate-900" : "bg-slate-800 text-slate-200"
                }`}
              >
                User view
              </button>
              <button
                onClick={async () => {
                  setAdminTab("trace");
                  await run("daily");
                }}
                className="rounded-full bg-slate-700 px-4 py-2 text-sm font-semibold text-white transition hover:bg-slate-600"
              >
                Run daily...
              </button>
              <button
                onClick={async () => {
                  setAdminTab("trace");
                  await run("weekly");
                }}
                className="rounded-full bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-500"
              >
                Run weekly
              </button>
              <button
                onClick={() => setAdminTab("trace")}
                className={`rounded-full px-4 py-2 text-sm font-semibold ${
                  adminTab === "trace" ? "bg-white text-slate-900" : "bg-slate-800 text-slate-200"
                }`}
              >
                Trace view
              </button>
              <button
                onClick={() => setAdminTab("memory")}
                className={`rounded-full px-4 py-2 text-sm font-semibold ${
                  adminTab === "memory" ? "bg-white text-slate-900" : "bg-slate-800 text-slate-200"
                }`}
              >
                Insights about you
              </button>
              <button
                onClick={() => setAdminTab("dataset")}
                className={`rounded-full px-4 py-2 text-sm font-semibold ${
                  adminTab === "dataset" ? "bg-white text-slate-900" : "bg-slate-800 text-slate-200"
                }`}
              >
                Scenario context
              </button>
            </div>
          </section>
        )}

        {(adminTab === "user" || !adminMode) && <main className="grid gap-6">
          <div className="grid min-w-0 gap-6">
            <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
              <section className="rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
                <h1 className="mb-5 text-4xl font-semibold tracking-[-0.05em] text-slate-800">
                  Your daily health companion
                </h1>

                <div className="h-[300px] overflow-y-auto rounded-[22px] bg-[#eef3fc] p-3">
                  <div ref={chatScrollRef} className="h-full overflow-y-auto space-y-3 pr-1">
                    {chatMessages.map((message, index) => (
                      <div
                        key={`${message.role}-${index}`}
                        className={`max-w-[88%] rounded-[22px] px-4 py-3 text-sm leading-6 shadow-sm ${
                          message.role === "assistant"
                            ? "bg-white text-slate-700"
                            : "ml-auto bg-[#4d86ef] text-white"
                        }`}
                      >
                        {message.content}
                      </div>
                    ))}
                  </div>
                </div>

                <div className="mt-5 space-y-3">
                  <div className="rounded-[22px] border border-[#dce6f8] bg-white p-4 shadow-sm">
                    <button
                      onClick={startCheckin}
                      disabled={Boolean(sessionId) || checkinBusy}
                      className="rounded-full bg-[#313846] px-5 py-3 text-sm font-semibold text-white disabled:opacity-60"
                    >
                      {sessionId ? "Check-in active" : "Start guided check-in"}
                    </button>
                    <div className="mt-2 text-xs text-slate-500">
                      {lastCheckinHours === null
                        ? "No completed check-in yet."
                        : `Last completed check-in: ${lastCheckinHours.toFixed(1)} h ago`}
                    </div>
                  </div>
                  <form onSubmit={submitCheckinReply} className="rounded-[22px] bg-white p-3 shadow-sm">
                    {showQuickScoreButtons && (
                      <div className="mb-3 grid grid-cols-5 gap-2 sm:grid-cols-10">
                        {Array.from({ length: 10 }, (_, index) => index + 1).map((score) => (
                          <button
                            key={score}
                            type="button"
                            onClick={async () => submitQuickScore(score)}
                            className="rounded-full border border-slate-200 bg-[#eef3fc] px-2 py-2 text-xs font-semibold text-slate-700 transition hover:bg-[#dce6f8]"
                          >
                            {score}
                          </button>
                        ))}
                      </div>
                    )}
                    <div className="flex items-center gap-2">
                      <input
                        value={replyText}
                        onChange={(event) => setReplyText(event.target.value)}
                        placeholder={sessionId ? "Type your answer..." : "Start check-in first"}
                        className="min-w-0 flex-1 rounded-full border border-slate-200 px-4 py-3 text-sm outline-none"
                      />
                      <button
                        type="submit"
                        disabled={!canSubmitReply}
                        className="rounded-full bg-[#4d86ef] px-5 py-3 text-sm font-semibold text-white disabled:opacity-60"
                      >
                        Send
                      </button>
                    </div>
                  </form>
                  {!sessionId && report?.daily_brief && (
                    <div className="rounded-[18px] border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm leading-6 text-emerald-900">
                      <div className="text-xs font-semibold uppercase tracking-[0.14em] text-emerald-700">
                        Check-in completed
                      </div>
                      <div className="mt-1">
                        Your daily synthesis is ready. Next step: review Today at a glance, then pick one task from Tasks to execute now.
                      </div>
                    </div>
                  )}
                </div>
              </section>

              <section className="grid gap-6">
                <section className="rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <div className="text-xs font-semibold uppercase tracking-[0.24em] text-slate-400">
                        GP report
                      </div>
                      <h2 className="mt-3 text-3xl font-semibold tracking-[-0.04em] text-slate-800">
                        {report?.kind === "weekly" ? "Weekly summary" : "Today at a glance"}
                      </h2>
                      {report?.kind === "weekly" && (report.evidence_support?.length ?? 0) > 0 && (
                        <button
                          type="button"
                          onClick={() => scrollToSection(evidenceSectionRef)}
                          className="mt-3 inline-flex items-center gap-2 rounded-full border border-blue-300 bg-[#4d86ef] px-4 py-2 text-sm font-semibold text-white shadow-[0_8px_24px_rgba(77,134,239,0.35)] transition hover:bg-[#3d79e8]"
                        >
                          <span aria-hidden>↓</span>
                          <span>See evidence</span>
                        </button>
                      )}
                    </div>
                    <span
                      className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] ${
                        report ? confidenceTone[report.overall_confidence] : confidenceTone.medium
                      }`}
                    >
                      {report?.overall_confidence ?? "no-run"}
                    </span>
                  </div>
                  {report?.today_status && (
                    <div className="mt-3">
                      <span
                        className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.14em] ${
                          todayStatusTone[report.today_status]
                        }`}
                      >
                        {todayStatusLabel[report.today_status]}
                      </span>
                    </div>
                  )}
                  <div className="mt-4 space-y-3">
                    {runError ? (
                      <div className="rounded-[16px] border border-rose-300 bg-rose-50 px-4 py-3 text-sm leading-6 text-rose-900">
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-rose-700">
                          Workflow error
                        </div>
                        <div className="mt-1">{runError}</div>
                      </div>
                    ) : (
                      <>
                        {disclaimerParagraphs.map((paragraph) => (
                          <div
                            key={paragraph}
                            className="rounded-[16px] border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900"
                          >
                            <div className="text-xs font-semibold uppercase tracking-[0.14em] text-amber-700">
                              Safety disclaimer
                            </div>
                            <div className="mt-1">{paragraph}</div>
                          </div>
                        ))}
                        {infoParagraphs.map((paragraph) => (
                          <p key={paragraph} className="text-base leading-7 text-slate-600">
                            {paragraph}
                          </p>
                        ))}
                        {topPriorityText && (
                          <div className="rounded-[16px] border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm leading-6 text-indigo-900">
                            <div className="flex items-center justify-between gap-3">
                              <div className="text-xs font-semibold uppercase tracking-[0.14em] text-indigo-700">
                                Top priority today
                              </div>
                              <button
                                type="button"
                                onClick={() => scrollToSection(tasksSectionRef)}
                                className="inline-flex items-center gap-2 rounded-full border border-indigo-300 bg-indigo-600 px-4 py-2 text-xs font-semibold uppercase tracking-[0.08em] text-white shadow-[0_8px_24px_rgba(79,70,229,0.3)] transition hover:bg-indigo-500"
                              >
                                <span>Other tasks</span>
                                <span aria-hidden>→</span>
                              </button>
                            </div>
                            <div className="mt-1">{topPriorityText}</div>
                          </div>
                        )}
                        {whySummaryBullets.length > 0 && (
                          <div className="rounded-[16px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700">
                            <button
                              type="button"
                              onClick={() => setShowWhySummary((value) => !value)}
                              className="w-full text-left text-xs font-semibold uppercase tracking-[0.14em] text-slate-500"
                            >
                              Why this summary? {showWhySummary ? "Hide" : "Show"}
                            </button>
                            {showWhySummary && (
                              <ul className="mt-2 space-y-2 text-sm leading-6 text-slate-700">
                                {whySummaryBullets.map((bullet) => (
                                  <li key={bullet} className="rounded-[12px] bg-slate-50 px-3 py-2">
                                    {bullet}
                                  </li>
                                ))}
                              </ul>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </section>

                <section className="grid gap-6 md:grid-cols-2">
                  <article className="rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
                    <div className="text-xs font-semibold uppercase tracking-[0.24em] text-slate-400">
                      Scenario context
                    </div>
                    <div className="mt-3 rounded-xl bg-white p-3 text-sm text-slate-700 shadow-sm">
                      <div className="font-semibold">{currentScenario?.title ?? "Baseline mixed quality"}</div>
                      <div className="mt-1 text-xs uppercase tracking-[0.14em] text-slate-400">Problem case</div>
                      <div className="mt-1 text-sm leading-6">{currentScenario?.problem_case ?? "Mixed-quality signals."}</div>
                    </div>
                    <ul className="mt-4 space-y-3 text-sm leading-6 text-slate-700">
                      {(currentScenario?.expected_agent_reactions ?? [
                        { text: "Sleep agent returns moderate confidence findings.", tone: "neutral" as const },
                        { text: "Activity agent highlights slight staleness.", tone: "warning" as const },
                        { text: "Recovery agent raises caution due to stale/sparse feed.", tone: "warning" as const }
                      ]).map((item) => (
                        <li
                          key={item.text}
                          className={`rounded-[16px] border px-3 py-2 ${
                            item.tone === "warning"
                              ? "border-rose-200 bg-rose-50 text-rose-800"
                              : item.tone === "positive"
                                ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                                : "border-slate-200 bg-white text-slate-700"
                          }`}
                        >
                          {item.text}
                        </li>
                      ))}
                    </ul>
                    <div className="mt-4 rounded-[16px] border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
                      <div>Simulated date: {currentScenario?.current_simulated_date ?? "n/a"}</div>
                      <div>Scenario start: {currentScenario?.scenario_start_date ?? "n/a"}</div>
                      <div>
                        Visible window: {currentScenario?.visible_window_start ?? "n/a"} to {currentScenario?.visible_window_end ?? "n/a"}
                      </div>
                    </div>
                  </article>

                  <article className="rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
                    <div className="text-xs font-semibold uppercase tracking-[0.24em] text-slate-400">
                      Workflow status
                    </div>
                    <div className="mt-4 rounded-[22px] bg-white p-4 text-sm leading-6 text-slate-600 shadow-sm">
                      {status}
                    </div>
                  </article>
                </section>
              </section>
            </section>

            <section className="grid gap-6 xl:grid-cols-[1.05fr_0.95fr]">
              <section ref={tasksSectionRef} className="rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
                <div className="flex items-center justify-between gap-4">
                  <h2 className="text-2xl font-semibold tracking-[-0.03em] text-slate-800">Tasks</h2>
                  <button
                    onClick={openReminderModal}
                    className="rounded-full bg-[#e8f1ff] px-4 py-2 text-sm font-semibold text-[#4d86ef]"
                  >
                    Export to reminders
                  </button>
                </div>
                <ul className="mt-5 space-y-3">
                  {report?.tasks.map((task, index) => (
                    <li key={task.task_id} className="rounded-[22px] bg-white p-4 shadow-sm">
                      <div className="flex items-center justify-between gap-2">
                        <div className="font-semibold text-slate-800">{task.title}</div>
                        {(task.is_recommended_first || index === 0) && (
                          <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-indigo-700">
                            Start here
                          </span>
                        )}
                      </div>
                      <div className="mt-1 text-sm leading-6 text-slate-600">{task.description}</div>
                    </li>
                  )) ?? <li className="text-sm text-slate-500">No tasks yet.</li>}
                </ul>
              </section>

              <section className="rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
                <h2 className="text-2xl font-semibold tracking-[-0.03em] text-slate-800">Key findings</h2>
                <ul className="mt-5 space-y-3 text-sm leading-6 text-slate-700">
                  {report?.problem_list.map((item) => (
                    <li key={item} className="rounded-[22px] bg-white p-4 shadow-sm">
                      {item}
                    </li>
                  )) ?? <li className="text-slate-500">No findings yet.</li>}
                </ul>
              </section>

              {report?.kind === "weekly" && (report.evidence_support?.length ?? 0) > 0 && (
                <section ref={evidenceSectionRef} className="rounded-[32px] bg-[#f4f7fd] p-6 shadow-[0_18px_60px_rgba(159,178,216,0.14)]">
                  <h2 className="text-2xl font-semibold tracking-[-0.03em] text-slate-800">Evidence support</h2>
                  <ul className="mt-5 space-y-3 text-sm leading-6 text-slate-700">
                    {report.evidence_support?.map((item) => (
                      <li key={`${item.pmid}-${item.title}`} className="rounded-[22px] bg-white p-4 shadow-sm">
                        <div className="font-semibold text-slate-800">{item.title}</div>
                        <div className="mt-1 text-xs uppercase tracking-[0.12em] text-slate-500">
                          PMID: {item.pmid} | {item.journal}{item.year ? ` (${item.year})` : ""}
                        </div>
                        <div className="mt-2 text-sm leading-6 text-slate-600">{item.short_summary}</div>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </section>
          </div>
        </main>}

        {adminMode && adminTab === "trace" && (
          <section className="rounded-[32px] bg-slate-900 p-6 text-slate-100 shadow-[0_20px_70px_rgba(15,23,42,0.35)]">
            <div className="flex items-center gap-3">
              <h2 className="text-2xl font-semibold tracking-[-0.03em]">Trace view</h2>
              {runningKind && (
                <div className="inline-flex items-center gap-2 rounded-full border border-indigo-400/40 bg-indigo-500/15 px-3 py-1 text-xs font-semibold uppercase tracking-[0.12em] text-indigo-200">
                  <span className="relative inline-flex h-2.5 w-2.5">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-indigo-300 opacity-75"></span>
                    <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-indigo-300"></span>
                  </span>
                  {runningKind} running
                </div>
              )}
              <button
                type="button"
                onClick={() => setShowTraceLegend((value) => !value)}
                className="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-600 bg-slate-800 text-xs font-semibold text-slate-200"
                title="Show graph color legend"
              >
                ?
              </button>
            </div>
            <div className="mt-3 text-xs uppercase tracking-[0.16em] text-slate-400">
              Current graph node: {workflow?.trace.current_node ?? "n/a"}
              {runningKind && (
                <span className="ml-2 text-slate-400">
                  (running {formatElapsed(currentNodeElapsedMs)})
                </span>
              )}
              {runningKind && <span className="ml-2 inline-block animate-pulse text-indigo-300">live</span>}
            </div>
            {showTraceLegend && (
              <div className="mt-3 rounded-xl border border-slate-700 bg-slate-800/70 p-3 text-xs text-slate-200">
                <div className="font-semibold uppercase tracking-[0.12em] text-slate-300">Graph colors</div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <span className="rounded-full border border-indigo-300 bg-indigo-400/20 px-3 py-1 text-indigo-100">Current node</span>
                  <span className="rounded-full border border-amber-500/40 bg-amber-900/30 px-3 py-1 text-amber-200">Warning</span>
                  <span className="rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-slate-300">Completed</span>
                </div>
              </div>
            )}
            <div className="mt-4 overflow-x-auto pb-1">
              <div className="inline-flex min-w-full items-center gap-2">
                {tracePathNodes.length === 0 ? (
                  <div className="rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs uppercase tracking-[0.14em] text-slate-400">
                    Waiting for events...
                  </div>
                ) : (
                  tracePathNodes.map((node) => {
                    const isActive = activeTraceNode === node.id;
                    return (
                      <div
                        key={node.id}
                        className={`rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.12em] ${
                          isActive
                            ? "border-indigo-300 bg-indigo-400/20 text-indigo-100"
                            : node.status === "running"
                              ? "border-indigo-400/50 bg-indigo-500/10 text-indigo-200"
                              : node.status === "warning"
                              ? "border-amber-500/40 bg-amber-900/30 text-amber-200"
                              : "border-slate-700 bg-slate-800 text-slate-300"
                        }`}
                      >
                        {node.label}
                      </div>
                    );
                  })
                )}
              </div>
            </div>
            <div ref={traceScrollRef} className="mt-5 grid max-h-[72vh] min-w-0 gap-3 overflow-y-auto pr-1">
              {traceStepsNewest.map(({ step, rawIndex }) => {
                const stepKey = `${workflow?.trace.run_id ?? workflow?.run_id ?? "run"}-${rawIndex}`;
                return (
                  <div
                    key={stepKey}
                    className={`min-w-0 rounded-[22px] border border-slate-700 p-4 ${
                      step.status === "warning"
                        ? "bg-amber-950/40"
                        : step.status === "running"
                          ? "bg-indigo-950/30"
                          : "bg-slate-800/80"
                    }`}
                  >
                    <div className="flex min-w-0 items-center justify-between gap-2 text-sm font-semibold uppercase tracking-[0.18em]">
                      <span className="truncate">{step.name}</span>
                      <span className="text-[10px] text-slate-400">
                        run: {workflow?.report.kind ?? "n/a"}
                        {step.duration_ms != null && (
                          <span className="ml-2 text-slate-500">took {formatDurationMs(step.duration_ms)}</span>
                        )}
                      </span>
                    </div>
                    <div className="mt-1 text-[10px] uppercase tracking-[0.14em] text-slate-400">{formatDateTime(step.occurred_at)}</div>
                    <div className="mt-2 truncate text-xs uppercase tracking-[0.16em] text-slate-400" title={step.graph_node ?? "unknown"}>
                      node: {step.graph_node ?? "unknown"}
                    </div>
                    {step.status === "warning" && step.warning_reason && (
                      <div className="mt-2 rounded-lg border border-amber-500/40 bg-amber-900/30 px-3 py-2 text-xs leading-5 text-amber-200">
                        Warning reason: {step.warning_reason}
                      </div>
                    )}
                    <p className="mt-2 break-words text-sm leading-6 text-slate-100">
                      {expandedTrace[stepKey] ? step.detail : `${step.detail.slice(0, 160)}${step.detail.length > 160 ? "..." : ""}`}
                    </p>
                    {step.detail.length > 160 && (
                      <button onClick={() => setExpandedTrace((prev) => ({ ...prev, [stepKey]: !prev[stepKey] }))} className="mt-1 text-xs font-semibold text-indigo-300">
                        {expandedTrace[stepKey] ? "Collapse" : "Expand"}
                      </button>
                    )}
                    {step.llm_excerpt && (
                      <div className="mt-2 min-w-0 rounded-xl bg-slate-950 p-3 text-xs text-slate-300">
                        <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">LLM excerpt</div>
                        {expandedTrace[`llm-${stepKey}`] ? (
                          <pre className="mt-2 whitespace-pre-wrap break-words text-xs leading-5 text-slate-200">{step.llm_excerpt}</pre>
                        ) : (
                          <span className="mt-2 block break-words">
                            {`${step.llm_excerpt.slice(0, 180)}${step.llm_excerpt.length > 180 ? "..." : ""}`}
                          </span>
                        )}
                        {step.llm_excerpt.length > 180 && (
                          <button
                            onClick={() => setExpandedTrace((prev) => ({ ...prev, [`llm-${stepKey}`]: !prev[`llm-${stepKey}`] }))}
                            className="ml-2 text-xs font-semibold text-indigo-300"
                          >
                            {expandedTrace[`llm-${stepKey}`] ? "less" : "more"}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
              {traceStepsNewest.length === 0 && (
                <div className="rounded-[18px] border border-slate-700 bg-slate-800/70 px-4 py-3 text-sm text-slate-300">
                  No trace events yet. Run a daily or weekly workflow to populate live events.
                </div>
              )}
            </div>
          </section>
        )}

        {adminMode && adminTab === "memory" && (
          <section className="rounded-[32px] bg-slate-900 p-6 text-slate-100 shadow-[0_20px_70px_rgba(15,23,42,0.35)]">
            <h2 className="text-2xl font-semibold tracking-[-0.03em]">Insights about you</h2>
            <ul className="mt-5 space-y-3">
              {memorySorted.map((item) => (
                <li key={item.memory_id} className="rounded-[22px] border border-slate-700 bg-slate-800/80 p-4">
                  <div className="text-xs uppercase tracking-[0.18em] text-slate-400">{item.category}</div>
                  <div className="mt-1 text-[11px] text-slate-500">{formatDateTime(item.created_at)}</div>
                  <div className="mt-2 text-sm leading-6 text-slate-100">{item.content}</div>
                </li>
              ))}
              {memorySorted.length === 0 && (
                <li className="rounded-[22px] border border-slate-700 bg-slate-800/70 px-4 py-3 text-sm text-slate-300">
                  Insights will appear after your next synthesis run.
                </li>
              )}
            </ul>
          </section>
        )}

        {adminMode && adminTab === "dataset" && (
          <section className="rounded-[32px] bg-slate-900 p-6 text-slate-100 shadow-[0_20px_70px_rgba(15,23,42,0.35)]">
            <h2 className="text-2xl font-semibold tracking-[-0.03em]">Scenario context generation</h2>
            <p className="mt-2 text-sm text-slate-400">
              Select a mock dataset edge case to test agent/GP behavior. This affects the next generated runs.
            </p>
            <div className="mt-4 grid gap-4">
              {mockScenarios.map((scenario) => {
                const isActive = currentScenario?.scenario_id === scenario.scenario_id;
                return (
                  <div
                    key={scenario.scenario_id}
                    className={`rounded-[20px] border p-4 ${isActive ? "border-indigo-400 bg-indigo-500/10" : "border-slate-700 bg-slate-800/70"}`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <div className="text-base font-semibold">{scenario.title}</div>
                        <div className="mt-1 text-xs uppercase tracking-[0.14em] text-slate-400">Problem case</div>
                        <div className="mt-1 text-sm text-slate-200">{scenario.problem_case}</div>
                      </div>
                      <button
                        onClick={async () => {
                          await selectScenario(scenario.scenario_id);
                        }}
                        className={`rounded-full px-4 py-2 text-sm font-semibold ${
                          isActive ? "bg-white text-slate-900" : "bg-indigo-600 text-white hover:bg-indigo-500"
                        }`}
                        disabled={isActive}
                      >
                        {isActive ? "Active" : "Generate / Select"}
                      </button>
                    </div>
                    <div className="mt-3 text-sm text-slate-300">{scenario.description}</div>
                    <div className="mt-2 text-xs text-slate-400">
                      Simulated: {scenario.current_simulated_date ?? "n/a"} | Window: {scenario.visible_window_start ?? "n/a"} to {scenario.visible_window_end ?? "n/a"}
                    </div>
                    <ul className="mt-3 space-y-2 text-sm text-slate-200">
                      {scenario.expected_agent_reactions.map((reaction) => (
                        <li key={reaction.text}>- {reaction.text}</li>
                      ))}
                    </ul>
                    {isActive && (
                      <button
                        onClick={async () => {
                          await advanceSimulatedDay();
                        }}
                        className="mt-3 rounded-full bg-slate-700 px-4 py-2 text-xs font-semibold text-white hover:bg-slate-600"
                      >
                        Advance simulated day
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {showReminderModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 px-4 py-6">
            <div className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-[28px] bg-[#f4f7fd] p-6 shadow-[0_24px_80px_rgba(15,23,42,0.28)]">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h3 className="text-2xl font-semibold tracking-[-0.03em] text-slate-800">Export tasks to Google Calendar</h3>
                  <p className="mt-1 text-sm text-slate-600">Set a time per task, then create each reminder as a calendar event.</p>
                </div>
                <button
                  type="button"
                  onClick={() => setShowReminderModal(false)}
                  className="rounded-full bg-white px-4 py-2 text-sm font-semibold text-slate-700 shadow-sm"
                >
                  Close
                </button>
              </div>

              <ul className="mt-5 space-y-3">
                {(report?.tasks ?? []).map((task) => {
                  const draft = taskReminderDrafts[task.task_id] ?? { startLocal: getDefaultReminderStart(), durationMinutes: 30 };
                  return (
                    <li key={task.task_id} className="rounded-[20px] bg-white p-4 shadow-sm">
                      <div className="font-semibold text-slate-800">{task.title}</div>
                      <div className="mt-1 text-sm leading-6 text-slate-600">{task.description}</div>
                      <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_160px_auto]">
                        <label className="text-xs uppercase tracking-[0.12em] text-slate-500">
                          Start time
                          <input
                            type="datetime-local"
                            value={draft.startLocal}
                            onChange={(event) => updateReminderDraft(task.task_id, { startLocal: event.target.value })}
                            className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-700 outline-none"
                          />
                        </label>
                        <label className="text-xs uppercase tracking-[0.12em] text-slate-500">
                          Duration (min)
                          <input
                            type="number"
                            min={5}
                            step={5}
                            value={draft.durationMinutes}
                            onChange={(event) =>
                              updateReminderDraft(task.task_id, {
                                durationMinutes: Math.max(5, Number(event.target.value) || 30)
                              })
                            }
                            className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-700 outline-none"
                          />
                        </label>
                        <div className="flex items-end">
                          <button
                            type="button"
                            onClick={() => createGoogleReminder(task.task_id, task.title, task.description)}
                            className="w-full rounded-full bg-[#4d86ef] px-4 py-2 text-sm font-semibold text-white sm:w-auto"
                          >
                            Create reminder
                          </button>
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
