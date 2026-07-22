FROM node:26.5.0-trixie-slim

ARG APP_UID=1000

ENV NODE_ENV=production
WORKDIR /app

COPY steam-adapter/package.json steam-adapter/package-lock.json ./
RUN if [ "${APP_UID}" != "1000" ]; then usermod --uid "${APP_UID}" node; fi \
    && npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
    && mkdir -p /data \
    && chown node:node /data

COPY steam-adapter/src ./src

USER node
CMD ["node", "src/index.mjs"]
