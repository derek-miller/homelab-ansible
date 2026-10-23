FROM node:22-alpine

RUN apk add --no-cache openssh-client && mkdir /data && chown node:node /data

WORKDIR /app
RUN npm install --omit=dev --no-audit --no-fund marked dompurify && cp node_modules/marked/lib/marked.umd.js node_modules/dompurify/dist/purify.min.js .
COPY server.mjs admin.html ./

EXPOSE 3000

USER node
VOLUME /data

CMD ["node", "--disable-warning=ExperimentalWarning", "server.mjs"]
