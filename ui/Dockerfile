# ui/Dockerfile — Streamlit chat UI
# ==================================
# Lightweight image for the internal demo UI.
# Railway deploys this as a separate service from the same repo —
# set the service's Root Directory to /ui in the Railway dashboard.

FROM python:3.12-slim

WORKDIR /app

# Install only what the UI needs — no FastAPI, no DB drivers.
# Pinned ranges keep builds reproducible without a lockfile.
RUN pip install --no-cache-dir \
    streamlit>=1.40.0 \
    requests>=2.32.0

COPY chat_app.py .

# Railway injects $PORT at runtime. Streamlit must bind to 0.0.0.0
# (not 127.0.0.1) so Railway's proxy can reach it.
# --server.headless=true disables the "email" prompt on first run.
CMD streamlit run chat_app.py \
    --server.port=$PORT \
    --server.address=0.0.0.0 \
    --server.headless=true
