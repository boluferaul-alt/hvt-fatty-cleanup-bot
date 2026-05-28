FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Default command (overridden in render.yaml for cron vs web).
CMD ["gunicorn", "app:app", "--workers", "1", "--threads", "4", "--timeout", "300", "--bind", "0.0.0.0:10000"]
