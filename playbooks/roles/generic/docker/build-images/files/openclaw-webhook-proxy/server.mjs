/**
 * OpenClaw Webhook Proxy
 *
 * Receives webhook POSTs from GitHub and YouTrack, validates authentication,
 * and forwards valid payloads to the OpenClaw hooks API with bearer auth.
 *
 * Supports single-source or dual-source deployment: at least one of
 * GITHUB_HOOK_PATH or YOUTRACK_HOOK_PATH must be set. Each source's
 * env vars are only required when that source is enabled.
 *
 * Always required:
 *   OPENCLAW_HOOKS_TOKEN     — bearer token for the OpenClaw hooks API
 *   OPENCLAW_HOOKS_URL       — full URL to OpenClaw hooks endpoint
 *
 * Optional (with defaults):
 *   PORT                     — listen port (default: 3000)
 *   LOG_LEVEL                — logging level: "info" (default) or "debug"
 *   OPENCLAW_WAKE_MODE       — wake mode for OpenClaw hooks (default: "now")
 *   OPENCLAW_DELIVER         — deliver flag for OpenClaw hooks (default: "false")
 *   BATCH_WINDOW_MS          — debounce window in ms to batch related events (default: 10000)
 *
 * At least one hook source must be configured:
 *
 * GitHub (enabled when GITHUB_HOOK_PATH is set):
 *   GITHUB_HOOK_PATH         — URL path prefix for GitHub webhooks
 *   GITHUB_WEBHOOK_SECRET    — the webhook secret shared with GitHub
 *   GITHUB_GUARDRAILS_FILE   — path to file containing GitHub guardrails text
 *   GITHUB_ALLOW_USERS       — comma-separated usernames to allow (allowlist mode; mutually exclusive with GITHUB_IGNORE_USERS)
 *   GITHUB_IGNORE_USERS      — comma-separated usernames to skip (ignorelist mode; mutually exclusive with GITHUB_ALLOW_USERS)
 *
 * YouTrack (enabled when YOUTRACK_HOOK_PATH is set):
 *   YOUTRACK_HOOK_PATH       — URL path prefix for YouTrack webhooks
 *   YOUTRACK_WEBHOOK_SECRET  — shared secret for YouTrack webhook validation
 *   YOUTRACK_BASE_URL        — base URL for YouTrack instance
 *   YOUTRACK_ALLOW_USERS     — comma-separated usernames to allow (allowlist mode; mutually exclusive with YOUTRACK_IGNORE_USERS)
 *   YOUTRACK_IGNORE_USERS    — comma-separated usernames to skip (ignorelist mode; mutually exclusive with YOUTRACK_ALLOW_USERS)
 *   YOUTRACK_GUARDRAILS_FILE — path to file containing YouTrack guardrails text
 */

import { createServer } from "node:http";
import { createHmac, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

const PORT = parseInt(process.env.PORT || "3000", 10);
const LOG_LEVEL = (process.env.LOG_LEVEL || "info").toLowerCase();
const DEBUG = LOG_LEVEL === "debug";

function logDebug(...args) {
  if (DEBUG) console.log(`[${new Date().toISOString()}] [DEBUG]`, ...args);
}

// ─── Environment variable validation ────────────────────────────────

// Always required
const ALWAYS_REQUIRED = ["OPENCLAW_HOOKS_TOKEN", "OPENCLAW_HOOKS_URL"];

const missing = ALWAYS_REQUIRED.filter((key) => !process.env[key]);
if (missing.length > 0) {
  console.error(`FATAL: Missing required environment variables: ${missing.join(", ")}`);
  process.exit(1);
}

// Determine which sources are enabled
const GITHUB_ENABLED = !!process.env.GITHUB_HOOK_PATH;
const YOUTRACK_ENABLED = !!process.env.YOUTRACK_HOOK_PATH;

if (!GITHUB_ENABLED && !YOUTRACK_ENABLED) {
  console.error(
    "FATAL: At least one hook source must be configured. " +
      "Set GITHUB_HOOK_PATH and/or YOUTRACK_HOOK_PATH."
  );
  process.exit(1);
}

// Conditionally required env vars per source
const conditionalMissing = [];

if (GITHUB_ENABLED) {
  for (const key of ["GITHUB_WEBHOOK_SECRET", "GITHUB_GUARDRAILS_FILE"]) {
    if (!process.env[key]) conditionalMissing.push(key);
  }
}

if (YOUTRACK_ENABLED) {
  for (const key of [
    "YOUTRACK_WEBHOOK_SECRET",
    "YOUTRACK_BASE_URL",
    "YOUTRACK_GUARDRAILS_FILE",
  ]) {
    if (!process.env[key]) conditionalMissing.push(key);
  }
}

if (conditionalMissing.length > 0) {
  console.error(
    `FATAL: Missing required environment variables for enabled sources: ${conditionalMissing.join(", ")}`
  );
  process.exit(1);
}

const OPENCLAW_HOOKS_TOKEN = process.env.OPENCLAW_HOOKS_TOKEN;
const OPENCLAW_HOOKS_URL = process.env.OPENCLAW_HOOKS_URL;

// GitHub config (only when enabled)
const GITHUB_WEBHOOK_SECRET = process.env.GITHUB_WEBHOOK_SECRET || "";
const GITHUB_HOOK_PATH = process.env.GITHUB_HOOK_PATH || "";
const GITHUB_ALLOW_USERS = (process.env.GITHUB_ALLOW_USERS || "")
  .split(",")
  .map((u) => u.trim().toLowerCase())
  .filter(Boolean);
const GITHUB_IGNORE_USERS = (process.env.GITHUB_IGNORE_USERS || "")
  .split(",")
  .map((u) => u.trim().toLowerCase())
  .filter(Boolean);

// YouTrack config (only when enabled)
const YOUTRACK_WEBHOOK_SECRET = process.env.YOUTRACK_WEBHOOK_SECRET || "";
const YOUTRACK_BASE_URL = process.env.YOUTRACK_BASE_URL || "";
const YOUTRACK_HOOK_PATH = process.env.YOUTRACK_HOOK_PATH || "";
const YOUTRACK_ALLOW_USERS = (process.env.YOUTRACK_ALLOW_USERS || "")
  .split(",")
  .map((u) => u.trim().toLowerCase())
  .filter(Boolean);
const YOUTRACK_IGNORE_USERS = (process.env.YOUTRACK_IGNORE_USERS || "")
  .split(",")
  .map((u) => u.trim().toLowerCase())
  .filter(Boolean);

// Validate mutually exclusive allow/ignore lists
if (GITHUB_ALLOW_USERS.length > 0 && GITHUB_IGNORE_USERS.length > 0) {
  console.error(
    "FATAL: GITHUB_ALLOW_USERS and GITHUB_IGNORE_USERS are mutually exclusive. Set only one."
  );
  process.exit(1);
}
if (YOUTRACK_ALLOW_USERS.length > 0 && YOUTRACK_IGNORE_USERS.length > 0) {
  console.error(
    "FATAL: YOUTRACK_ALLOW_USERS and YOUTRACK_IGNORE_USERS are mutually exclusive. Set only one."
  );
  process.exit(1);
}

// ─── Wake/deliver configuration (optional, with defaults) ──────────

const OPENCLAW_WAKE_MODE = process.env.OPENCLAW_WAKE_MODE || "now";
const OPENCLAW_DELIVER = (process.env.OPENCLAW_DELIVER || "false").toLowerCase() === "true";
const BATCH_WINDOW_MS = parseInt(process.env.BATCH_WINDOW_MS || "10000", 10);
const MAX_BATCH_SIZE = 50;

/** Map of batchKey → { events: [{eventType, detail, formattedMessage}], timer, guardrails, source, sessionKey } */
const pendingBatches = new Map();

// ─── Ticket ID extraction ───────────────────────────────────────────

/**
 * Project keys for ticket ID extraction.
 * Set TICKET_PROJECT_KEYS env var to a comma-separated list of known project keys
 * (e.g. "AGENT,HL,HC,ESP,MQTT,ESS,TPL,LIB,FL"). If not set, falls back to a
 * generic pattern matching any uppercase 2-10 char prefix.
 */
const TICKET_PROJECT_KEYS = process.env.TICKET_PROJECT_KEYS || "";
const TICKET_KEY_GROUP = TICKET_PROJECT_KEYS
  ? `(?:${TICKET_PROJECT_KEYS.split(",").map(k => k.trim()).filter(Boolean).join("|")})`
  : "[A-Z]{2,10}";
const TICKET_ID_PATTERN = new RegExp(`\\b(${TICKET_KEY_GROUP}-\\d+)\\b`);
const BRANCH_TICKET_PATTERN = new RegExp(`(?:^|/)(${TICKET_KEY_GROUP}-\\d+)\\b`);

/**
 * Extract a YouTrack ticket ID from a GitHub event payload.
 * Checks branch name first (most reliable), then PR/issue title, then body.
 * For check_run events, also checks associated pull_requests and commit messages.
 * @returns {string|null} Ticket ID like "TPL-2" or null
 */
function extractTicketIdFromGitHub(event, payload) {
  // Try branch name — match ticket ID at the start or immediately after a "/"
  // (e.g. "AGENT-32-foo", "agent/AGENT-32-foo", "feature/AGENT-32-foo")
  const branch =
    payload.pull_request?.head?.ref ||
    (event === "push" ? (payload.ref || "").replace(/^refs\/heads\//, "") : null) ||
    (event === "check_run" ? payload.check_run?.check_suite?.head_branch : null) ||
    (event === "check_suite" ? payload.check_suite?.head_branch : null);
  if (branch) {
    const branchMatch = branch.match(BRANCH_TICKET_PATTERN);
    if (branchMatch) return branchMatch[1];
  }

  // For check_run events, try associated pull requests' head branches
  if (event === "check_run" && payload.check_run?.pull_requests) {
    for (const pr of payload.check_run.pull_requests) {
      const prBranch = pr.head?.ref;
      if (prBranch) {
        const prMatch = prBranch.match(BRANCH_TICKET_PATTERN);
        if (prMatch) return prMatch[1];
      }
    }
  }

  // For check_suite events, try associated pull requests' head branches
  if (event === "check_suite" && payload.check_suite?.pull_requests) {
    for (const pr of payload.check_suite.pull_requests) {
      const prBranch = pr.head?.ref;
      if (prBranch) {
        const prMatch = prBranch.match(BRANCH_TICKET_PATTERN);
        if (prMatch) return prMatch[1];
      }
    }
  }

  // Try PR title: "TICKET-ID: description" or "TICKET-ID description"
  const title = payload.pull_request?.title || payload.issue?.title;
  if (title) {
    const titleMatch = title.match(TICKET_ID_PATTERN);
    if (titleMatch) return titleMatch[1];
  }

  // For check_run on default branch (post-merge), try the commit message
  if (event === "check_run") {
    const commitMsg = payload.check_run?.output?.title || payload.check_run?.head_sha;
    // GitHub doesn't include the full commit message in check_run, but the
    // check_suite head_commit message may be available
    const headCommitMsg = payload.check_run?.check_suite?.head_commit?.message;
    if (headCommitMsg) {
      const commitMatch = headCommitMsg.match(TICKET_ID_PATTERN);
      if (commitMatch) return commitMatch[1];
    }
  }

  // For check_suite events, try the head commit message
  if (event === "check_suite") {
    const headCommitMsg = payload.check_suite?.head_commit?.message;
    if (headCommitMsg) {
      const commitMatch = headCommitMsg.match(TICKET_ID_PATTERN);
      if (commitMatch) return commitMatch[1];
    }
  }

  // Try PR body for "Fixes TICKET-ID" / "Closes TICKET-ID" patterns
  const body = payload.pull_request?.body || payload.issue?.body;
  if (body) {
    const fixesMatch = body.match(new RegExp(`(?:fixes|closes|resolves)\\s+(${TICKET_KEY_GROUP}-\\d+)`, "i"));
    if (fixesMatch) return fixesMatch[1];
  }

  return null;
}

/**
 * Extract a YouTrack ticket ID from a YouTrack webhook payload.
 * @returns {string|null} Ticket ID like "AGENT-32" or null
 */
function extractTicketIdFromYouTrack(payload) {
  return payload.issue?.idReadable || null;
}

// ─── Load guardrails from mounted files ─────────────────────────────

function loadGuardrails(filePath, label) {
  try {
    const content = readFileSync(filePath, "utf-8").trim();
    if (!content) {
      console.error(`FATAL: Guardrails file is empty: ${filePath} (${label})`);
      process.exit(1);
    }
    return content;
  } catch (err) {
    console.error(`FATAL: Failed to read ${label} guardrails file (${filePath}): ${err.message}`);
    process.exit(1);
  }
}

const GITHUB_GUARDRAILS = GITHUB_ENABLED
  ? loadGuardrails(process.env.GITHUB_GUARDRAILS_FILE, "GitHub")
  : "";
const YOUTRACK_GUARDRAILS = YOUTRACK_ENABLED
  ? loadGuardrails(process.env.YOUTRACK_GUARDRAILS_FILE, "YouTrack")
  : "";

// ─── Source detection ───────────────────────────────────────────────

/** Detect webhook source from request URL path (only for enabled sources) */
function detectSource(url) {
  if (YOUTRACK_ENABLED && url.startsWith(YOUTRACK_HOOK_PATH)) return "youtrack";
  if (GITHUB_ENABLED && url.startsWith(GITHUB_HOOK_PATH)) return "github";
  return null;
}

// ─── GitHub helpers ─────────────────────────────────────────────────

/** Validate GitHub HMAC-SHA256 signature */
function validateGitHubSignature(payload, signatureHeader) {
  if (!signatureHeader || !GITHUB_WEBHOOK_SECRET) return false;
  const expected = Buffer.from(
    "sha256=" +
      createHmac("sha256", GITHUB_WEBHOOK_SECRET).update(payload).digest("hex")
  );
  const actual = Buffer.from(signatureHeader);
  if (expected.length !== actual.length) return false;
  return timingSafeEqual(expected, actual);
}

/** Check if a review comment body contains a GitHub suggestion block */
function extractSuggestion(body) {
  if (!body) return null;
  const match = body.match(/```suggestion\r?\n([\s\S]*?)```/);
  return match ? match[1].trimEnd() : null;
}

/** Format GitHub event into a meaningful agent message */
function formatGitHubMessage(event, payload) {
  const repo = payload.repository?.full_name || "unknown";

  switch (event) {
    case "pull_request_review": {
      const pr = payload.pull_request;
      const review = payload.review;
      const action = payload.action; // submitted, edited, dismissed
      // Diff URL: PR html_url + /files
      const diffUrl = `${pr.html_url}/files`;
      return [
        `GitHub PR Review [${action}] on ${repo}#${pr.number}: "${pr.title}"`,
        `Reviewer: ${review.user.login}`,
        `State: ${review.state}`,
        `Action: ${action}`,
        review.body ? `Review body:\n${review.body}` : null,
        `PR URL: ${pr.html_url}`,
        `Diff URL: ${diffUrl}`,
        `Review URL: ${review.html_url}`,
      ]
        .filter(Boolean)
        .join("\n");
    }

    case "pull_request_review_comment": {
      const pr = payload.pull_request;
      const comment = payload.comment;
      const action = payload.action;
      const suggestion = extractSuggestion(comment.body);
      // diff_hunk gives the surrounding code context
      const diffHunk = comment.diff_hunk
        ? `\nCode context (diff hunk):\n${comment.diff_hunk}`
        : "";
      const replyInfo = comment.in_reply_to_id
        ? `\nIn reply to comment #${comment.in_reply_to_id}`
        : "";
      return [
        `GitHub PR Review Comment [${action}] on ${repo}#${pr.number}: "${pr.title}"`,
        `Author: ${comment.user.login}`,
        `File: ${comment.path}:${comment.line || comment.original_line || "?"}`,
        replyInfo || null,
        `Comment:\n${comment.body}`,
        suggestion ? `\nSuggested change:\n${suggestion}` : null,
        diffHunk || null,
        `PR URL: ${pr.html_url}`,
        `Comment URL: ${comment.html_url}`,
      ]
        .filter(Boolean)
        .join("\n");
    }

    case "issue_comment": {
      const issue = payload.issue;
      const comment = payload.comment;
      const action = payload.action;
      const isPR = !!issue.pull_request;
      const issueState = issue.state; // open or closed
      return [
        `GitHub ${isPR ? "PR" : "Issue"} Comment [${action}] on ${repo}#${issue.number}: "${issue.title}"`,
        `Author: ${comment.user.login}`,
        `${isPR ? "PR" : "Issue"} state: ${issueState}`,
        `Comment:\n${comment.body}`,
        `Issue URL: ${issue.html_url}`,
        `Comment URL: ${comment.html_url}`,
      ].join("\n");
    }

    case "issues": {
      const issue = payload.issue;
      const action = payload.action;
      return [
        `GitHub Issue [${action}] on ${repo}#${issue.number}: "${issue.title}"`,
        `Author: ${issue.user.login}`,
        `State: ${issue.state}`,
        issue.body ? `\nBody:\n${issue.body}` : null,
        `URL: ${issue.html_url}`,
      ]
        .filter(Boolean)
        .join("\n");
    }

    case "pull_request": {
      const pr = payload.pull_request;
      const action = payload.action;
      const lines = [
        `GitHub PR [${action}] on ${repo}#${pr.number}: "${pr.title}"`,
        `Author: ${pr.user.login}`,
        `Branch: ${pr.head.ref} → ${pr.base.ref}`,
        `State: ${pr.state}`,
      ];

      // Include full body for opened/edited PRs
      if (action === "opened" || action === "edited" || action === "reopened") {
        if (pr.body) {
          lines.push(`\nDescription:\n${pr.body}`);
        }
      }

      // For merged PRs, include merge commit
      if (pr.merged) {
        lines.push(`Merged: yes`);
        if (pr.merge_commit_sha) {
          lines.push(`Merge commit: ${pr.merge_commit_sha}`);
        }
        if (pr.merged_by) {
          lines.push(`Merged by: ${pr.merged_by.login}`);
        }
      }

      // For ready_for_review, make it clear
      if (action === "ready_for_review") {
        lines.push(`PR is now ready for review (was draft)`);
      }

      lines.push(`URL: ${pr.html_url}`);
      return lines.filter(Boolean).join("\n");
    }

    case "check_run": {
      const checkRun = payload.check_run;
      const action = payload.action; // created, completed, rerequested, requested_action
      const conclusion = checkRun.conclusion; // success, failure, neutral, cancelled, timed_out, action_required, skipped, stale, null
      const status = checkRun.status; // queued, in_progress, completed
      const checkName = checkRun.name;
      const headSha = checkRun.head_sha?.slice(0, 7) || "unknown";
      const branch = checkRun.check_suite?.head_branch || "unknown";
      const htmlUrl = checkRun.html_url;

      // Identify associated PRs
      const associatedPRs = (checkRun.pull_requests || [])
        .map((pr) => `#${pr.number} (${pr.head?.ref || "?"})`)
        .join(", ");

      // Determine if this is a default branch build (post-merge)
      const defaultBranch = payload.repository?.default_branch || "main";
      const isDefaultBranch = branch === defaultBranch;

      const lines = [
        `GitHub Check Run [${action}] on ${repo}: "${checkName}"`,
        `Status: ${status}${conclusion ? ` / ${conclusion}` : ""}`,
        `Branch: ${branch}${isDefaultBranch ? " (default)" : ""}`,
        `Commit: ${headSha}`,
      ];

      if (associatedPRs) {
        lines.push(`Associated PRs: ${associatedPRs}`);
      }

      // Include head commit message for context (especially useful for default branch)
      const headCommitMsg = checkRun.check_suite?.head_commit?.message;
      if (headCommitMsg) {
        lines.push(`Commit message: ${headCommitMsg.split("\n")[0]}`);
      }

      // Include output summary/text if available (contains failure details)
      if (checkRun.output) {
        if (checkRun.output.title && checkRun.output.title !== checkName) {
          lines.push(`Output title: ${checkRun.output.title}`);
        }
        if (checkRun.output.summary) {
          lines.push(`Output summary:\n${checkRun.output.summary}`);
        }
        // output.text can be very long; truncate for the message
        if (checkRun.output.text) {
          const text = checkRun.output.text;
          lines.push(`Output text:\n${text.length > 2000 ? text.slice(0, 2000) + "\n... (truncated)" : text}`);
        }
      }

      if (htmlUrl) {
        lines.push(`URL: ${htmlUrl}`);
      }

      return lines.filter(Boolean).join("\n");
    }

    case "check_suite": {
      const checkSuite = payload.check_suite;
      const action = payload.action; // completed, requested, rerequested
      const conclusion = checkSuite.conclusion; // success, failure, neutral, cancelled, timed_out, action_required, stale, startup_failure, null
      const status = checkSuite.status; // queued, in_progress, completed
      const headSha = checkSuite.head_sha?.slice(0, 7) || "unknown";
      const branch = checkSuite.head_branch || "unknown";
      const htmlUrl = checkSuite.url;
      const app = checkSuite.app?.name || "unknown";

      // Identify associated PRs
      const associatedPRs = (checkSuite.pull_requests || [])
        .map((pr) => `#${pr.number} (${pr.head?.ref || "?"})`)
        .join(", ");

      // Determine if this is a default branch build (post-merge)
      const defaultBranch = payload.repository?.default_branch || "main";
      const isDefaultBranch = branch === defaultBranch;

      const lines = [
        `GitHub Check Suite [${action}] on ${repo}: "${app}"`,
        `Status: ${status}${conclusion ? ` / ${conclusion}` : ""}`,
        `Branch: ${branch}${isDefaultBranch ? " (default)" : ""}`,
        `Commit: ${headSha}`,
      ];

      if (associatedPRs) {
        lines.push(`Associated PRs: ${associatedPRs}`);
      }

      // Include head commit message for context
      const headCommitMsg = checkSuite.head_commit?.message;
      if (headCommitMsg) {
        lines.push(`Commit message: ${headCommitMsg.split("\n")[0]}`);
      }

      if (htmlUrl) {
        lines.push(`URL: ${htmlUrl}`);
      }

      return lines.filter(Boolean).join("\n");
    }

    case "push": {
      const ref = payload.ref || "";
      // Extract branch name from refs/heads/branch-name
      const branch = ref.startsWith("refs/heads/")
        ? ref.slice("refs/heads/".length)
        : ref;
      const pusher = payload.pusher?.name || "unknown";
      const commits = payload.commits || [];
      const commitCount = commits.length;
      const forced = payload.forced ? " (force push)" : "";

      // Summarize commits (up to 10)
      const commitLines = commits.slice(0, 10).map((c) => {
        const shortSha = c.id.slice(0, 7);
        const msg = c.message.split("\n")[0]; // first line only
        return `  ${shortSha} ${msg}`;
      });
      if (commits.length > 10) {
        commitLines.push(`  ... and ${commits.length - 10} more commits`);
      }

      // Files changed summary across all commits
      const allAdded = new Set();
      const allModified = new Set();
      const allRemoved = new Set();
      for (const c of commits) {
        (c.added || []).forEach((f) => allAdded.add(f));
        (c.modified || []).forEach((f) => allModified.add(f));
        (c.removed || []).forEach((f) => allRemoved.add(f));
      }
      const filesSummary = [];
      if (allAdded.size > 0) filesSummary.push(`+${allAdded.size} added`);
      if (allModified.size > 0) filesSummary.push(`~${allModified.size} modified`);
      if (allRemoved.size > 0) filesSummary.push(`-${allRemoved.size} removed`);

      const lines = [
        `GitHub Push to ${repo} (branch: ${branch})${forced}`,
        `Pusher: ${pusher}`,
        `Commits: ${commitCount}`,
      ];
      if (commitLines.length > 0) {
        lines.push(`\nCommit messages:\n${commitLines.join("\n")}`);
      }
      if (filesSummary.length > 0) {
        lines.push(`\nFiles changed: ${filesSummary.join(", ")}`);
      }
      if (payload.compare) {
        lines.push(`Compare: ${payload.compare}`);
      }
      return lines.filter(Boolean).join("\n");
    }

    default:
      return `GitHub event "${event}" on ${repo}: ${JSON.stringify(payload).slice(0, 500)}`;
  }
}

// ─── YouTrack helpers ───────────────────────────────────────────────

/** Validate YouTrack webhook shared secret */
function validateYouTrackSecret(req) {
  if (!YOUTRACK_WEBHOOK_SECRET) return false;
  const provided = req.headers["x-youtrack-token"];
  if (!provided) return false;
  const expected = Buffer.from(YOUTRACK_WEBHOOK_SECRET);
  const actual = Buffer.from(provided);
  if (expected.length !== actual.length) return false;
  return timingSafeEqual(expected, actual);
}

/**
 * Extract readable field changes from a YouTrack webhook payload.
 * YouTrack sends an array of changed fields in `updatedFields` or
 * individual `oldValue`/`newValue` pairs.
 */
function formatFieldChanges(fields) {
  if (!fields || !Array.isArray(fields) || fields.length === 0) return null;
  return fields
    .map((f) => {
      const name = f.name || f.id || "unknown field";
      const oldVal = extractFieldValue(f.oldValue);
      const newVal = extractFieldValue(f.newValue);
      if (oldVal && newVal) return `  ${name}: ${oldVal} → ${newVal}`;
      if (newVal) return `  ${name}: → ${newVal}`;
      if (oldVal) return `  ${name}: ${oldVal} → (cleared)`;
      return `  ${name}: changed`;
    })
    .join("\n");
}

/** Extract a human-readable value from a YouTrack field value object */
function extractFieldValue(val) {
  if (val == null) return null;
  if (typeof val === "string") return val;
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  if (Array.isArray(val)) {
    return val.map((v) => extractFieldValue(v)).filter(Boolean).join(", ");
  }
  // YouTrack field values are objects with name/login/text/presentation
  return val.name || val.login || val.text || val.presentation || val.id || JSON.stringify(val);
}

/** Build a YouTrack issue URL from the issue ID */
function youtrackIssueUrl(issueId) {
  return `${YOUTRACK_BASE_URL}/issue/${issueId}`;
}

/**
 * Format a YouTrack webhook payload into a meaningful agent message.
 *
 * YouTrack webhook payloads vary by event type. Common structure:
 * - Issue events: { issue: { id, idReadable, summary, ... }, updatedFields: [...] }
 * - Comment events: { issue: { ... }, comment: { text, author: { login, name }, ... } }
 * - The top-level may also include: action, timestamp, updater
 */
function formatYouTrackMessage(payload) {
  const issue = payload.issue;
  const issueId = issue?.idReadable || issue?.id || "unknown";
  const summary = issue?.summary || "(no summary)";
  const action = payload.action || "updated";
  const updater =
    payload.updater?.name ||
    payload.updater?.login ||
    payload.author?.name ||
    payload.author?.login ||
    "unknown";

  // Comment added/updated/removed
  if (payload.comment) {
    const comment = payload.comment;
    const commentAuthor =
      comment.author?.name || comment.author?.login || updater;
    const commentText = comment.text || "(empty comment)";
    const deleted = comment.deleted ? " [DELETED]" : "";
    return [
      `YouTrack Comment${deleted} on ${issueId}: "${summary}"`,
      `Author: ${commentAuthor}`,
      `Comment:\n${commentText}`,
      `URL: ${youtrackIssueUrl(issueId)}`,
    ].join("\n");
  }

  // Field changes (state transitions, assignments, etc.)
  const fields = payload.updatedFields || payload.changedFields;
  const fieldChanges = formatFieldChanges(fields);

  // Detect specific high-value events
  let eventType = `Issue [${action}]`;

  if (fields && Array.isArray(fields)) {
    for (const f of fields) {
      const fname = (f.name || f.id || "").toLowerCase();
      const newVal = extractFieldValue(f.newValue);

      // State change detection
      if (fname === "state" || fname === "status") {
        const oldVal = extractFieldValue(f.oldValue);
        if (newVal) {
          eventType = `Issue State Change`;
          // Check for key transitions
          const newLower = newVal.toLowerCase();
          if (newLower === "ready" || newLower === "open") {
            eventType = `Issue Moved to Ready`;
          } else if (
            newLower === "in progress" ||
            newLower === "in review"
          ) {
            eventType = `Issue Moved to ${newVal}`;
          } else if (newLower === "done" || newLower === "resolved" || newLower === "closed") {
            eventType = `Issue Resolved`;
          }
        }
      }

      // Assignment detection
      if (fname === "assignee" || fname === "assignees") {
        eventType = `Issue Assigned`;
      }
    }
  }

  const lines = [
    `YouTrack ${eventType} — ${issueId}: "${summary}"`,
    `Updated by: ${updater}`,
  ];

  if (fieldChanges) {
    lines.push(`\nField changes:\n${fieldChanges}`);
  }

  // Include description for new issues
  if (action === "created" && issue?.description) {
    lines.push(`\nDescription:\n${issue.description}`);
  }

  lines.push(`URL: ${youtrackIssueUrl(issueId)}`);
  return lines.filter(Boolean).join("\n");
}

// ─── Batching helpers ────────────────────────────────────────────────

/** Return the batch key for a GitHub event, or null to forward immediately */
function getGitHubBatchKey(event, payload) {
  const repo = payload.repository?.full_name || "unknown";
  switch (event) {
    case "pull_request_review":
    case "pull_request_review_comment":
    case "pull_request":
      return `github:${repo}:${payload.pull_request?.number}`;
    case "issue_comment":
    case "issues":
      return `github:${repo}:${payload.issue?.number}`;
    case "check_run":
      // No batching for check_run — forward immediately so the agent can
      // react to CI failures as fast as possible
      return null;
    case "check_suite":
      return null;
    default:
      return null; // push and unknown events: forward immediately
  }
}

/** Return true if this event should flush its batch right away */
function isImmediateFlushEvent(event, payload) {
  // Only flush immediately on approval — changes_requested reviews are
  // typically accompanied by review comments that arrive as separate webhook
  // events shortly after, so we let the batch window collect them together.
  if (event === "pull_request_review") {
    const state = (payload.review?.state || "").toLowerCase();
    return state === "approved";
  }
  return false;
}

/** Return a human-readable { eventType, detail } for batch event headers */
function getGitHubEventDetail(event, payload) {
  const repo = payload.repository?.full_name || "unknown";
  switch (event) {
    case "pull_request_review":
      return { eventType: event, detail: `${repo}#${payload.pull_request?.number} state=${payload.review?.state}` };
    case "pull_request_review_comment":
      return { eventType: event, detail: `${repo}#${payload.pull_request?.number}` };
    case "pull_request":
      return { eventType: event, detail: `${repo}#${payload.pull_request?.number} action=${payload.action}` };
    case "issue_comment":
      return { eventType: event, detail: `${repo}#${payload.issue?.number}` };
    case "issues":
      return { eventType: event, detail: `${repo}#${payload.issue?.number} action=${payload.action}` };
    default:
      return { eventType: event, detail: repo };
  }
}

/** Return a human-readable { eventType, detail } for a YouTrack batch event header */
function getYouTrackEventDetail(payload) {
  const issueId = payload.issue?.idReadable || payload.issue?.id || "unknown";
  const action = payload.action || "event";
  return { eventType: action, detail: issueId };
}

/** Add an event entry to a pending batch, starting the debounce timer if needed */
function addToBatch(key, eventEntry, guardrails, source, sessionKey = null) {
  if (pendingBatches.has(key)) {
    const batch = pendingBatches.get(key);
    batch.events.push(eventEntry);
    // First non-null sessionKey wins (batch events should share the same ticket)
    if (sessionKey && !batch.sessionKey) batch.sessionKey = sessionKey;
    logDebug(`Added event to batch ${key} (${batch.events.length} total)`);
    if (batch.events.length >= MAX_BATCH_SIZE) {
      logDebug(`Batch ${key} reached max size (${MAX_BATCH_SIZE}), flushing immediately`);
      flushBatch(key);
    }
  } else {
    const timer = setTimeout(() => {
      logDebug(`Batch window expired for ${key}, flushing`);
      flushBatch(key);
    }, BATCH_WINDOW_MS);
    pendingBatches.set(key, { events: [eventEntry], timer, guardrails, source, sessionKey });
    logDebug(`Started new batch for ${key}${sessionKey ? ` (session: ${sessionKey})` : ""}`);
  }
}

/** Build the combined message for a batch and forward it to OpenClaw */
async function flushBatch(key) {
  const batch = pendingBatches.get(key);
  if (!batch) return;
  clearTimeout(batch.timer);
  pendingBatches.delete(key);

  const { events, guardrails, source, sessionKey } = batch;
  if (events.length === 0) return;

  let message;
  if (events.length === 1) {
    message = `${guardrails}\n${events[0].formattedMessage}`;
  } else {
    const parts = events.map(
      (e, i) =>
        `--- Event ${i + 1} of ${events.length}: ${e.eventType} (${e.detail}) ---\n${e.formattedMessage}`
    );
    message = `${guardrails}\n\n${parts.join("\n\n")}`;
  }

  logDebug(`Flushing batch ${key} with ${events.length} event(s)${sessionKey ? ` → session ${sessionKey}` : ""}`);
  await forwardToOpenClaw(message, source, sessionKey);
}

/** Flush all pending batches — called on graceful shutdown */
async function flushAllBatches() {
  const keys = [...pendingBatches.keys()];
  if (keys.length > 0) {
    console.log(`[${new Date().toISOString()}] Flushing ${keys.length} pending batch(es) before shutdown`);
    await Promise.all(keys.map((k) => flushBatch(k)));
  }
}

// ─── User filtering ─────────────────────────────────────────────────

/**
 * Determine whether an event from the given actor should be processed.
 * @param {string|null|undefined} actor - The username/login of the event actor
 * @param {string[]} allowList - If non-empty, only process actors in this list
 * @param {string[]} ignoreList - If non-empty, skip actors in this list
 * @returns {{ process: boolean, reason: string }}
 */
function shouldProcessUser(actor, allowList, ignoreList) {
  if (!actor) return { process: true, reason: "no actor" };
  const actorLower = actor.toLowerCase();
  if (allowList.length > 0) {
    if (!allowList.includes(actorLower)) {
      return { process: false, reason: "not in allowlist" };
    }
    return { process: true, reason: "in allowlist" };
  }
  if (ignoreList.length > 0 && ignoreList.includes(actorLower)) {
    return { process: false, reason: "ignored user" };
  }
  return { process: true, reason: "no filter" };
}

// ─── Request handler ────────────────────────────────────────────────

const server = createServer(async (req, res) => {
  // Health check
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
    return;
  }

  // Only accept POST
  if (req.method !== "POST") {
    res.writeHead(405);
    res.end("Method not allowed");
    return;
  }

  const source = detectSource(req.url);
  if (!source) {
    res.writeHead(404);
    res.end("Unknown webhook path");
    return;
  }

  // Read body
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks);

  // ── GitHub path ──
  if (source === "github") {
    // Validate GitHub signature
    const signature = req.headers["x-hub-signature-256"];
    if (!validateGitHubSignature(body, signature)) {
      console.error(
        `[${new Date().toISOString()}] REJECTED GitHub: invalid signature from ${req.socket.remoteAddress}`
      );
      res.writeHead(403);
      res.end("Invalid signature");
      return;
    }

    let payload;
    try {
      payload = JSON.parse(body.toString());
    } catch {
      res.writeHead(400);
      res.end("Invalid JSON");
      return;
    }

    const event = req.headers["x-github-event"];
    const delivery = req.headers["x-github-delivery"];

    logDebug(`GitHub inbound payload:\n${JSON.stringify(payload, null, 2)}`);

    // Skip ping events
    if (event === "ping") {
      console.log(
        `[${new Date().toISOString()}] GitHub ping received: ${payload.zen}`
      );
      res.writeHead(200);
      res.end("pong");
      return;
    }

    const repo = payload.repository?.full_name || "unknown";

    console.log(
      `[${new Date().toISOString()}] GitHub ${event} on ${repo} (delivery: ${delivery})`
    );

    // Filter check_run events: only forward failures/errors, skip success/neutral/skipped
    if (event === "check_run") {
      const action = payload.action;
      const conclusion = payload.check_run?.conclusion;
      // Only care about completed check runs that failed
      if (action === "completed") {
        const dominated = ["success", "neutral", "skipped"];
        if (dominated.includes(conclusion)) {
          logDebug(`Skipping check_run with conclusion=${conclusion} (not a failure)`);
          res.writeHead(200);
          res.end(`OK (skipped: check_run ${conclusion})`);
          return;
        }
      } else if (action === "created" || action === "rerequested") {
        // Skip non-completed check runs (queued, in_progress)
        logDebug(`Skipping check_run action=${action} (not completed)`);
        res.writeHead(200);
        res.end(`OK (skipped: check_run ${action})`);
        return;
      }
    }

    // Filter check_suite events: only forward failures, skip success/neutral/skipped
    if (event === "check_suite") {
      const action = payload.action;
      const conclusion = payload.check_suite?.conclusion;
      if (action === "completed") {
        const dominated = ["success", "neutral", "skipped"];
        if (dominated.includes(conclusion)) {
          logDebug(`Skipping check_suite with conclusion=${conclusion} (not a failure)`);
          res.writeHead(200);
          res.end(`OK (skipped: check_suite ${conclusion})`);
          return;
        }
      } else if (action === "requested") {
        // Skip requested (initial trigger when code is pushed)
        logDebug(`Skipping check_suite action=${action} (not completed)`);
        res.writeHead(200);
        res.end(`OK (skipped: check_suite ${action})`);
        return;
      }
    }

    // Filter events by user (allowlist or ignorelist)
    // Note: check_run events bypass user filtering — they are system events
    // triggered by CI, not by a specific user action
    let actor = null;
    switch (event) {
      case "pull_request_review":
        actor = payload.review?.user?.login;
        break;
      case "pull_request_review_comment":
      case "issue_comment":
        actor = payload.comment?.user?.login;
        break;
      case "issues":
      case "pull_request":
        actor = payload.sender?.login;
        break;
      case "push":
        actor = payload.pusher?.name || payload.sender?.login;
        break;
      case "check_run":
        // Check runs are system events; skip user filtering
        actor = null;
        break;
      case "check_suite":
        // Check suites are system events; skip user filtering
        actor = null;
        break;
      default:
        actor = payload.sender?.login;
        break;
    }
    const { process: processGitHub, reason: githubReason } = shouldProcessUser(
      actor,
      GITHUB_ALLOW_USERS,
      GITHUB_IGNORE_USERS
    );
    if (!processGitHub) {
      console.log(
        `[${new Date().toISOString()}] Skipping GitHub event from ${actor || "(no actor)"} (${githubReason})`
      );
      res.writeHead(200);
      res.end(`OK (skipped: ${githubReason})`);
      return;
    }

    const eventMessage = formatGitHubMessage(event, payload);
    const batchKey = getGitHubBatchKey(event, payload);

    // Extract ticket ID for session routing
    const ticketId = extractTicketIdFromGitHub(event, payload);
    const sessionKey = ticketId ? `ticket:${ticketId}` : null;
    if (ticketId) {
      logDebug(`GitHub ${event}: extracted ticket ID ${ticketId} → session ${sessionKey}`);
    }

    if (!batchKey) {
      // No batch key (e.g. push) — forward immediately
      logDebug(`GitHub ${event}: no batch key, forwarding immediately`);
      const message = `${GITHUB_GUARDRAILS}\n${eventMessage}`;
      res.writeHead(200);
      res.end("OK");
      forwardToOpenClaw(message, "GitHub", sessionKey);
      return;
    }

    const { eventType, detail } = getGitHubEventDetail(event, payload);
    const eventEntry = { eventType, detail, formattedMessage: eventMessage };

    if (isImmediateFlushEvent(event, payload)) {
      logDebug(`GitHub ${event} (${detail}): immediate flush trigger`);
      addToBatch(batchKey, eventEntry, GITHUB_GUARDRAILS, "GitHub", sessionKey);
      res.writeHead(200);
      res.end("OK");
      flushBatch(batchKey);
      return;
    }

    addToBatch(batchKey, eventEntry, GITHUB_GUARDRAILS, "GitHub", sessionKey);
    res.writeHead(200);
    res.end("OK");
    return;
  }

  // ── YouTrack path ──
  if (source === "youtrack") {
    // Validate YouTrack shared secret
    if (!validateYouTrackSecret(req)) {
      console.error(
        `[${new Date().toISOString()}] REJECTED YouTrack: invalid token from ${req.socket.remoteAddress}`
      );
      res.writeHead(403);
      res.end("Invalid token");
      return;
    }

    let payload;
    try {
      payload = JSON.parse(body.toString());
    } catch {
      res.writeHead(400);
      res.end("Invalid JSON");
      return;
    }

    logDebug(`YouTrack inbound payload:\n${JSON.stringify(payload, null, 2)}`);

    const issueId =
      payload.issue?.idReadable || payload.issue?.id || "unknown";
    const action = payload.action || "event";

    console.log(
      `[${new Date().toISOString()}] YouTrack ${action} on ${issueId}`
    );

    // Filter events by user (allowlist or ignorelist)
    const updaterLogin =
      payload.updater?.login ||
      payload.author?.login ||
      payload.comment?.author?.login ||
      null;
    const { process: processYouTrack, reason: youtrackReason } = shouldProcessUser(
      updaterLogin,
      YOUTRACK_ALLOW_USERS,
      YOUTRACK_IGNORE_USERS
    );
    if (!processYouTrack) {
      console.log(
        `[${new Date().toISOString()}] Skipping YouTrack event from ${updaterLogin || "(no actor)"} (${youtrackReason})`
      );
      res.writeHead(200);
      res.end(`OK (skipped: ${youtrackReason})`);
      return;
    }

    const eventMessage = formatYouTrackMessage(payload);
    const batchKey = `youtrack:${issueId}`;
    const { eventType, detail } = getYouTrackEventDetail(payload);

    // Extract ticket ID for session routing
    const ticketId = extractTicketIdFromYouTrack(payload);
    const sessionKey = ticketId ? `ticket:${ticketId}` : null;
    if (ticketId) {
      logDebug(`YouTrack ${action}: extracted ticket ID ${ticketId} → session ${sessionKey}`);
    }

    addToBatch(batchKey, { eventType, detail, formattedMessage: eventMessage }, YOUTRACK_GUARDRAILS, "YouTrack", sessionKey);
    res.writeHead(200);
    res.end("OK");
    return;
  }
});

/** Forward a formatted message to the OpenClaw hooks API */
async function forwardToOpenClaw(message, sourceName, sessionKey = null) {
  try {
    const hookBody = {
      message,
      name: sourceName,
      wakeMode: OPENCLAW_WAKE_MODE,
      deliver: OPENCLAW_DELIVER,
    };
    if (sessionKey) {
      hookBody.sessionKey = sessionKey;
    }
    const hookPayload = JSON.stringify(hookBody);

    logDebug(`Forwarding ${sourceName} message to OpenClaw:\n${message}`);

    const response = await fetch(OPENCLAW_HOOKS_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${OPENCLAW_HOOKS_TOKEN}`,
      },
      body: hookPayload,
    });

    if (!response.ok) {
      const text = await response.text();
      console.error(
        `[${new Date().toISOString()}] OpenClaw hooks returned ${response.status}: ${text}`
      );
      return;
    }

    console.log(
      `[${new Date().toISOString()}] Forwarded ${sourceName} event to OpenClaw hooks`
    );
  } catch (err) {
    console.error(
      `[${new Date().toISOString()}] Failed to forward to OpenClaw: ${err.message}`
    );
  }
}

async function shutdown(signal) {
  console.log(`[${new Date().toISOString()}] Received ${signal}, shutting down`);
  await flushAllBatches();
  server.close(() => {
    console.log(`[${new Date().toISOString()}] Server closed`);
    process.exit(0);
  });
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

server.listen(PORT, () => {
  const sources = [];
  if (GITHUB_ENABLED) sources.push(`GitHub (${GITHUB_HOOK_PATH})`);
  if (YOUTRACK_ENABLED) sources.push(`YouTrack (${YOUTRACK_HOOK_PATH})`);
  console.log(`OpenClaw webhook proxy listening on port ${PORT}`);
  console.log(`Forwarding to: ${OPENCLAW_HOOKS_URL}`);
  console.log(`Active sources: ${sources.join(", ")}`);
  console.log(`Log level: ${LOG_LEVEL}`);
});
