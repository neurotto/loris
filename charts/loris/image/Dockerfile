FROM node:22-bookworm-slim AS node
FROM composer:2 AS composer
FROM php:8.4.27-apache-bookworm@sha256:e4fb8001898613d5d0d00eabbae33b9c30b050698b522f15d864bb0bac916945

RUN php -r 'if (version_compare(PHP_VERSION, "8.4.27", "<")) { fwrite(STDERR, "PHP >= 8.4.27 is required for CVE-2026-109618 and CVE-2026-109619.\n"); exit(1); }'

ARG LORIS_VERSION=29.0.0
ARG LORIS_MRI_VERSION=29.0.0

ENV APACHE_DOCUMENT_ROOT=/var/www/loris/htdocs \
    LORIS_MRI_ROOT=/opt/loris/bin/mri \
    PATH=/opt/loris/bin/mri/.venv/bin:${PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates gettext git make node-gyp python3 python3-dev python3-pip python3-venv \
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

RUN git clone --depth 1 --branch "v${LORIS_VERSION}" https://github.com/aces/Loris.git /var/www/loris \
    && cd /var/www/loris \
    && composer install --no-interaction --no-dev --prefer-dist --optimize-autoloader \
    && npm ci \
    && make \
    && git clone --depth 1 --branch "v${LORIS_MRI_VERSION}" https://github.com/aces/Loris-MRI.git "${LORIS_MRI_ROOT}" \
    && python3 -m venv "${LORIS_MRI_ROOT}/.venv" \
    && "${LORIS_MRI_ROOT}/.venv/bin/pip" install --no-cache-dir "${LORIS_MRI_ROOT}" \
    && mkdir -p /var/www/loris/smarty/templates_c \
    && chown -R www-data:www-data /var/www/loris/project /var/www/loris/smarty/templates_c \
    && printf '%s\n' '<Directory /var/www/loris/htdocs>' '    AllowOverride All' '    Require all granted' '</Directory>' \
       > /etc/apache2/conf-available/loris.conf \
    && a2enconf loris \
    && printf '%s\n' 'ServerName localhost' >> /etc/apache2/apache2.conf

COPY --chmod=0444 bootstrap-loris.php /usr/local/bin/bootstrap-loris.php
COPY --chmod=0444 module-manager-empty-response.patch /tmp/module-manager-empty-response.patch
COPY --chmod=0444 module-manager-required-modules.patch /tmp/module-manager-required-modules.patch
COPY --chmod=0444 brainbrowser-react-mount.patch /tmp/brainbrowser-react-mount.patch

RUN git -C /var/www/loris apply --check /tmp/module-manager-empty-response.patch \
    && git -C /var/www/loris apply /tmp/module-manager-empty-response.patch \
    && git -C /var/www/loris apply --check /tmp/module-manager-required-modules.patch \
    && git -C /var/www/loris apply /tmp/module-manager-required-modules.patch \
    && php -l /var/www/loris/modules/module_manager/php/modules.class.inc \
    && rm /tmp/module-manager-empty-response.patch /tmp/module-manager-required-modules.patch

RUN git -C /var/www/loris apply --check /tmp/brainbrowser-react-mount.patch \
    && git -C /var/www/loris apply /tmp/brainbrowser-react-mount.patch \
    && node --check /var/www/loris/modules/brainbrowser/js/brainbrowser.loris.js \
    && rm /tmp/brainbrowser-react-mount.patch

RUN docker-php-ext-install gettext \
    && sed -i 's@DocumentRoot /var/www/html@DocumentRoot /var/www/loris/htdocs@' /etc/apache2/sites-available/000-default.conf

WORKDIR /var/www/loris
EXPOSE 80
CMD ["apache2-foreground"]