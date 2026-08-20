FROM python:3.11-slim

RUN useradd --create-home --uid 10001 botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gares_bot.py .

RUN mkdir -p /app/data && chown -R botuser:botuser /app

CMD ["python", "gares_bot.py"]
