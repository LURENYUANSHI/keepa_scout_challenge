# Multi-stage build: compile the Vue SPA with Node, then serve the static
# output with nginx. Keeps the shipped image small (no node_modules/Vite in
# the final layer) and matches how a real static frontend gets deployed.
FROM node:20-slim AS build
WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

# VITE_API_BASE_URL is baked into the built JS at build time (Vite inlines
# import.meta.env.* at build, not runtime) -- see docker-compose.yml's
# `frontend` service for the value used in the compose stack. Default here
# only matters for a build run without --build-arg.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine AS serve
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
