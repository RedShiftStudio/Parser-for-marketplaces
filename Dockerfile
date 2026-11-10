FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium
COPY . .
RUN useradd -m appuser && chown -R appuser:appuser /app && chmod -R a+rX /ms-playwright
USER appuser
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000"]
