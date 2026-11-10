FROM node:22-bookworm-slim AS node
FROM composer:2 AS composer
FROM php:8.4.27-apache-bookworm@sha256:e4fb8001898613d5d0d00eabbae33b9c30b050698b522f15d864bb0bac916945

RUN php -r 'if (version_compare(PHP_VERSION, "8.4.27", "<")) { fwrite(STDERR, "PHP >= 8.4.27 is required for CVE-2026-109618 and CVE-2026-109619.\n"); exit(1); }'

ARG LORIS_VERSION=29.0.0
ARG LORIS_MRI_VERSION=29.0.0

ENV APACHE_DOCUMENT_ROOT=/var/www/loris/htdocs \
    LORIS_MRI_ROOT=/opt/loris/bin/mri \
    PATH=/opt/loris/bin/mri/.venv/bin:${PATH}

ARG PROTOC_GEN_JS_VERSION=3.21.4
ARG PROTOC_GEN_JS_SHA256=c57ba4130471c642462fcf98c844a3c933f6c4708b9fddc859900fd2a2e72a45

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates gettext git make node-gyp protobuf-compiler python3 python3-dev python3-pip python3-venv \
    libcurl4-openssl-dev libonig-dev \
    libfreetype6-dev libicu-dev libjpeg62-turbo-dev libmariadb-dev libpng-dev \
    libwebp-dev libxml2-dev libzip-dev zlib1g-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg --with-webp \
    && docker-php-ext-install -j"$(nproc)" curl gd intl mbstring mysqli opcache pdo_mysql xml zip \
    && a2enmod rewrite headers expires \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=composer /usr/bin/composer /usr/local/bin/composer

# Debian's protoc no longer ships the JavaScript generator the EEG viewer needs.
RUN python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], "/tmp/protoc-gen-js.tar.gz")' \
        "https://github.com/protocolbuffers/protobuf-javascript/releases/download/v${PROTOC_GEN_JS_VERSION}/protobuf-javascript-${PROTOC_GEN_JS_VERSION}-linux-x86_64.tar.gz" \
    && echo "${PROTOC_GEN_JS_SHA256}  /tmp/protoc-gen-js.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/protoc-gen-js.tar.gz -C /usr/local bin/protoc-gen-js \
    && rm /tmp/protoc-gen-js.tar.gz \
    && test -x /usr/local/bin/protoc-gen-js

RUN git clone --depth 1 --branch "v${LORIS_VERSION}" https://github.com/aces/Loris.git /var/www/loris \
    && cd /var/www/loris \
    && composer install --no-interaction --no-dev --prefer-dist --optimize-autoloader \
    && npm ci

COPY --chmod=0444 bootstrap-loris.php /usr/local/bin/bootstrap-loris.php
COPY --chmod=0444 module-manager-empty-response.patch module-manager-required-modules.patch \
    brainbrowser-react-mount.patch re-move-annex-content.patch /tmp/patches/

# Patches go in before the webpack build: re-move-annex-content changes the EEG viewer sources.
# re-move-annex-content: on-demand content of clone-sourced sites (RE_MOVE_CACHE_URL), 503 Retry-After
# instead of 404 with BrainBrowser and EEG viewer retries, EEG chunks prepared by re-move-cache.
RUN cd /var/www/loris \
    && for patch in module-manager-empty-response module-manager-required-modules \
        brainbrowser-react-mount re-move-annex-content; do \
        git apply --check "/tmp/patches/${patch}.patch" && git apply "/tmp/patches/${patch}.patch" || exit 1; \
    done \
    && rm -r /tmp/patches \
    && php -l modules/module_manager/php/modules.class.inc \
    && php -l src/ReMove/AnnexContent.php \
    && php -l modules/api/php/endpoints/candidate/visit/image/image.class.inc \
    && php -l modules/brainbrowser/php/imageinfo.class.inc \
    && php -l modules/electrophysiology_browser/php/file_reader.class.inc \
    && node --check modules/brainbrowser/js/brainbrowser.loris.js \
    && (cd modules/electrophysiology_browser/jsx/react-series-data-viewer \
        && protoc --plugin=protoc-gen-js=/usr/local/bin/protoc-gen-js \
            --js_out=import_style=commonjs,binary:./src/ protocol-buffers/chunk.proto \
        && test -s src/protocol-buffers/chunk_pb.js) \
    && EEG_VIS_ENABLED=true make \
    && grep -q deserializeBinary modules/electrophysiology_browser/js/electrophysiologySessionView.js \
    && grep -q Retry-After modules/electrophysiology_browser/js/electrophysiologySessionView.js \
    && git clone --depth 1 --branch "v${LORIS_MRI_VERSION}" https://github.com/aces/Loris-MRI.git "${LORIS_MRI_ROOT}" \
    && python3 -m venv "${LORIS_MRI_ROOT}/.venv" \
    && "${LORIS_MRI_ROOT}/.venv/bin/pip" install --no-cache-dir "${LORIS_MRI_ROOT}" \
    && mkdir -p /var/www/loris/smarty/templates_c \
    && chown -R www-data:www-data /var/www/loris/project /var/www/loris/smarty/templates_c \
    && printf '%s\n' '<Directory /var/www/loris/htdocs>' '    AllowOverride All' '    Require all granted' '</Directory>' \
       > /etc/apache2/conf-available/loris.conf \
    && a2enconf loris \
    && printf '%s\n' 'ServerName localhost' >> /etc/apache2/apache2.conf

RUN docker-php-ext-install gettext \
    && sed -i 's@DocumentRoot /var/www/html@DocumentRoot /var/www/loris/htdocs@' /etc/apache2/sites-available/000-default.conf

WORKDIR /var/www/loris
EXPOSE 80
CMD ["apache2-foreground"]