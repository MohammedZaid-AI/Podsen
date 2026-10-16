FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FLASK_ENV=production PORT=3000
EXPOSE 3000
# --timeout 300: a first "today" run fetches every saved feed while the user waits;
# Gunicorn's 30s default could kill it mid-flight.
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:$PORT --timeout 300 app:app"]
