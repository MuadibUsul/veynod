/**
 * Veynod v0.2 protocol schemas (doc://veynod-role-rules §5.1).
 *
 * The ingress layer rejects illegal states before they reach the database:
 * a missing value is never accepted, only the tri-state VALUE / PENDING / NA
 * is. `parse*` helpers turn a Zod failure into a 422 `validation_error`, which
 * is what the API returns to an agent.
 */
import { z } from "zod";

/** Explicit defaults from §1.6; never rely on an implicit platform default. */
export const DEFAULT_LEASE_SECONDS = 900;
export const LEASE_RENEW_WINDOW_SEC = 300;
export const MAX_LEASE_RENEWS = 12;
export const DEFAULT_MAX_RETRIES = 3;
export const PRIORITY_MIN = -50;
export const PRIORITY_MAX = 100;

/** A schema violation is a caller mistake: 422, fix the request and resend. */
export class ProtocolValidationError extends Error {
  readonly status = 422;
  readonly code = "validation_error";

  constructor(readonly issues: z.ZodIssue[]) {
    const first = issues[0];
    const where = first && first.path.length > 0 ? `${first.path.join(".")}: ` : "";
    super(`invalid protocol payload: ${where}${first?.message ?? "unknown issue"}`);
    this.name = "ProtocolValidationError";
  }
}

/** Parse with `schema`, or throw `ProtocolValidationError` (status 422). */
export function parseWith<T extends z.ZodTypeAny>(schema: T, value: unknown): z.infer<T> {
  const result = schema.safeParse(value);
  if (!result.success) {
    throw new ProtocolValidationError(result.error.issues);
  }
  return result.data;
}

/** §1.1: VALUE | PENDING (with target and reason) | NA (with reason). */
export const TriState = <T extends z.ZodTypeAny>(valueSchema: T) =>
  z.discriminatedUnion("mode", [
    z.object({ mode: z.literal("VALUE"), value: valueSchema }),
    z.object({
      mode: z.literal("PENDING"),
      field_name: z.string().min(1),
      escalate_to: z.string().regex(/^(agent|user):[a-zA-Z0-9_-]+$/),
      reason: z.string().min(5),
    }),
    z.object({
      mode: z.literal("NA"),
      reason: z.string().min(5),
    }),
  ]);

export type TriStateValue<T> =
  | { mode: "VALUE"; value: T }
  | { mode: "PENDING"; field_name: string; escalate_to: string; reason: string }
  | { mode: "NA"; reason: string };

/** §3.2: the four project-attribution fields, each tri-state. */
export const ProjectAttributionSchema = z.object({
  project: TriState(z.enum(["Veynod", "Tline", "plus.tlines.tech", "AgentGraph"])),
  repo: TriState(z.string().regex(/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/)),
  local_path: TriState(z.string().min(1)),
  deploy_target: TriState(z.string().min(1)),
});

/** §3.3: short `doc://<doc_key>` form with an explicit positive version pin. */
export const ContextDocRefSchema = z.object({
  uri: z
    .string()
    .regex(/^doc:\/\/[a-z0-9-]+$/, "禁止携带命名空间前缀，仅允许 doc://<doc_key>"),
  version: z.number().int().positive("必须显式 pin 正整数版本号"),
});

const VAGUE_PATTERNS = /^(做好|优化一下|处理一下|修复|搞定|完成任务|待定|TBD)$/i;

/** §3.1 + Q7: acceptance items must be checkable; AUTO needs a runner. */
export const AcceptanceItemSchema = z
  .object({
    id: z.string().regex(/^acc-[a-z0-9-]+$/),
    kind: z.enum(["AUTO", "MANUAL"]),
    statement: z
      .string()
      .min(10, "验收标准必须为可独立判定的具体陈述（至少10字符）")
      .refine((val) => !VAGUE_PATTERNS.test(val.trim()), "包含禁用模糊表述"),
    auto_runner_id: z.string().min(1).optional(),
  })
  .refine(
    (data) => (data.kind === "AUTO" ? Boolean(data.auto_runner_id) : true),
    {
      message: "kind=AUTO 必须绑定已注册的 auto_runner_id，无机器检查器请标 MANUAL",
      path: ["auto_runner_id"],
    },
  );

/** §5.1.5: create-task payload. */
export const CreateTaskPayloadSchema = z.object({
  task_key: z
    .string()
    .regex(/^[a-z0-9]+(-[a-z0-9]+)*$/, "必须为严格 kebab-case，禁止斜杠与大写"),
  namespace: z.string().min(1),
  title: z.string().min(6, "禁止只写代号"),
  description: z.string().min(20, "描述必须包含背景与具体执行目标"),
  attribution: ProjectAttributionSchema,
  acceptance: z.array(AcceptanceItemSchema).min(1, "验收条目至少一条"),
  priority: z.number().int().min(PRIORITY_MIN).max(PRIORITY_MAX),
  kind: z.enum(["work", "recon", "spec", "audit"]),
  assigned_role: z.enum(["EXECUTOR", "ANALYST", "AUDITOR", "SCOUT"]),
  assignee: z
    .string()
    .regex(/^(agent|user):[a-zA-Z0-9_-]+$/)
    .nullable()
    .default(null),
  depends_on: z.array(z.string()).default([]),
  context_docs: z.array(ContextDocRefSchema).default([]),
  max_retries: z.number().int().min(0).max(10).default(DEFAULT_MAX_RETRIES),
  requires_code_delivery: z.boolean().default(false),
});

/** §5.1.6 + the code_delivery patch: the handoff briefing an EXECUTOR submits. */
export const HandoffBriefingSchema = z.object({
  fencing_token: z.number().int().positive(),
  output_location: z.string().min(1),
  code_delivery: z
    .object({
      branch_url: z.string().url(),
      pr_url: z.string().url().optional(),
      commit: z.string().min(7),
    })
    .optional(),
  summary: z.string().min(10),
  key_metrics: z.record(z.string(), z.union([z.string(), z.number(), z.boolean()])),
  unverified_hypotheses: z.array(z.string()),
  risk_flag: z.enum(["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]),
});

/** §5.1.7: per-acceptance-id verdicts; a FAIL must say what to rework. */
export const ReviewSubmissionSchema = z.object({
  verdicts: z
    .array(
      z.discriminatedUnion("result", [
        z.object({
          acceptance_id: z.string(),
          result: z.literal("PASS"),
          evidence_doc: ContextDocRefSchema.optional(),
        }),
        z.object({
          acceptance_id: z.string(),
          result: z.literal("FAIL"),
          rework_target: z.string().min(5, "必须写明改什么"),
          rework_standard: z.string().min(5, "必须写明改到什么标准"),
          reverify_by: z.enum(["AUDITOR", "OWNER", "AUTO_RUNNER"]),
        }),
      ]),
    )
    .min(1),
});

export type CreateTaskPayload = z.infer<typeof CreateTaskPayloadSchema>;
export type HandoffBriefing = z.infer<typeof HandoffBriefingSchema>;
export type ReviewSubmission = z.infer<typeof ReviewSubmissionSchema>;
export type ReviewVerdict = ReviewSubmission["verdicts"][number];

export const parseCreateTaskPayload = (value: unknown): CreateTaskPayload =>
  parseWith(CreateTaskPayloadSchema, value);

export const parseHandoffBriefing = (value: unknown): HandoffBriefing =>
  parseWith(HandoffBriefingSchema, value);

export const parseReviewSubmission = (value: unknown): ReviewSubmission =>
  parseWith(ReviewSubmissionSchema, value);
