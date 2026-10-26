FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home scheduler
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /var/lib/fpga-scheduler && chown scheduler:scheduler /var/lib/fpga-scheduler
USER scheduler
ENTRYPOINT ["fpga-scheduler"]
CMD ["serve", "--config", "/etc/fpga-scheduler/config.toml"]
