import { makeApi, Zodios, type ZodiosOptions } from "@zodios/core";
import { z } from "zod";

const HealthResponse = z
  .object({
    status: z.string().optional().default("ok"),
    browser_ready: z.boolean(),
    chrome_path: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const NavigateStep = z
  .object({
    action: z.literal("navigate").default("navigate"),
    url: z.string(),
    new_tab: z.boolean().optional().default(false),
    settle_seconds: z.number().gte(0).lte(60).optional().default(3),
  })
  .passthrough();
const WaitForStep = z
  .object({
    action: z.literal("wait_for").default("wait_for"),
    selector: z.string(),
    timeout_s: z.number().gt(0).lte(120).optional().default(15),
  })
  .passthrough();
const WaitForAnyStep = z
  .object({
    action: z.literal("wait_for_any").default("wait_for_any"),
    selectors: z.array(z.string()).min(1),
    timeout_s: z.number().gt(0).lte(120).optional().default(15),
  })
  .passthrough();
const WaitForEvalStep = z
  .object({
    action: z.literal("wait_for_eval").default("wait_for_eval"),
    expression: z.string(),
    timeout_s: z.number().gt(0).lte(120).optional().default(15),
    interval_s: z.number().gt(0).lte(5).optional().default(0.25),
  })
  .passthrough();
const WaitForTextStep = z
  .object({
    action: z.literal("wait_for_text").default("wait_for_text"),
    text: z.string(),
    timeout_s: z.number().gt(0).lte(120).optional().default(15),
  })
  .passthrough();
const SleepStep = z
  .object({
    action: z.literal("sleep").default("sleep"),
    seconds: z.number().gt(0).lte(60),
  })
  .passthrough();
const ClickStep = z
  .object({
    action: z.literal("click").default("click"),
    selector: z.string(),
    timeout_s: z.number().gt(0).lte(120).optional().default(15),
  })
  .passthrough();
const ScrollStep = z
  .object({
    action: z.literal("scroll").default("scroll"),
    direction: z.enum(["down", "up"]).default("down"),
    amount: z.number().int().default(50),
    times: z.number().int().gte(1).lte(50).default(1),
  })
  .partial()
  .passthrough();
const ExtractStep = z
  .object({
    action: z.literal("extract").default("extract"),
    name: z.string(),
    selector: z.string(),
    kind: z.enum(["text", "html", "attr"]).optional().default("text"),
    attr: z.union([z.string(), z.null()]).optional(),
    many: z.boolean().optional().default(false),
  })
  .passthrough();
const FindTextStep = z
  .object({
    action: z.literal("find_text").default("find_text"),
    name: z.string(),
    text: z.string(),
    timeout_s: z.number().gt(0).lte(120).optional().default(10),
  })
  .passthrough();
const EvalStep = z
  .object({
    action: z.literal("eval").default("eval"),
    expression: z.string(),
    name: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const ScreenshotStep = z
  .object({
    action: z.literal("screenshot").default("screenshot"),
    name: z.string(),
    full_page: z.boolean().optional().default(false),
  })
  .passthrough();
const Proxy = z
  .object({
    server: z.string(),
    username: z.union([z.string(), z.null()]).optional(),
    password: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const Cookie = z
  .object({
    name: z.string(),
    value: z.string(),
    domain: z.union([z.string(), z.null()]).optional(),
    path: z.string().optional().default("/"),
  })
  .passthrough();
const RunRequest = z
  .object({
    steps: z
      .array(
        z.discriminatedUnion("action", [
          NavigateStep,
          WaitForStep,
          WaitForAnyStep,
          WaitForEvalStep,
          WaitForTextStep,
          SleepStep,
          ClickStep,
          ScrollStep,
          ExtractStep,
          FindTextStep,
          EvalStep,
          ScreenshotStep,
        ])
      )
      .min(1),
    start_url: z.union([z.string(), z.null()]).optional(),
    session_id: z.union([z.string(), z.null()]).optional(),
    proxy: z.union([Proxy, z.null()]).optional(),
    cookies: z.union([z.array(Cookie), z.null()]).optional(),
    headless: z.boolean().optional().default(false),
  })
  .passthrough();
const StepResult = z
  .object({
    index: z.number().int(),
    action: z.string(),
    ok: z.boolean(),
    error: z.union([z.string(), z.null()]).optional(),
    duration_ms: z.number().int().optional().default(0),
  })
  .passthrough();
const RunResponse = z
  .object({
    ok: z.boolean(),
    final_url: z.union([z.string(), z.null()]).optional(),
    elapsed_ms: z.number().int(),
    data: z.object({}).partial().passthrough().optional(),
    steps: z.array(StepResult).optional(),
    timings: z.record(z.number().int()).optional(),
    error: z.union([z.string(), z.null()]).optional(),
  })
  .passthrough();
const ValidationError = z
  .object({
    loc: z.array(z.union([z.string(), z.number()])),
    msg: z.string(),
    type: z.string(),
    input: z.unknown().optional(),
    ctx: z.object({}).partial().passthrough().optional(),
  })
  .passthrough();
const HTTPValidationError = z
  .object({ detail: z.array(ValidationError) })
  .partial()
  .passthrough();
const SessionInfo = z
  .object({
    id: z.string(),
    label: z.union([z.string(), z.null()]).optional(),
    exit_server: z.union([z.string(), z.null()]).optional(),
    created_ts: z.number(),
    last_used_ts: z.number(),
    age_s: z.number().int(),
    idle_s: z.number().int(),
    runs: z.number().int(),
  })
  .passthrough();
const SessionListResponse = z
  .object({ sessions: z.array(SessionInfo) })
  .partial()
  .passthrough();
const SessionCreateRequest = z
  .object({
    label: z.union([z.string(), z.null()]),
    proxy_country: z.union([z.string(), z.null()]),
    proxy_type: z.union([z.string(), z.null()]),
    warmup: z.boolean().default(true),
    warmup_url: z.union([z.string(), z.null()]),
    warmup_settle_s: z.union([z.number(), z.null()]),
  })
  .partial()
  .passthrough();

export const schemas = {
  HealthResponse,
  NavigateStep,
  WaitForStep,
  WaitForAnyStep,
  WaitForEvalStep,
  WaitForTextStep,
  SleepStep,
  ClickStep,
  ScrollStep,
  ExtractStep,
  FindTextStep,
  EvalStep,
  ScreenshotStep,
  Proxy,
  Cookie,
  RunRequest,
  StepResult,
  RunResponse,
  ValidationError,
  HTTPValidationError,
  SessionInfo,
  SessionListResponse,
  SessionCreateRequest,
};

const endpoints = makeApi([
  {
    method: "get",
    path: "/health",
    alias: "health_health_get",
    requestFormat: "json",
    response: HealthResponse,
  },
  {
    method: "post",
    path: "/run",
    alias: "run_run_post",
    description: `Execute a scenario (list of steps) in a real Chrome and return the
extracted data. Use &#x60;start_url&#x60; for the initial navigation.`,
    requestFormat: "json",
    parameters: [
      {
        name: "body",
        type: "Body",
        schema: RunRequest,
      },
    ],
    response: RunResponse,
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "get",
    path: "/sessions",
    alias: "list_sessions_sessions_get",
    description: `List live warm sessions with age/idle/run counters.`,
    requestFormat: "json",
    response: SessionListResponse,
  },
  {
    method: "post",
    path: "/sessions",
    alias: "create_session_sessions_post",
    description: `Launch a warm, proxied browser and keep it alive for reuse via
&#x60;POST /run&#x60; with its &#x60;session_id&#x60;. Hold several (e.g. a small pool per
marketplace) to scrape batches in parallel without re-warming each card.`,
    requestFormat: "json",
    parameters: [
      {
        name: "body",
        type: "Body",
        schema: SessionCreateRequest,
      },
    ],
    response: SessionInfo,
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
  {
    method: "delete",
    path: "/sessions/:session_id",
    alias: "delete_session_sessions__session_id__delete",
    description: `Tear down a warm session (stops its browser, frees the exit IP).`,
    requestFormat: "json",
    parameters: [
      {
        name: "session_id",
        type: "Path",
        schema: z.string(),
      },
    ],
    response: z.object({}).partial().passthrough(),
    errors: [
      {
        status: 422,
        description: `Validation Error`,
        schema: HTTPValidationError,
      },
    ],
  },
]);

export const api = new Zodios(endpoints);

export function createApiClient(baseUrl: string, options?: ZodiosOptions) {
  return new Zodios(baseUrl, endpoints, options);
}
