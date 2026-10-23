FROM node:22-alpine

RUN apk add --no-cache openssh-client && mkdir /data && chown node:node /data

WORKDIR /app
COPY server.mjs .

EXPOSE 3000

USER node
VOLUME /data

CMD ["node", "--disable-warning=ExperimentalWarning", "server.mjs"]
