# VehicleGPT edge image — multi-arch (linux/arm64 for Raspberry Pi 4/5, AWS Graviton, automotive ARM boards; linux/amd64).
# Build:  docker buildx build --platform linux/arm64 -t vehiclegpt:edge -f deploy/Dockerfile .
# Run under the competition budget (2 cores, 2 GB):  see deploy/run_edge.sh
FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 VEHICLEGPT_ENV=production VEHICLEGPT_NET_PROBE=0 \
    VEHICLEGPT_LLM_THREADS=2 OMP_NUM_THREADS=2 VEHICLEGPT_LLM=llamacpp
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake can-utils iproute2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/vehiclegpt
COPY requirements.txt .
RUN pip install -r requirements.txt && pip install python-can cantools "llama-cpp-python==0.3.2"
COPY vehiclegpt vehiclegpt
COPY ui ui
COPY app.py run.sh ./
COPY scripts scripts
COPY benchmarks benchmarks
# Models are mounted read-only at runtime (-v $PWD/models:/opt/vehiclegpt/models:ro) to keep the image small.
RUN useradd -r -u 10001 vgpt && mkdir -p data && chown vgpt data
USER vgpt
EXPOSE 8502
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.port", "8502", "--server.address", "0.0.0.0"]
