ARG BASE_IMAGE=eido-base:latest
FROM ${BASE_IMAGE}

WORKDIR /app
COPY backend/ .
COPY mcp.json /etc/eido/mcp.json

RUN mkdir -p /workspace/.claude/skills /var/log/eido

ENV WORKSPACE_ROOT=/workspace
ENV SKILLS_DIR=/workspace/.claude/skills
ENV LOG_DIR=/var/log/eido
ENV EIDO_DATA_ROOT=/workspace/.eido
ENV MCP_CONFIG_PATH=/etc/eido/mcp.json

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
