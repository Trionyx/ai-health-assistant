FROM node:22-alpine AS build

WORKDIR /app

COPY package.json ./
RUN corepack enable && corepack pnpm install --no-frozen-lockfile

COPY . .
RUN corepack enable && corepack pnpm build

FROM nginx:1.27-alpine

COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

