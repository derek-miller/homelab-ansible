FROM node:22-alpine

WORKDIR /app

# Embed the proxy server inline to avoid COPY issues with ansible build role
RUN cat > server.mjs << 'SERVEREOF'
import { createServer } from "node:http";
import { createHmac, timingSafeEqual } from "node:crypto";

const PORT = parseInt(process.env.PORT || "3000", 10);
const GITHUB_WEBHOOK_SECRET = process.env.GITHUB_WEBHOOK_SECRET;
const OPENCLAW_HOOKS_TOKEN = process.env.OPENCLAW_HOOKS_TOKEN;
const OPENCLAW_HOOKS_URL = process.env.OPENCLAW_HOOKS_URL || "http://openclaw:18789/hooks/agent";

if (!GITHUB_WEBHOOK_SECRET || !OPENCLAW_HOOKS_TOKEN) {
  console.error("FATAL: GITHUB_WEBHOOK_SECRET and OPENCLAW_HOOKS_TOKEN are required");
  process.exit(1);
}

function validateSignature(payload, signatureHeader) {
  if (!signatureHeader) return false;
  const expected = Buffer.from("sha256=" + createHmac("sha256", GITHUB_WEBHOOK_SECRET).update(payload).digest("hex"));
  const actual = Buffer.from(signatureHeader);
  if (expected.length !== actual.length) return false;
  return timingSafeEqual(expected, actual);
}

function formatMessage(event, payload) {
  const repo = payload.repository?.full_name || "unknown";
  switch (event) {
    case "pull_request_review": {
      const pr = payload.pull_request;
      const review = payload.review;
      return [`GitHub PR Review on ${repo}#${pr.number}: "${pr.title}"`, `Reviewer: ${review.user.login}`, `State: ${review.state}`, review.body ? `Comment: ${review.body}` : null, `PR URL: ${pr.html_url}`].filter(Boolean).join("\n");
    }
    case "pull_request_review_comment": {
      const pr = payload.pull_request;
      const comment = payload.comment;
      return [`GitHub PR Comment on ${repo}#${pr.number}: "${pr.title}"`, `Author: ${comment.user.login}`, `File: ${comment.path}:${comment.line || comment.original_line || "?"}`, `Comment: ${comment.body}`, `PR URL: ${pr.html_url}`, `Comment URL: ${comment.html_url}`].join("\n");
    }
    case "issue_comment": {
      const issue = payload.issue;
      const comment = payload.comment;
      const isPR = !!issue.pull_request;
      return [`GitHub ${isPR ? "PR" : "Issue"} Comment on ${repo}#${issue.number}: "${issue.title}"`, `Author: ${comment.user.login}`, `Comment: ${comment.body}`, `URL: ${comment.html_url}`].join("\n");
    }
    case "issues": {
      const issue = payload.issue;
      return [`GitHub Issue ${payload.action} on ${repo}#${issue.number}: "${issue.title}"`, issue.body ? `Body: ${issue.body.slice(0, 500)}` : null, `Author: ${issue.user.login}`, `URL: ${issue.html_url}`].filter(Boolean).join("\n");
    }
    case "pull_request": {
      const pr = payload.pull_request;
      return [`GitHub PR ${payload.action} on ${repo}#${pr.number}: "${pr.title}"`, pr.body ? `Body: ${pr.body.slice(0, 500)}` : null, `Author: ${pr.user.login}`, `URL: ${pr.html_url}`].filter(Boolean).join("\n");
    }
    default:
      return `GitHub event "${event}" on ${repo}: ${JSON.stringify(payload).slice(0, 500)}`;
  }
}

const server = createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
    return;
  }
  if (req.method !== "POST") {
    res.writeHead(405);
    res.end("Method not allowed");
    return;
  }
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks);
  const signature = req.headers["x-hub-signature-256"];
  if (!validateSignature(body, signature)) {
    console.error(`[${new Date().toISOString()}] REJECTED: invalid signature from ${req.socket.remoteAddress}`);
    res.writeHead(403);
    res.end("Invalid signature");
    return;
  }
  let payload;
  try { payload = JSON.parse(body.toString()); } catch { res.writeHead(400); res.end("Invalid JSON"); return; }
  const event = req.headers["x-github-event"];
  const delivery = req.headers["x-github-delivery"];
  if (event === "ping") {
    console.log(`[${new Date().toISOString()}] Ping received: ${payload.zen}`);
    res.writeHead(200);
    res.end("pong");
    return;
  }
  const message = formatMessage(event, payload);
  const repoName = payload.repository?.full_name || "unknown";
  console.log(`[${new Date().toISOString()}] ${event} on ${repoName} (delivery: ${delivery})`);
  try {
    const response = await fetch(OPENCLAW_HOOKS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${OPENCLAW_HOOKS_TOKEN}` },
      body: JSON.stringify({ message, name: "GitHub", wakeMode: "now", deliver: false }),
    });
    if (!response.ok) {
      const text = await response.text();
      console.error(`[${new Date().toISOString()}] OpenClaw hooks returned ${response.status}: ${text}`);
      res.writeHead(502);
      res.end("Upstream error");
      return;
    }
    console.log(`[${new Date().toISOString()}] Forwarded ${event} to OpenClaw hooks`);
    res.writeHead(200);
    res.end("OK");
  } catch (err) {
    console.error(`[${new Date().toISOString()}] Failed to forward to OpenClaw: ${err.message}`);
    res.writeHead(502);
    res.end("Upstream error");
  }
});

server.listen(PORT, () => {
  console.log(`OpenClaw webhook proxy listening on port ${PORT}`);
  console.log(`Forwarding to: ${OPENCLAW_HOOKS_URL}`);
});
SERVEREOF

EXPOSE 3000
USER node
CMD ["node", "server.mjs"]
