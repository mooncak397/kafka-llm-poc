FROM docker.io/library/python:3.12-slim
WORKDIR /app
# requirements.txt comes from uv.lock (`make proxy-image` exports it), so the versions are the locked ones
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY agent.py pricing.py ./
USER 65532
CMD ["python", "agent.py"]
