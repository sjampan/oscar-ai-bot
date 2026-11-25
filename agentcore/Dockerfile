# OSCAR AgentCore runtime image. AgentCore Runtime requires linux/arm64.
# Build context is the agentcore/ directory; run utils/agentcore_export.py first
# so agentcore/specs/*.json exists.

FROM --platform=linux/arm64 python:3.12.12-slim AS builder
WORKDIR /app
RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM --platform=linux/arm64 python:3.12.12-slim
RUN useradd --system --uid 1001 --no-create-home oscar
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=oscar . /app/agentcore/
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1
USER oscar
EXPOSE 8080
# The app binds 0.0.0.0:8080 inside the container. AgentCore routes traffic to it
# internally; never publish this port beyond 127.0.0.1 when running locally.
CMD ["python", "-m", "agentcore.app"]
