FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FLASK_ENV=production PORT=3000
EXPOSE 3000
# --timeout 300: a triage request blocks on the ranking call. Gunicorn's 30s default
# would kill it mid-flight (and a local Ollama backend can take minutes).
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:$PORT --timeout 300 app:app"]
