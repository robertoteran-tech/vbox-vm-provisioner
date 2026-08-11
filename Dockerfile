FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libcurl4t64 \
        liblzf1 \
        libpng16-16t64 \
        libvpx9 \
        libxml2 \
        openssl \
        python3 \
        python3-yaml \
        xorriso \
    && rm -rf /var/lib/apt/lists/*

ENV VBOX_INSTALL_PATH=/usr/lib/virtualbox
ENV LD_LIBRARY_PATH=/usr/lib/virtualbox
ENV PATH=/usr/lib/virtualbox:${PATH}

WORKDIR /workspace

ENTRYPOINT ["python3"]
