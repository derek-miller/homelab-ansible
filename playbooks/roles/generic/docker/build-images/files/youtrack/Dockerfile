# Custom arm64 YouTrack image for Raspberry Pi 4 (ARMv8.0)
# The official jetbrains/youtrack image uses Amazon Linux 2023 which requires
# ARMv8.2-a (Graviton 2+). This image repackages YouTrack on an Ubuntu base
# with Eclipse Temurin JDK 21, which supports ARMv8.0.

FROM jetbrains/youtrack:2026.1.12458 AS source

FROM eclipse-temurin:21-jdk-noble

RUN apt-get update && apt-get install -y --no-install-recommends \
    fontconfig \
    hostname \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r -g 13001 jetbrains && \
    useradd -r -g jetbrains -u 13001 -d /home/jetbrains jetbrains && \
    mkdir -p /home/jetbrains && \
    chown jetbrains:jetbrains /home/jetbrains

COPY --from=source /opt/youtrack /opt/youtrack
COPY --from=source /run.sh /run.sh
COPY --from=source /stop.sh /stop.sh
COPY --from=source /usr/bin/stop /usr/bin/stop

# Remove bundled Amazon Corretto JDK — we use Temurin instead
# Ensure installation.xml marks this as a Docker install (not ZIP)
RUN rm -rf /opt/youtrack/internal/java && \
    sed -i 's_<type>ZIP</type>_<type>DOCKER</type>_g' /opt/youtrack/internal/conf/installation.xml && \
    sed -i '/<type>DOCKER<\/type>/{ /installationPort/! s_\(<type>DOCKER</type>\)_\1\n  <installationPort>8080</installationPort>_ }' /opt/youtrack/internal/conf/installation.xml && \
    mkdir -p /opt/youtrack/conf/internal && \
    touch /opt/youtrack/conf/internal/inside.container.conf.marker && \
    mkdir -p /not-mapped-to-volume-dir && \
    chown -R jetbrains:jetbrains /opt/youtrack /not-mapped-to-volume-dir

ENV JAVA_HOME=/opt/java/openjdk
ENV LANG=C.UTF-8
ENV JETBRAINS_YOUTRACK_OCR_TRAINEDDATAPATH=/opt/youtrack/image-recognition/tessdata
ENV JETBRAINS_YOUTRACK_OCR_ENABLED=true

EXPOSE 8080

USER jetbrains

VOLUME ["/opt/youtrack/logs", "/opt/youtrack/conf", "/opt/youtrack/data", "/opt/youtrack/backups"]

ENTRYPOINT ["/bin/bash", "/run.sh"]
