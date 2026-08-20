FROM python:3.11-slim

RUN useradd --create-home --uid 10001 botuser \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gares_bot.py .
RUN mkdir -p /app/data && chown -R botuser:botuser /app

# Le volume ./data est monté par-dessus le répertoire de l'image : le chown
# fait au build ne le concerne pas, seul l'hôte décide de son propriétaire.
# On corrige donc ici, à chaud (root, bref), avant de rendre la main à
# botuser. Le chown est best-effort : s'il échoue (montage exotique), on
# laisse la sonde applicative (storage_ok) le signaler plutôt que de faire
# planter le conteneur en boucle.
ENTRYPOINT ["sh", "-c", "if [ \"$(id -u)\" = \"0\" ]; then mkdir -p /app/data; chown -R botuser:botuser /app/data 2>/dev/null || true; exec gosu botuser \"$@\"; fi; exec \"$@\"", "--"]
CMD ["python", "gares_bot.py"]
