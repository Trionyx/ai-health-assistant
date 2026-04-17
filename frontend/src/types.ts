export type FreshnessStatus = "fresh" | "acceptable" | "stale";
export type ConfidenceLevel = "high" | "medium" | "low";

export type TaskItem = {
  task_id: string;
  title: string;
  description: string;
  status: "export_candidate";
  is_recommended_first?: boolean;
};

export type EvidenceReference = {
  title: string;
  pmid: string;
  journal: string;
  year?: number | null;
  short_summary: string;
  query_key: "sleep_deficit" | "recovery_strain" | "overload_pattern" | "stress_fatigue";
};

export type WorkflowTraceStep = {
  name: string;
  status: "running" | "completed" | "warning";
  freshness?: FreshnessStatus;
  confidence?: ConfidenceLevel;
  detail: string;
  warning_reason?: string | null;
  graph_node?: string | null;
  llm_excerpt?: string | null;
  duration_ms?: number | null;
  occurred_at?: string;
};

export type GPReportOutput = {
  report_id: string;
  snapshot_id: string;
  kind: "daily" | "weekly";
  problem_list: string[];
  daily_brief?: string | null;
  weekly_report?: string | null;
  priority_flags: string[];
  next_steps: string[];
  tasks: TaskItem[];
  evidence_support?: EvidenceReference[];
  top_priority?: string | null;
  today_status?: "steady" | "caution" | "recovery_focus" | "overload" | null;
  overall_confidence: ConfidenceLevel;
  uncertainty_notes: string[];
  safety_notes: string[];
};

export type WorkflowResponse = {
  run_id: string;
  conversation_id: string;
  report: GPReportOutput;
  trace: {
    run_id: string;
    current_node?: string | null;
    steps: WorkflowTraceStep[];
  };
  memory: {
    memory_id: string;
    category: string;
    content: string;
    created_at?: string;
  }[];
  error?: string | null;
  used_fallback?: boolean;
};

export type MetaResponse = {
  llm_provider: string;
  llm_model: string;
  llm_configured: boolean;
  evidence_enabled?: boolean;
  evidence_configured?: boolean;
  evidence_transport?: string;
};

export type CheckInStartResponse = {
  session_id: string;
  conversation_id: string;
  question: string;
};

export type CheckInReplyResponse = {
  session_id: string;
  conversation_id: string;
  status: "active" | "completed" | "cancelled";
  next_question?: string | null;
  run?: {
    run_id: string;
    report: GPReportOutput;
    trace: {
      run_id: string;
      current_node?: string | null;
      steps: WorkflowTraceStep[];
    };
    memory: {
      memory_id: string;
      category: string;
      content: string;
    }[];
    error?: string | null;
    used_fallback?: boolean;
  };
};

export type AuthUser = {
  user_id: string;
  email: string;
  display_name?: string | null;
};

export type RunEventsResponse = {
  run_id: string;
  status: "running" | "completed" | "failed";
  current_node: string;
  current_node_elapsed_ms?: number;
  running_for_ms?: number;
  events: WorkflowTraceStep[];
  result: WorkflowResponse | null;
  error: string | null;
};

export type MockScenario = {
  scenario_id: string;
  title: string;
  problem_case: string;
  description: string;
  expected_agent_reactions: { text: string; tone: "warning" | "positive" | "neutral" }[];
  scenario_start_date?: string;
  current_simulated_date?: string;
  visible_window_start?: string;
  visible_window_end?: string;
};
