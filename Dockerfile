FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates ./templates
COPY data ./data

ENV PORT=5000
EXPOSE 5000
CMD ["sh", "-c", "gunicorn app:app --workers 1 --worker-class gthread --threads 4 --timeout 120 --bind 0.0.0.0:${PORT}"]
