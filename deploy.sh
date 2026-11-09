```bash
#!/bin/bash
set -e

cd ~/telegram_sampler

echo "==> Pulling latest code"
git pull

echo "==> Building Docker image"
docker build -t telegram_sampler .

echo "==> Running tests"
docker run --rm --entrypoint python telegram_sampler -m pytest -v

echo "==> Tests passed! Deploying..."

docker rm -f telegram_sampler

docker run -d --name telegram_sampler --restart unless-stopped \
  --env-file .env -v /opt/sampler-data:/data:Z telegram_sampler

echo "==> Deployment complete!"
```