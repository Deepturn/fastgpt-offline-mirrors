FROM python:3.12-slim-bookworm

ARG PYPISERVER_VERSION=2.3.2
RUN python -m pip install --no-cache-dir --disable-pip-version-check "pypiserver==${PYPISERVER_VERSION}"

EXPOSE 8081
ENTRYPOINT ["pypi-server"]
