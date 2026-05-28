ARG REGISTRY=docker.io
FROM ${REGISTRY}/python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
      supervisor \
      fonts-noto-cjk \
      fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code --registry https://registry.npmmirror.com

RUN fc-cache -fv

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com

COPY backend/ .

COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

RUN mkdir -p /workspace/.claude/skills /var/log/eido

ENV WORKSPACE_ROOT=/workspace
ENV SKILLS_DIR=/workspace/.claude/skills
ENV LOG_DIR=/var/log/eido

EXPOSE 8000

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
