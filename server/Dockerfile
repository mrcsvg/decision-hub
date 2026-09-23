FROM python:3.12-slim

WORKDIR /app
COPY requirements-app.txt .
RUN pip install --no-cache-dir -r requirements-app.txt

COPY decision_memory ./decision_memory

ENV PYTHONUNBUFFERED=1
USER nobody
CMD ["python", "-m", "decision_memory"]
