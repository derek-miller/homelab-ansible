/**
 * OpenClaw Webhook Proxy
 *
 * Receives GitHub webhook POSTs, validates HMAC-SHA256 signatures,
 * and forwards valid payloads to the OpenClaw hooks API with bearer auth.
 *
 * Environment variables:
 *   GITHUB_WEBHOOK_SECRET  — the webhook secret shared with GitHub
 *   OPENCLAW_HOOKS_TOKEN   — bearer token for the OpenClaw hooks API
 *   OPENCLAW_HOOKS_URL     — full URL to OpenClaw hooks endpoint
 *                            (default: http://openclaw:18789/hooks/agent)
 *   PORT                   — listen port (default: 3000)
 */

import { createServer } from "node:http";
import { createHmac, timingSafeEqual } from "node:crypto";

const PORT = parseInt(process.env.PORT || "3000", 10);
const GITHUB_WEBHOOK_SECRET = process.env.GITHUB_WEBHOOK_SECRET;
const OPENCLAW_HOOKS_TOKEN = process.env.OPENCLAW_HOOKS_TOKEN;
const OPENCLAW_HOOKS_URL =
  process.env.OPENCLAW_HOOKS_URL || "http://openclaw:18789/hooks/agent";

if (!GITHUB_WEBHOOK_SECRET || !OPENCLAW_HOOKS_TOKEN) {
  console.error(
    "FATAL: GITHUB_WEBHOOK_SECRET and OPENCLAW_HOOKS_TOKEN are required"
  );
  process.exit(1);
}

/** Validate GitHub HMAC-SHA256 signature */
function validateSignature(payload, signatureHeader) {
  if (!signatureHeader) return false;
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
function formatMessage(event, payload) {
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
      // GitHub doesn't provide in_reply_to for issue comments in the webhook
      // but we can note the issue/PR state
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

  // Read body
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks);

  // Validate GitHub signature
  const signature = req.headers["x-hub-signature-256"];
  if (!validateSignature(body, signature)) {
    console.error(
      `[${new Date().toISOString()}] REJECTED: invalid signature from ${req.socket.remoteAddress}`
    );
    res.writeHead(403);
    res.end("Invalid signature");
    return;
  }

  // Parse payload
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

  // Skip ping events
  if (event === "ping") {
    console.log(`[${new Date().toISOString()}] Ping received: ${payload.zen}`);
    res.writeHead(200);
    res.end("pong");
    return;
  }

  const eventMessage = formatMessage(event, payload);
  const repo = payload.repository?.full_name || "unknown";

  console.log(
    `[${new Date().toISOString()}] ${event} on ${repo} (delivery: ${delivery})`
  );

  // Prepend guardrails to every message
  const guardrails = [
    "## GitHub Event Guardrails (MANDATORY)",
    "- NEVER merge a PR without an explicit approval review from Derek",
    "- NEVER push directly to main/master",
    "- NEVER open PRs on repos outside derek-miller / finitelabs orgs",
    "- Read YouTrack KB articles (Guardrails, Workflow Guide) before acting",
    "- If unsure about any action, do NOT act — respond with a comment asking for clarification",
    "---",
  ].join("\n");

  const message = `${guardrails}\n${eventMessage}`;

  // Forward to OpenClaw hooks API
  try {
    const hookPayload = JSON.stringify({
      message,
      name: "GitHub",
      wakeMode: "now",
      deliver: false,
    });

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
      `[${new Date().toISOString()}] Forwarded ${event} to OpenClaw hooks`
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
});

server.listen(PORT, () => {
  console.log(`OpenClaw webhook proxy listening on port ${PORT}`);
  console.log(`Forwarding to: ${OPENCLAW_HOOKS_URL}`);
});
