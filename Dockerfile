FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 pia
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY pasarela ./pasarela
USER pia
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/salud', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "pasarela.app:crear_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--timeout-graceful-shutdown", "30"]
