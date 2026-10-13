ARG BUILD_FROM
FROM $BUILD_FROM

COPY rootfs /

RUN apk add --no-cache git \
    && chmod a+x /usr/bin/github-backup

CMD ["/usr/bin/github-backup"]
