#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: bash $0 HOST_IPV4 DEPLOY_DIR NEW_OR_EXISTING_DATA_ROOT" >&2
  exit 2
fi
if [[ $(id -u) != 0 ]]; then
  echo "Run as root to provision the Compose volumes and credentials." >&2
  exit 2
fi

ip=$1
deploy=$(realpath "$2")
data=$(realpath "$3")
image=fastgpt-mirror-manager:1.0.0-amd64
here=$(cd "$(dirname "$0")/.." && pwd)
[[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && $data != / && $deploy != / ]]
[[ -f $data/npm/config.yaml && -d $data/npm/storage && -d $data/pypi/packages ]]
docker image inspect "$image" >/dev/null

# Keep the original NPM data and configuration available for rollback.
init_pending="$data/npm/.managed-storage.pending"
if [[ ! -d $data/npm/managed-storage ]]; then
  mkdir "$init_pending"
  trap 'rm -rf -- "$init_pending"' EXIT
  cp -a "$data/npm/storage/." "$init_pending/"
  docker run --rm --pull=never --network none --user 0:0 \
    -v "$init_pending:/migration" --entrypoint python3.12 "$image" /app/registry.py prepare-npm /migration
  chown -R 1000:1000 "$init_pending"
  mv "$init_pending" "$data/npm/managed-storage"
  trap - EXIT
fi

secrets="$data/manager/secrets"
install -d -m 0750 -o root -g 1000 "$secrets"
install -d -m 0750 -o 1000 -g 1000 "$data/manager/state" "$data/manager/state/jobs"
chgrp 1000 "$data/pypi" "$data/pypi/packages"
chmod g+rwx "$data/pypi" "$data/pypi/packages"

if [[ ! -e $secrets/manager.json ]]; then
  umask 077
  if [[ ! -s $secrets/initial-admin-password ]]; then
    openssl rand -hex 24 > "$secrets/initial-admin-password"
  fi
  docker run --rm -i --pull=never --network none "$image" init-admin \
    < "$secrets/initial-admin-password" > "$secrets/manager.json.pending"
  mv "$secrets/manager.json.pending" "$secrets/manager.json"
fi
if [[ ! -s $secrets/publisher-password ]]; then
  (umask 077; openssl rand -hex 32 > "$secrets/publisher-password")
fi
if [[ ! -s $secrets/htpasswd ]]; then
  (umask 077
   printf 'mirror-publisher:' > "$secrets/htpasswd"
   openssl passwd -apr1 -stdin < "$secrets/publisher-password" >> "$secrets/htpasswd")
fi
if [[ ! -s $secrets/server.crt || ! -s $secrets/server.key ]]; then
  (umask 077
   openssl req -x509 -newkey rsa:2048 -sha256 -days 397 -nodes \
     -subj "/CN=$ip" -addext "subjectAltName=IP:$ip" \
     -keyout "$secrets/server.key" -out "$secrets/server.crt" 2>/dev/null)
fi
chown root:1000 "$secrets/"{publisher-password,htpasswd,manager.json,server.key,server.crt}
chmod 0640 "$secrets/"{publisher-password,htpasswd,manager.json,server.key,server.crt}
chmod 0600 "$secrets/initial-admin-password" 2>/dev/null || true

install -d "$deploy/compose"
for file in docker-compose.yml docker-compose.manager.yml npm-managed.yaml; do
  if [[ $file == docker-compose.yml && -f $deploy/compose/$file ]]; then continue; fi
  source="$here/compose/$file"
  [[ $source -ef $deploy/compose/$file ]] || install -m 0644 "$source" "$deploy/compose/$file"
done
envfile="$deploy/compose/.env"
if [[ ! -f $envfile ]]; then
  (umask 077; printf 'MIRROR_BIND_IP=%s\nMIRROR_ROOT=%s\n' "$ip" "$data" > "$envfile")
else
  grep -Fxq "MIRROR_BIND_IP=$ip" "$envfile"
  grep -Fxq "MIRROR_ROOT=$data" "$envfile"
fi
compose=(docker compose --project-name local-mirrors --env-file "$envfile"
  -f "$deploy/compose/docker-compose.yml" -f "$deploy/compose/docker-compose.manager.yml")
"${compose[@]}" config -q
"${compose[@]}" up -d --pull never --wait --wait-timeout 100
echo "Management: https://$ip:18082/"
echo "Initial password file (root): $secrets/initial-admin-password"
echo "Certificate: $secrets/server.crt"
