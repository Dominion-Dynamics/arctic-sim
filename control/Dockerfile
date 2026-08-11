# Control service: reset, logs, .env editing.
#
# docker:cli already carries the Docker CLI and the compose plugin, so every
# operation is a plain `docker compose ...` rather than hand-rolled Engine API
# calls. Only python3 is added on top.
FROM docker:cli
RUN apk add --no-cache python3
COPY control/server.py /app/server.py
WORKDIR /repo
EXPOSE 8090
CMD ["python3", "/app/server.py"]
