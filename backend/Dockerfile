FROM python:3.11-slim

ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# PlantUML deps: Java to run the jar, Graphviz for non-sequence layouts,
# wget+ca-certificates to fetch the jar at build time.
ARG PLANTUML_VERSION=1.2026.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        default-jre-headless \
        graphviz \
        wget \
        ca-certificates \
 && wget -q -O /opt/plantuml.jar \
        "https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar" \
 && apt-get purge -y --auto-remove wget \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8018

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8018"]
