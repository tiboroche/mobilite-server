# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Vue d'ensemble

Service mono-utilisateur de suivi de routines de mobilité + rappels push via [ntfy](https://ntfy.sh). `server.py` sert la PWA (`static/`), une API JSON et stocke tout dans SQLite. **Bibliothèque standard Python uniquement (3.9+), aucune dépendance** — ne pas en ajouter sans raison forte. Code, messages et commentaires sont en français.

## Commandes

```bash
# Lancer en local (server.py lit l'environnement, pas le fichier .env)
set -a; . ./.env; set +a; python3 server.py

# Tests (bout en bout : vrai serveur + faux ntfy, DB temporaire)
python3 test_server.py
python3 test_server.py T.test_quick_link        # un seul test (unittest)

# Valider content.json sans démarrer le serveur
python3 server.py --check

# Docker
docker compose up -d --build
./deploy.sh                 # sur le serveur : git pull + rebuild + vérification /api/health
```

`MOB_TOKEN` (≥ 16 caractères) est obligatoire, sinon `main()` s'arrête. Exemples de reverse proxy HTTPS : `Caddyfile.example`, `apache2.conf.example`, `nginx.conf.example`. Voir `.env.example` pour les autres variables (`NTFY_*`, `PUBLIC_URL`, `MOB_TZ`, `MOB_TRUST_PROXY`, `MOB_DB`).

## Architecture

- **`server.py`** : un seul fichier, sections séparées par des commentaires `# ----`.
  - Config lue en variables globales au moment de l'import → les tests posent `os.environ` **avant** `import server`.
  - SQLite via `run()` (une connexion par appel, sérialisé par `DBLOCK`). Tables : `kv` (réglages JSON sous la clé `cfg`), `log` (créneaux cochés), `sent` (notifications envoyées, `stage` 0 = rappel, 1 = relance).
  - `clean_cfg()` valide les réglages en ignorant silencieusement les valeurs invalides (fusion sur la config existante).
  - Planificateur : thread `scheduler()` qui appelle `tick()` toutes les 20 s. Un rappel n'est envoyé que dans `WINDOW_MIN` (10 min) après l'heure prévue — pas de rattrapage si le serveur était éteint. `tick(now)` accepte une date injectée pour les tests.
  - HTTP : `ThreadingHTTPServer` + `Handler.route()` → `api()` ou `static()`. Auth `Bearer MOB_TOKEN` (comparaison à temps constant), limitation à 10 échecs / 10 min / IP (`FAILS`, en mémoire). `/api/health` et `/api/quick` sont sans jeton ; `/api/quick` est le bouton « Fait » des notifications, protégé par HMAC (`sign()`) et valable seulement J/J+1.
- **`static/index.html`** : toute la PWA (HTML/CSS/JS inline, ES5, pas de build). Le contenu métier n'y est plus : la page le charge depuis `/content.json` (variables `EX`, `ROUT`, `PH`, `TIPS`…). Jeton et cache stockés en `localStorage`. CSP stricte définie dans `server.py` (`CSP`) : seules les polices Google sont autorisées en externe.
- **`content.json`** : tout le contenu éditable (exercices, routines `reveil`/`pause`/`coucher`, plan de force, conseils, textes des notifications). Servi sans jeton sur `/content.json`, relu par le serveur quand son mtime change (`read_content()`, dernière version valide conservée si le fichier est cassé), validé par `validate_content()`. Monté en lecture seule dans Docker : le modifier ne demande pas de rebuild. Toute évolution de son schéma touche `validate_content()`, le rendu dans `index.html` et la table du `README.md`.
- **`static/sw.js`** : service worker réseau-d'abord pour le shell ; n'intercepte jamais `/api/`. Incrémenter `CACHE` si la liste `SHELL` change.

## Invariants à respecter

- **Logique des créneaux dupliquée** : `slots_for()` dans `server.py` et `slotsFor()` dans `static/index.html` doivent rester identiques (réveil, pauses `pHH:MM` les jours travaillés de `start` à `end` par pas de `every` h, `force` les jours de force, `coucher`). Toute modification doit être faite des deux côtés.
- Identifiants de créneaux validés par `SLOT_RE` : `reveil`, `coucher`, `force`, `pHH:MM`. Un nouveau type de créneau implique `SLOT_RE`, `DEFAULT_MESSAGES` (et `notifications` dans `content.json`), les deux fonctions de créneaux et le front.
- Nouveaux réglages : les ajouter à `DEFAULT_CFG` **et** à la validation de `clean_cfg()`, sinon ils sont ignorés.
- Tenir à jour la table d'API et le nombre de tests dans `README.md`.
