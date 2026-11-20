FROM python:3.10-slim-bookworm AS python310
FROM node:24-bookworm-slim AS node24
FROM python:3.12-slim-bookworm

COPY --from=python310 /usr/local/bin/python3.10 /usr/local/bin/
COPY --from=python310 /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/
COPY --from=python310 /usr/local/lib/python3.10 /usr/local/lib/python3.10
COPY --from=node24 /usr/local/bin/node /usr/local/bin/
COPY --from=node24 /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ldconfig \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && python3.10 -m pip --version \
    && npm --version
WORKDIR /app
COPY . /app/
USER 1000:1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3.12", "/app/server.py"]
CMD ["serve"]
