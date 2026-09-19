FROM python:3.12-slim-bookworm
WORKDIR /app
COPY fan_guard.py /app/fan_guard.py
RUN mkdir -p /guard /state /config
USER 0:0
ENTRYPOINT ["python3", "-B", "/app/fan_guard.py"]
