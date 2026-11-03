#   docker build -t codehop-demo .
#   docker run --rm -p 8000:8000 codehop-demo        # http://localhost:8000
FROM docker.io/library/python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY codehop_sim codehop_sim
COPY static static
COPY server.py .

# One BLAS thread per worker; frames are deterministic in (config, seed), so
# any number of workers/replicas can serve any request without sticky sessions.
ENV PORT=8000 WORKERS=2 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}"]
