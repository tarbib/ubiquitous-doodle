FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gares_bot.py .
RUN mkdir -p /app/data

CMD ["python", "gares_bot.py"]
