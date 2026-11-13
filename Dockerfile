FROM python:3.13-slim

WORKDIR /app

# exiftool writes the metadata and reads the films, jpegtran rotates JPEGs without re-encoding,
# ffmpeg takes the one picture out of a film that stands for it in the overview
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl libjpeg-turbo-progs ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# the list of towns that names the place of a photo (GeoNames, CC BY 4.0), boiled down to one small file.
# It stands before the programme itself, so it is fetched again only when geo.py changes.
# GEO_LANG: the language of the names where GeoNames has one; empty = as GeoNames lists them
ARG GEO_LANG=de
COPY src/augenblick/geo.py /tmp/geo.py
RUN python /tmp/geo.py fetch /opt/augenblick/geo ${GEO_LANG} && rm /tmp/geo.py

COPY pyproject.toml VERSION README.md LICENSE ./
COPY src/ src/
COPY VERSION src/augenblick/VERSION
RUN pip install --no-cache-dir . && rm -rf src build

# the two small networks that find and compare faces (OpenCV Zoo: YuNet, MIT; SFace, Apache 2.0),
# checked against their hashes – at run time nothing is fetched from outside
RUN python -m augenblick.faces fetch /opt/augenblick/models

ENV AUGENBLICK_PHOTOS=/fotos \
    AUGENBLICK_DATA=/data \
    AUGENBLICK_PORT=8000 \
    AUGENBLICK_MODELS=/opt/augenblick/models \
    AUGENBLICK_GEO=/opt/augenblick/geo \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/version', timeout=5)"]

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["augenblick"]
