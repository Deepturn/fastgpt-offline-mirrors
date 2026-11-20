FROM node:24-bookworm-slim

ARG VERDACCIO_VERSION=6.1.6
RUN npm install --global --omit=dev --no-audit --no-fund "verdaccio@${VERDACCIO_VERSION}" \
    && mkdir -p /verdaccio/conf /verdaccio/storage \
    && chown -R node:node /verdaccio

USER node
EXPOSE 4873
ENTRYPOINT ["verdaccio"]
