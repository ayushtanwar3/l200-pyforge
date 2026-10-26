FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY configs/ ./configs/
COPY tests/ ./tests/
RUN mkdir -p /app/bin

ENTRYPOINT ["python3", "-m", "src.cli"]
CMD ["--help"]

