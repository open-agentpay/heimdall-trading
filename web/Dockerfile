# ==============================================================================
# Heimdall Trading Platform - 前端控制台多阶段 Dockerfile (React/Vite + Nginx)
# ==============================================================================

FROM node:20-alpine AS builder

WORKDIR /web

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . ./

ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm run build

FROM nginx:1.27-alpine AS runtime

RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/heimdall.conf
COPY --from=builder /web/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
