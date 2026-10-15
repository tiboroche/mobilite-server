# Mobilité : suivi + rappels sur ton serveur

Petit service Python (bibliothèque standard, **aucune dépendance**) qui sert la page de suivi,
garde ton historique dans SQLite et t'envoie les rappels sur ton téléphone via [ntfy](https://ntfy.sh).

```
téléphone ──HTTPS──▶ reverse proxy ──▶ server.py (page + API + SQLite)
    ▲                                        │ à l'heure de chaque séance
    └────────── notification ◀── ntfy ◀──────┘
```

Ce que ça fait : réveil, pauses toutes les 2 h les jours travaillés, séances de force et coucher
sont notifiés à l'heure prévue, avec une relance si tu n'as rien coché. Le bouton **Fait** de la
notification coche la séance sans ouvrir l'app. L'état est partagé entre tes appareils.

## Mise en route (5 minutes)

### 1. Les notifications (ntfy)
1. Installe l'app **ntfy** sur ton téléphone (App Store / Google Play / F-Droid).
2. Choisis un nom de canal long et aléatoire, ex. `mobilite-k3j9x2m8q1w7`, et abonne-toi dans l'app.
   Sur ntfy.sh n'importe qui connaissant le nom peut lire le canal : le contenu des rappels n'a rien
   de sensible, mais garde ce nom secret. Tu peux aussi héberger ton propre ntfy.

### 2. Le service
```bash
cp .env.example .env
# édite .env : MOB_TOKEN, NTFY_TOPIC, PUBLIC_URL
python3 server.py            # lit les variables de .env si tu les exportes (voir ci-dessous)
```
`server.py` lit l'environnement, pas le fichier `.env`. En direct : `set -a; . ./.env; set +a; python3 server.py`.

**Docker (recommandé)**
```bash
docker compose up -d --build
docker compose logs -f
```
Pour les mises à jour : `./deploy.sh` (git pull, reconstruction, redémarrage, vérification de `/api/health`).

**systemd (sans Docker)**
```bash
sudo useradd -r -s /usr/sbin/nologin mobilite
sudo mkdir -p /opt/mobilite && sudo cp -r server.py static .env /opt/mobilite/
sudo mkdir -p /opt/mobilite/data && sudo chown -R mobilite /opt/mobilite/data
sudo cp mobilite.service /etc/systemd/system/
sudo systemctl enable --now mobilite
```

### 3. HTTPS
Le service écoute en local (`127.0.0.1:8080`). Place un reverse proxy devant, avec HTTPS :
voir `Caddyfile.example` (certificat automatique) `apache2.conf.example` (Apache + certbot) ou `nginx.conf.example` (nginx + certbot). C'est indispensable : ton jeton circule dans
les requêtes, et l'installation de la page sur l'écran d'accueil exige HTTPS.

### 4. Sur le téléphone
1. Ouvre `https://ton-domaine`, entre le **jeton** (valeur de `MOB_TOKEN`).
2. **Ajouter à l'écran d'accueil** (Partager ▸ Sur l'écran d'accueil sur iPhone, menu ▸ Installer sur Android).
3. Dans **Régler mes horaires**, ajuste tes heures et appuie sur **Envoyer une notification test**.

Si le test échoue, regarde les logs du service : le message d'erreur de ntfy y figure.

## Comportement des rappels
- Un rappel part à l'heure exacte, s'il n'est pas déjà coché, **dans les 10 minutes** suivant l'heure prévue.
  Si le serveur était éteint, il ne rattrape pas les anciens créneaux (pas de rafale de notifications).
- Une seule relance par séance, après le délai choisi (10, 15 ou 30 min, ou aucune).
- Jours de pause au bureau, jours de force, heures et rappels on/off se règlent dans l'app.

## Modifier le contenu (exercices, routines, conseils)

Tout le contenu est dans **`content.json`** : tu n'as jamais besoin de toucher au code.

1. Édite `content.json` (n'importe quel éditeur).
2. Vérifie-le : `python3 server.py --check`. Les erreurs sont en français, avec l'exercice ou la phase concernée ;
   le fichier n'est pas modifié.
3. Recharge la page sur le téléphone (tire pour rafraîchir). Pas de redémarrage ni de reconstruction Docker :
   le serveur relit le fichier à chaque requête, et les notifications reprennent tes nouveaux textes dans la minute.

Si le fichier est cassé (virgule oubliée…), **rien ne plante** : la page garde la dernière version valable
et le serveur affiche l'erreur dans ses logs.

| Section | Rôle |
|---|---|
| `tags` | Zones du corps proposées comme filtres dans l'onglet Exercices. |
| `exercises` | La bibliothèque. Clé = identifiant stable (`a-z`, `0-9`, `_`), valeur = `name`, `tags`, `how` (la consigne affichée aussi dans le minuteur), `tip` (astuce, facultative), `from_notes` (`true` = étoile ★). |
| `routines` | `reveil`, `pause`, `coucher` (obligatoires). Chaque étape : `{"ex": "id", "sec": 30}` ; ajoute `"bi": true` pour un exercice fait des deux côtés (`sec` est alors **par côté**, la durée est doublée). |
| `force` | Textes de l'onglet, `rules` (tableau Rythme, Début…), et `phases` avec leurs exercices : `{"ex": "clam", "dose": "3 × 12 / côté"}`. `minutes` et `sub` servent à la frise et au héros. |
| `tips` | Les conseils : une liste de `{"title": "...", "items": ["...", "..."]}`, plus `alert` pour l'encadré en bas. |
| `notifications` | Titre et texte de chaque rappel (`reveil`, `pause`, `force`, `coucher`) et de la relance. |

**Ajouter un exercice à la pause du bureau**
```json
"poignets_cercles": {
  "name": "Cercles de poignets", "tags": ["Bras"], "from_notes": false,
  "how": "Bras tendus, fais des cercles lents avec les poignets.", "tip": "Utile après la souris."
}
```
puis dans `routines.pause.steps` : `{"ex": "poignets_cercles", "sec": 20}`. Si `tags` utilise une zone nouvelle
(ici `Bras`), ajoute-la aussi à la liste `tags` du début, sinon l'exercice n'aura pas de filtre.

**Ajouter un conseil** : ajoute un bloc à la liste `tips`. Le texte est affiché tel quel (le HTML n'est pas interprété).

Repères : les routines visent 5 minutes (`--check` prévient au-delà de 30 s d'écart), un exercice peut figurer
dans plusieurs routines et phases, et retirer un exercice encore utilisé quelque part est signalé comme erreur.
Avec Docker, `content.json` est monté depuis ton dossier (voir `docker-compose.yml`) ; la **première** mise à jour
de ce dossier demande un `docker compose up -d --build`, ensuite tes modifications du contenu sont immédiates.

## Sécurité
- Un seul utilisateur, un seul jeton (`MOB_TOKEN`), comparaison à temps constant, 10 essais ratés par
  10 minutes et par IP avant blocage temporaire.
- Le bouton **Fait** des notifications utilise un lien signé (HMAC) valable pour un seul créneau et
  seulement le jour même ou le lendemain : il ne donne pas accès au reste de l'app. Comme ce lien passe
  par ntfy, un serveur ntfy public peut le voir : le pire qu'il permette est de cocher une séance.
- Pour effacer ton historique : supprime `data/mobilite.db`.

## API
Toutes les routes sauf `/api/health` et `/api/quick` demandent `Authorization: Bearer <MOB_TOKEN>`.

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/health` | vivant ? |
| GET | `/api/state?days=90` | réglages + historique `{jour: {créneau: true}}` |
| PUT | `/api/cfg` | modifier les réglages (valeurs invalides ignorées) |
| POST | `/api/log` | `{day, slot, done}` : cocher ou décocher |
| POST | `/api/test-notify` | envoyer une notification test |
| POST | `/api/quick?day&slot&sig` | lien signé des notifications |

Créneaux : `reveil`, `coucher`, `force`, `pHH:MM` (ex. `p14:00`).

## Tests
```bash
python3 test_server.py     # lance le serveur et un faux ntfy, 17 tests (dont la validation de content.json)
```

## Pistes d'évolution
- Notifications Web Push natives (sans app ntfy) : ajouter `pywebpush`, des clés VAPID et un abonnement
  côté page et service worker.
- Export CSV de l'historique, statistiques mensuelles, plusieurs profils.
- Le contenu vit dans `content.json` ; la mise en page est dans `static/index.html`.
