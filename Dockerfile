# IncidentPilot demo image (used for the Hugging Face Space).
# Runs the API + UI in demo mode: a scripted model plays the LLM, so no API key is needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# CPU-only PyTorch first (much smaller than the default CUDA build), then the project requirements.
COPY requirements.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

COPY . .

# Download the embedding model and the reranker at build time, so the first visitor does not wait.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('all-MiniLM-L6-v2'); CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Hugging Face Spaces run the container as user 1000, which must be able to write data/ and reports/.
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user

EXPOSE 7860
CMD ["python", "main.py", "serve", "--demo"]
