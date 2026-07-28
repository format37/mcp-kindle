FROM python:3.11-slim

ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLANTUML_JAR=/opt/plantuml.jar \
    MPLCONFIGDIR=/tmp/matplotlib
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# One apt layer for every native dependency of the pipeline:
#   PlantUML   -> default-jre-headless + graphviz (dot) + the pinned jar
#   d2         -> static binary; librsvg2-bin rasterises its SVG, because d2's
#                 own PNG export downloads a headless Chromium at run time
#                 (fails in a slim image, and would add ~400 MB if bundled)
#   preview    -> poppler-utils gives pdftoppm/pdfinfo to rasterise the PDF
#   WeasyPrint -> pango/harfbuzz/ffi/jpeg/openjpeg + shared-mime-info.
#                 libharfbuzz-subset0 is a SEPARATE Debian package and is what
#                 WeasyPrint >=61 dlopens for font subsetting; it fails soft
#                 (allow_fail=True) so without it every PDF silently embeds
#                 whole font files instead of subsets.
#   fonts      -> WeasyPrint with no font installed renders empty boxes instead
#                 of failing, which would silently gut the preview feature
# Both downloads are exercised in this layer (d2 --version, plantuml -version)
# so a bad URL or a changed tarball layout fails the build instead of shipping
# an image whose diagram tools only break at request time.
ARG PLANTUML_VERSION=1.2026.3
ARG D2_VERSION=0.7.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        default-jre-headless \
        graphviz \
        poppler-utils \
        librsvg2-bin \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libharfbuzz-subset0 \
        libffi8 \
        libjpeg62-turbo \
        libopenjp2-7 \
        shared-mime-info \
        fonts-dejavu-core \
        fonts-liberation \
        ca-certificates \
        wget \
 && wget -q -O /opt/plantuml.jar \
        "https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar" \
 && wget -q -O /tmp/d2.tar.gz \
        "https://github.com/terrastruct/d2/releases/download/v${D2_VERSION}/d2-v${D2_VERSION}-linux-amd64.tar.gz" \
 && tar -xzf /tmp/d2.tar.gz -C /tmp \
 && install -m 0755 "/tmp/d2-v${D2_VERSION}/bin/d2" /usr/local/bin/d2 \
 && rm -rf /tmp/d2.tar.gz "/tmp/d2-v${D2_VERSION}" \
 && d2 --version \
 && rsvg-convert --version \
 && java -jar /opt/plantuml.jar -version > /dev/null \
 && apt-get purge -y --auto-remove wget \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8018
EXPOSE 8018

# Docker's own probe; Caddy on the VPS also polls /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c 'import os,urllib.request;urllib.request.urlopen("http://127.0.0.1:%s/health" % os.environ.get("PORT","8018"), timeout=4).read()' || exit 1

# Entry through main() (not the uvicorn CLI) so PORT/LOG_LEVEL and the
# proxy_headers/keep-alive settings the Caddy deployment needs actually apply.
CMD ["python", "main.py"]
