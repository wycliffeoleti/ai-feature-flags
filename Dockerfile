# Single image for every Python process in the stack. The API, the evaluator,
# the controller, and the demo differ only by their command, so building one
# image keeps them provably on the same code.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first so application edits do not invalidate the layer.
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir \
      "pydantic>=2.6,<3" "fastapi>=0.110,<1" "uvicorn>=0.27,<1" \
      "psycopg[binary]>=3.1,<4" "redis>=5.0,<6" "scipy>=1.11,<2" "httpx>=0.27,<1"

COPY aiflags ./aiflags
COPY migrations ./migrations
COPY scripts ./scripts

# Nothing in this image needs to write to its filesystem or run as root.
RUN useradd --create-home --uid 10001 aiflags
USER aiflags

CMD ["uvicorn", "aiflags.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
