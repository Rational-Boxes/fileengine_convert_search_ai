# convert_search_ai service image.
#
# Reuses the FileEngine Python client (`fileengine`, from the sibling
# python_interface/ checkout), so build with the *parent* directory as context:
#
#   podman build -f convert_search_ai/Containerfile -t convert-search-ai ..
#
# Run (pass LDAP/core/Redis/Postgres config via env; see .env.example):
#   podman run --rm -p 8092:8092 --env-file convert_search_ai/.env convert-search-ai
FROM python:3.12-slim

# Conversion toolchain: LibreOffice (Office -> PDF/text), ImageMagick (image
# thumbnails/previews), FFmpeg (video previews), libmagic (MIME detection).
# 3D/CAD: occt-draw is OpenCASCADE's DRAW CLI (STEP/IGES/BREP/OBJ/VRML -> glTF);
# nodejs/npm host convert2xkt (glTF/IFC/... -> XKT) for the xeokit viewer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
        imagemagick \
        ffmpeg \
        libmagic1 \
        occt-draw \
        nodejs npm \
    && npm install -g @xeokit/xeokit-convert@1.3.2 \
    && ln -sf "$(npm root -g)/@xeokit/xeokit-convert/convert2xkt.js" /usr/local/bin/convert2xkt \
    && chmod +x "$(npm root -g)/@xeokit/xeokit-convert/convert2xkt.js" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# On Debian the OpenCASCADE DRAW binary is named "occt-draw" (Fedora ships it as
# "DRAWEXE", the CSAI default), so point the CAD backend at it here.
ENV CSAI_3D_DRAWEXE=occt-draw

WORKDIR /app

# The reused client first (changes rarely -> better layer caching), then this
# service. Never copy the gitignored .env (it holds credentials).
COPY python_interface/ /app/python_interface/
COPY convert_search_ai/pyproject.toml convert_search_ai/README.md /app/convert_search_ai/
COPY convert_search_ai/src/ /app/convert_search_ai/src/
COPY convert_search_ai/migrations/ /app/convert_search_ai/migrations/

# Install with the `pdf` extra (pdfplumber) for table/structure-preserving PDF
# extraction. For higher fidelity add `pdf-docling` (best, heavy) — build with
# `--build-arg PDF_EXTRA=pdf,pdf-docling`.
ARG PDF_EXTRA=pdf
RUN pip install --no-cache-dir /app/python_interface && \
    pip install --no-cache-dir "/app/convert_search_ai[${PDF_EXTRA}]"

# Bind all interfaces inside the container.
ENV CSAI_HTTP_HOST=0.0.0.0 \
    CSAI_HTTP_PORT=8092
EXPOSE 8092

CMD ["convert-search-ai"]
