#!/bin/sh
# Apply only the concurrency code to an existing, verified Docker Compose app.
set -eu

app_directory=$(realpath "${1:?Pass the existing price-bot directory}")
revision=${2:?Pass the verified 40-character Git revision}
case "$revision" in *[!a-f0-9]*|'') exit 2 ;; esac
[ "${#revision}" -eq 40 ]
cd "$app_directory"
[ -f compose.yaml ] && [ -f bot.py ]

base_hash=59059213ddbdb400a18ed54d5cfcb6cf5f0b046493b4e3c8264616294d386624
[ "$(sha256sum bot.py | cut -d ' ' -f 1)" = "$base_hash" ] || {
    echo 'Source changed since verification; update cancelled.' >&2
    exit 1
}
container=$(docker compose ps -q price-bot)
[ -n "$container" ]
[ "$(docker exec "$container" sha256sum /app/bot.py | cut -d ' ' -f 1)" = "$base_hash" ]

stage=$(mktemp -d /tmp/pricebot-concurrency.XXXXXX)
base_url="https://raw.githubusercontent.com/alisavladimirovna02-source/price-bot/$revision"
files='bot.py price_jobs.py test_bot_runtime.py test_parse_prices.py test_price_files.py test_price_jobs.py'
for filename in $files; do
    curl --fail --silent --show-error --location --max-time 30 "$base_url/$filename" -o "$stage/$filename"
done
printf '%s  %s\n' \
    1de21c2c6e75ca083a5cc473fc11f9a371574fa2b80db35c0cae8f373e6e5329 "$stage/bot.py" \
    9b6326af6221cdcd330d5b977aad3fb617bfef0779d0b6261ee1eba6928379c0 "$stage/price_jobs.py" \
    | sha256sum --check

# The exact runtime image checks the overlay without tokens, network calls or
# a second Telegram polling process. Its filesystem is discarded afterwards.
old_image=$(docker inspect --format '{{.Image}}' "$container")
docker run --rm -v "$stage:/pricebot-update:ro" "$old_image" sh -c \
    'cp /pricebot-update/*.py /app/; cd /app; python3 -m unittest discover -q'

backup="$app_directory/deployment-backups/concurrency-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
for filename in $files; do
    if [ -f "$filename" ]; then cp -p "$filename" "$backup/$filename"; fi
done
printf '%s\n' "$old_image" > "$backup/previous-image.txt"
for filename in $files; do cp "$stage/$filename" "$filename"; done

if ! docker compose build price-bot; then
    for filename in $files; do
        if [ -f "$backup/$filename" ]; then
            cp -p "$backup/$filename" "$filename"
        else
            mv "$filename" "$backup/not-installed-$filename"
        fi
    done
    echo "Build failed; running container unchanged. Backup: $backup" >&2
    exit 1
fi
docker compose up -d --no-deps --timeout 180 price-bot
echo "Concurrency update installed. Backup: $backup"
docker compose ps
