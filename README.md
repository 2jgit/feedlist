# URL IP Monitor

A small Dockerized Flask app with a web UI to:

- add and remove URLs
- resolve each URL's hostname to IPv4 addresses only
- keep a plain text page of the current IPv4 list at `/ips`
- refresh IPs automatically every hour

## Run with Docker Compose

```bash
docker compose up -d --build
```

Open:

- Main UI: `http://localhost:8080/`
- Plain text IPv4 list: `http://localhost:8080/ips`
- JSON API: `http://localhost:8080/api/ips`

## Run with Docker

```bash
docker build -t url-ip-monitor .
docker run -d \
  --name url-ip-monitor \
  -p 8080:8080 \
  -e UPDATE_INTERVAL_MINUTES=60 \
  -v $(pwd)/data:/data \
  url-ip-monitor
```

## Notes

- The app stores URLs and resolved IPs in SQLite at `/data/data.db`.
- If a hostname has multiple A records, all IPv4 addresses are listed.
- Adding a URL triggers an immediate refresh.
- The scheduler refreshes all entries every 60 minutes by default.


Because the app includes an internal hourly scheduler, the container runs a single Gunicorn worker to avoid duplicate refresh jobs.
