FROM node:22-alpine AS builder

WORKDIR /build

COPY package.json yarn.lock tsconfig.json ./
COPY src ./src
COPY backend ./backend
COPY events ./events
COPY app ./app

RUN yarn install --frozen-lockfile --network-timeout 100000
RUN yarn build:server
RUN yarn install --production --frozen-lockfile --network-timeout 100000 && \
    yarn cache clean && \
    rm -rf /tmp/*

FROM node:22-alpine

RUN apk add --no-cache dumb-init

RUN addgroup -g 1001 -S mqttexplorer && \
    adduser -u 1001 -S mqttexplorer -G mqttexplorer

WORKDIR /app

COPY --from=builder --chown=mqttexplorer:mqttexplorer /build/dist ./dist
COPY --from=builder --chown=mqttexplorer:mqttexplorer /build/app/build ./app/build
COPY --from=builder --chown=mqttexplorer:mqttexplorer /build/app/index.html ./app/
COPY --from=builder --chown=mqttexplorer:mqttexplorer /build/node_modules ./node_modules
COPY --from=builder --chown=mqttexplorer:mqttexplorer /build/package.json ./

RUN mkdir -p /app/data && \
    chown -R mqttexplorer:mqttexplorer /app/data

USER mqttexplorer

EXPOSE 3000

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["node", "dist/src/server.js"]
