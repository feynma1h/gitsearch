# Embedding service container.
#
# Loads BAAI/bge-small-en-v1.5 once at startup, serves POST /embed.
# CPU-only — see ADR 0007. We explicitly pull the CPU-only PyTorch
# wheel from PyTorch's CPU index so we don't accidentally download
# the ~1.5 GB CUDA bundle, which is useless on the typical laptop or
# CPU-only server we deploy to.

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# --extra-index-url adds PyTorch's CPU wheel server. pip will prefer
# the CPU torch wheel found there over PyPI's default CUDA-enabled
# torch wheel.
RUN pip install --default-timeout=1000 --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt

COPY . /app/service

EXPOSE 8001

CMD ["uvicorn", "service.server:app", "--host", "0.0.0.0", "--port", "8001"]