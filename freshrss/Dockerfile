FROM freshrss/freshrss:latest

COPY apache-config.conf /etc/apache2/sites-available/000-default.conf

# Replace hardcoded port 80 with $PORT at runtime
CMD ["/bin/sh", "-c", "sed -i \"s/__PORT__/${PORT:-80}/g\" /etc/apache2/sites-available/000-default.conf && apache2-foreground"]
