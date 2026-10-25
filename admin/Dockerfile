FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --uid 10001 --create-home deya && mkdir -p /srv/staticfiles && chown deya /srv/staticfiles
USER deya
EXPOSE 8000
CMD ["gunicorn", "deya_admin.wsgi", "-b", "0.0.0.0:8000", "-w", "2"]
