# docker build \
# --tag minimal-alpine-checkmk-ci-master \
# -f buildscripts/scripts/Dockerfile .
#
# docker run -it --rm \
# --workdir /checkmk \
# -v ./:/checkmk \
# --name alpine321 \
# minimal-alpine-checkmk-ci-master:latest bash
#

FROM artifacts.lan.tribe29.com:4000/python@sha256:d26940861c070be314589373298b189cbf17e52f50548199e67e8f53b4c96fc4
# 119MB
# alpine:3.21 around 190MB
# python:3.11.6-slim around 1100MB

# bash is required to be used in Jenkins with image.inside(), requires +3MB
# git is required to use checkmk-dev-tools, requires +13MB
# make is required by scripts/fake-artifacts, see CMK-21338, requires +9MB
# coreutils, grep is required by validate_changes.py/find-[python,shell]-files
# to get "sort --zero-terminated" and "grep --binary-files=", requires +2MB
# cache removal saves 3MB
RUN apk update && \
    apk add bash git make coreutils grep && \
    rm -rf /var/cache/apk/*

# install checkmk-dev-tools to run ci-artifacts
# install pyyaml to parse editions.yml and stages.yml
RUN pip3 install --break-system-packages \
 "checkmk-dev-tools>=0.4.4" \
 pyyaml

# verify installation
RUN cmk-dev info
