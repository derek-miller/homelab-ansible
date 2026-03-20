/**
 * OpenClaw Webhook Proxy
 *
 * Receives webhook POSTs from GitHub and YouTrack, validates authentication,
 * and forwards valid payloads to the OpenClaw hooks API with bearer auth.
 *
 * All configuration is via environment variables (all required, no defaults):
 *   GITHUB_WEBHOOK_SECRET    — the webhook secret shared with GitHub
 *   YOUTRACK_WEBHOOK_SECRET  — shared secret for YouTrack webhook validation
 *   OPENCLAW_HOOKS_TOKEN     — bearer token for the OpenClaw hooks API
 *   OPENCLAW_HOOKS_URL       — full URL to OpenClaw hooks endpoint
 *   YOUTRACK_BASE_URL        — base URL for YouTrack instance
 *   YOUTRACK_SELF_USER       — YouTrack login to skip for self-event filtering
 *   GITHUB_HOOK_PATH         — URL path prefix for GitHub webhooks
 *   YOUTRACK_HOOK_PATH       — URL path prefix for YouTrack webhooks
 *   GITHUB_GUARDRAILS_FILE   — path to file containing GitHub guardrails text
 *   YOUTRACK_GUARDRAILS_FILE — path to file containing YouTrack guardrails text
 *   OPENCLAW_WAKE_MODE       — wake mode for OpenClaw hooks (default: "now")
 *   OPENCLAW_DELIVER         — deliver flag for OpenClaw hooks (default: "false")
 *   LOG_LEVEL                — logging level: "info" (default) or "debug"
 *                              debug logs full inbound payloads and forwarded messages
 *   PORT                     — listen port (default: 3000)
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

// ─── Required environment variables ─────────────────────────────────

const REQUIRED_ENV = [
  "GITHUB_WEBHOOK_SECRET",
  "YOUTRACK_WEBHOOK_SECRET",
  "OPENCLAW_HOOKS_TOKEN",
  "OPENCLAW_HOOKS_URL",
  "YOUTRACK_BASE_URL",
  "YOUTRACK_SELF_USER",
  "GITHUB_HOOK_PATH",
  "YOUTRACK_HOOK_PATH",
  "GITHUB_GUARDRAILS_FILE",
  "YOUTRACK_GUARDRAILS_FILE",
];

const missing = REQUIRED_ENV.filter((key) => !process.env[key]);
if (missing.length > 0) {
  console.error(`FATAL: Missing required environment variables: ${missing.join(", ")}`);
  process.exit(1);
}

const GITHUB_WEBHOOK_SECRET = process.env.GITHUB_WEBHOOK_SECRET;
const YOUTRACK_WEBHOOK_SECRET = process.env.YOUTRACK_WEBHOOK_SECRET;
const OPENCLAW_HOOKS_TOKEN = process.env.OPENCLAW_HOOKS_TOKEN;
const OPENCLAW_HOOKS_URL = process.env.OPENCLAW_HOOKS_URL;
const YOUTRACK_BASE_URL = process.env.YOUTRACK_BASE_URL;
const YOUTRACK_SELF_USER = process.env.YOUTRACK_SELF_USER;
const GITHUB_HOOK_PATH = process.env.GITHUB_HOOK_PATH;
const YOUTRACK_HOOK_PATH = process.env.YOUTRACK_HOOK_PATH;

// ─── Wake/deliver configuration (optional, with defaults) ──────────

const OPENCLAW_WAKE_MODE = process.env.OPENCLAW_WAKE_MODE || "now";
const OPENCLAW_DELIVER = (process.env.OPENCLAW_DELIVER || "false").toLowerCase() === "true";

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

const GITHUB_GUARDRAILS = loadGuardrails(process.env.GITHUB_GUARDRAILS_FILE, "GitHub");
const YOUTRACK_GUARDRAILS = loadGuardrails(process.env.YOUTRACK_GUARDRAILS_FILE, "YouTrack");

// ─── Source detection ───────────────────────────────────────────────

/** Detect webhook source from request URL path */
function detectSource(url) {
  if (url.startsWith(YOUTRACK_HOOK_PATH)) return "youtrack";
  if (url.startsWith(GITHUB_HOOK_PATH)) return "github";
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

    const eventMessage = formatGitHubMessage(event, payload);
    const repo = payload.repository?.full_name || "unknown";

    console.log(
      `[${new Date().toISOString()}] GitHub ${event} on ${repo} (delivery: ${delivery})`
    );

    const message = `${GITHUB_GUARDRAILS}\n${eventMessage}`;
    await forwardToOpenClaw(message, "GitHub", res);
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

    // Skip events from the OpenClaw user to avoid feedback loops
    const updaterLogin =
      payload.updater?.login ||
      payload.author?.login ||
      payload.comment?.author?.login ||
      "";
    if (updaterLogin === YOUTRACK_SELF_USER) {
      console.log(
        `[${new Date().toISOString()}] Skipping YouTrack event from ${YOUTRACK_SELF_USER} (self)`
      );
      res.writeHead(200);
      res.end("OK (skipped self)");
      return;
    }

    const eventMessage = formatYouTrackMessage(payload);
    const message = `${YOUTRACK_GUARDRAILS}\n${eventMessage}`;
    await forwardToOpenClaw(message, "YouTrack", res);
    return;
  }
});

/** Forward a formatted message to the OpenClaw hooks API */
async function forwardToOpenClaw(message, sourceName, res) {
  try {
    const hookPayload = JSON.stringify({
      message,
      name: sourceName,
      wakeMode: OPENCLAW_WAKE_MODE,
      deliver: OPENCLAW_DELIVER,
    });

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
      res.writeHead(502);
      res.end("Upstream error");
      return;
    }

    console.log(
      `[${new Date().toISOString()}] Forwarded ${sourceName} event to OpenClaw hooks`
    );
    res.writeHead(200);
    res.end("OK");
  } catch (err) {
    console.error(
      `[${new Date().toISOString()}] Failed to forward to OpenClaw: ${err.message}`
    );
    res.writeHead(502);
    res.end("Upstream error");
  }
}

server.listen(PORT, () => {
  console.log(`OpenClaw webhook proxy listening on port ${PORT}`);
  console.log(`Forwarding to: ${OPENCLAW_HOOKS_URL}`);
  console.log(`Sources: GitHub (${GITHUB_HOOK_PATH}), YouTrack (${YOUTRACK_HOOK_PATH})`);
  console.log(`Log level: ${LOG_LEVEL}`);
});
