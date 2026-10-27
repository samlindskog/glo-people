# Build context is the repo root (needs shared/permissions.json). Produces nginx + static app.
FROM node:22-alpine AS build
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY shared /src/shared
COPY web ./
RUN npm run build

FROM nginx:1.29-alpine
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx/prod.conf /etc/nginx/conf.d/glo-people.conf
COPY nginx/proxy-headers.inc /etc/nginx/conf.d/proxy-headers.inc
COPY --from=build /src/web/dist /usr/share/nginx/html
