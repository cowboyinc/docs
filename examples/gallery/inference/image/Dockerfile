# Inference actor serving image (CIP-10 workload). Built by build-image.sh into a
# digest-addressed rootfs; the container runs offchain.host at the workload's
# serving endpoint with /etc/cowboy/host.json bind-mounted by the operator.
FROM python:3.12-slim
RUN pip install --no-cache-dir coincurve cryptography pycryptodome
WORKDIR /app
COPY offchain/ /app/offchain/
COPY --chmod=0755 cbfs /usr/local/bin/cbfs
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-m", "offchain.host", "--config", "/etc/cowboy/host.json"]
