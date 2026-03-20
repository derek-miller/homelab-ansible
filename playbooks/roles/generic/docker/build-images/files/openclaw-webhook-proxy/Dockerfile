FROM node:22-alpine

WORKDIR /app
COPY server.mjs .
COPY guardrails/ /app/guardrails/

EXPOSE 3000

USER node

CMD ["node", "server.mjs"]
