FROM node:22.22.0-bookworm-slim

ENV NODE_ENV=production
WORKDIR /app

COPY steam-adapter/package.json steam-adapter/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force \
    && mkdir -p /data \
    && chown node:node /data

COPY steam-adapter/src ./src

USER node
CMD ["node", "src/index.mjs"]
