FROM ghcr.io/actions/actions-runner:2.337.0

# git, curl and jq come with the base image; LuaJIT and node-gyp builds need a C toolchain and python3.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3 \
    && rm -rf /var/lib/apt/lists/*

COPY start.sh /home/runner/start.sh
RUN chmod 755 /home/runner/start.sh

USER runner

ENTRYPOINT ["/home/runner/start.sh"]
