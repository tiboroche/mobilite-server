#!/bin/sh
# Met à jour le service Docker : récupère le code, reconstruit l'image, redémarre et vérifie.
# Usage : ./deploy.sh   (depuis n'importe où ; à lancer sur le serveur)
set -eu
cd "$(dirname "$0")"

[ -f .env ] || { echo "Fichier .env absent : cp .env.example .env puis édite-le." >&2; exit 1; }

before=$(git rev-parse --short HEAD)
git pull --ff-only
after=$(git rev-parse --short HEAD)
if [ "$before" = "$after" ]; then
    echo "Déjà à jour ($after), reconstruction quand même."
else
    echo "Mise à jour $before -> $after :"
    git log --oneline "$before..$after"
fi

docker compose up -d --build --remove-orphans

printf "Attente du service"
i=0
until docker compose exec -T mobilite python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)" \
    >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 15 ]; then
        echo " échec. Derniers logs :" >&2
        docker compose logs --tail 30 mobilite >&2
        exit 1
    fi
    printf "."
    sleep 2
done
echo " OK"

docker image prune -f >/dev/null
docker compose logs --tail 5 mobilite
