# Certified L2 container for the Dynamic World land-cover driver (ADR-0030 clean-room).
# Its heavy, unusual deps (earthengine-api + google-auth) live HERE — never in the host Python. The only
# I/O the container is permitted is its DECLARED egress (meta.yaml): the broker + the GEE hosts.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY run.py .

# The launcher injects at run time (docker run -e …):
#   AIRAWAT_API   the broker base url reachable from the container (e.g. http://host.docker.internal:8080)
#   RUN_TOKEN     the run's confined token (dev-run-landcover-dw@<tenant>)
#   AOI_ID · TENANT
#   GEE_PROJECT · GEE_SERVICE_ACCOUNT · GEE_SERVICE_ACCOUNT_KEY   (materialised from Vault; see SETUP.md)
ENTRYPOINT ["python", "run.py"]
