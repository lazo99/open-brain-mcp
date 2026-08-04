FROM python:3.11-slim

# --- CLI toolbox (ops-only use via the allowlisted run_command tool) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git postgresql-client apt-transport-https ca-certificates gnupg \
    && curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts --install-dir=/opt \
    && ln -s /opt/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud \
    && ln -s /opt/google-cloud-sdk/bin/gsutil /usr/local/bin/gsutil \
    && /opt/google-cloud-sdk/bin/gcloud components install kubectl --quiet \
    && ln -s /opt/google-cloud-sdk/bin/kubectl /usr/local/bin/kubectl \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the fastembed model at build time so Cloud Run cold starts
# (scale-to-zero -> new instance) don't re-fetch it on every request.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"

COPY server.py server_http.py ./

ENV OPEN_BRAIN_EMBED_MODEL=BAAI/bge-small-en-v1.5
EXPOSE 8080
CMD ["python", "server_http.py"]
