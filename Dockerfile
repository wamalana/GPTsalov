FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY --chown=app:app gptsalov /app/gptsalov
COPY --chown=app:app config.toml /app/config.toml
USER app
ENTRYPOINT ["python", "-m", "gptsalov"]
CMD ["scan", "--source", "demo", "--config", "/app/config.toml"]
