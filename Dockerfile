FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
RUN groupadd --gid 10001 wallet && useradd --uid 10001 --gid wallet --no-create-home wallet && mkdir /private /mail /recovery /test-config && chown wallet:wallet /private /mail /recovery /test-config

FROM dependencies AS runtime
# Installers and bundled bootstrap wheels are build inputs only.
RUN python -m pip uninstall --yes pip setuptools && rm -rf /usr/local/lib/python3.13/ensurepip /root/.cache
COPY . .
RUN DJANGO_SETTINGS_MODULE=config.collect python manage.py collectstatic --noinput
USER wallet
EXPOSE 8000
CMD ["gunicorn","config.wsgi:application","--bind","0.0.0.0:8000","--workers","3","--threads","4","--timeout","15","--access-logfile","/dev/null"]

FROM dependencies AS verification
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
COPY requirements-dev.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock && playwright install --with-deps chromium && chmod -R a+rX /opt/browsers
COPY . .
RUN DJANGO_SETTINGS_MODULE=config.collect python manage.py collectstatic --noinput
USER wallet
CMD ["gunicorn","config.wsgi:application","--bind","0.0.0.0:8000","--workers","3","--threads","4","--timeout","15","--access-logfile","/dev/null"]
