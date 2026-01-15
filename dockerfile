FROM itzg/minecraft-server

LABEL maintainer="Kantaphong"
LABEL version="1.0"
LABEL description="Minecraft Server for SDA Project"

HEALTHCHECK --interval=1m --timeout=10s --retries=3 \
  CMD mc-health