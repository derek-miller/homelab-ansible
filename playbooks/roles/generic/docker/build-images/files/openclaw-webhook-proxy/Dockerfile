FROM node:24-alpine

RUN apk add --no-cache openssh-client && mkdir /data && chown node:node /data

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && cp node_modules/marked/lib/marked.umd.js node_modules/dompurify/dist/purify.min.js .
COPY server.mjs admin.html ./

EXPOSE 3000

USER node
VOLUME /data

CMD ["node", "--disable-warning=ExperimentalWarning", "server.mjs"]
