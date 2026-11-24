# 接收服务只运行 Webhook。消费者额外安装 Claude Code，供模板推荐调用模型。
# 构建上下文是仓库根目录，以便同时复制 common 和 issue-gov-agent。
FROM python:3.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

COPY issue-gov-agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=app:app common /app/common
COPY --chown=app:app issue-gov-agent/issue_gov /app/issue_gov
COPY --chown=app:app issue-gov-agent/config /app/config
USER app

FROM base AS receiver
EXPOSE 7000
CMD ["python", "-m", "issue_gov.receiver"]

FROM base AS consumer
USER root
ARG NODE_VERSION=22.21.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl xz-utils \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && npm install -g @anthropic-ai/claude-code@2.1.316 \
    && apt-get purge -y curl xz-utils \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
USER app
CMD ["python", "-m", "issue_gov.worker"]
