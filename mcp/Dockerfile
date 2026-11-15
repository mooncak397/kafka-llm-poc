# S17: mcp-confluent v1.6.1 (MIT), exact dependency tree from package-lock.json (npm ci).
FROM docker.io/library/node:22.23.5-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
USER node
# Config, tool allow-list and secrets come from the Deployment (k8s/11-mcp-confluent.yaml).
ENTRYPOINT ["node", "/app/node_modules/@confluentinc/mcp-confluent/dist/index.js"]
