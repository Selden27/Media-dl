# Media DL

Petite application web qui télécharge n'importe quel lien audio ou vidéo (YouTube, SoundCloud, Vimeo, X, Instagram, liens directs… : tout ce que [yt-dlp](https://github.com/yt-dlp/yt-dlp) sait lire) et range le fichier dans **pCloud**.

- **Audio** : extraction en MP3 (meilleure qualité), titre/artiste et pochette intégrés.
- **Vidéo** : MP4.
- **Playlists** : option « Playlist entière », rangée dans un sous-dossier au nom de la playlist.
- Envoi vers pCloud en streaming (pas de limite de taille liée à la mémoire), fichiers locaux supprimés après envoi.
- yt-dlp mis à jour automatiquement à chaque démarrage du conteneur.

## Tags musicaux (audio)

Chaque MP3 est identifié sur MusicBrainz (titre, artiste, album, n° de piste, année, label, ISRC), reçoit sa
pochette officielle (Cover Art Archive, sinon la miniature recadrée en carré) et un genre unique choisi dans
`app/data/genres.txt` (correspondances dans `app/data/genre_map.txt`). Tags écrits en ID3v2.3.
Si la confiance est suffisante, le fichier est rangé selon `MUSIC_PATH_TEMPLATE` ; sinon il part dans `_A trier`.
Réglages persistants : `/data/library.json`. Plan complet : [docs/tags-plan.md](docs/tags-plan.md).

## Pages

- `/` : téléchargement, cartes récapitulatives des derniers téléchargements (bouton « Modifier »).
- `/valider` : morceaux à vérifier (identification incertaine, genre manquant, modification, nettoyage), regroupés par album.
- `/genres` : liste de genres, correspondances, artistes mémorisés.
- `/nettoyer` : nettoyage de la bibliothèque existante (pochettes, tags, genre, renommage), 100 fichiers max par passage,
  avec journal et annulation. Les tags sont modifiés sans toucher aux autres informations du fichier (paroles, commentaires…).

## Déploiement (Coolify)

Application « Public repository », build pack **Dockerfile**, port **8000**.

### Variables d'environnement

| Variable | Rôle | Défaut |
|---|---|---|
| `PCLOUD_CLIENT_ID` / `PCLOUD_CLIENT_SECRET` | App OAuth pCloud (recommandé, compatible double authentification) : active le bouton « Connecter pCloud » | — |
| `PUBLIC_URL` | Adresse publique, pour le retour OAuth (ex. `https://dl.mycloudy.fr`) | `https://<host>` |
| `PCLOUD_ACCESS_TOKEN` | Jeton pCloud (voir « Connexion pCloud par jeton ») | — |
| `PCLOUD_USERNAME` / `PCLOUD_PASSWORD` | Identifiants pCloud (comptes sans double authentification) | — |
| `PCLOUD_AUTH_TOKEN` | Alternative : jeton `auth` pCloud | — |
| `PCLOUD_ACCESS_TOKEN` | Alternative : jeton OAuth pCloud | — |
| `PCLOUD_REGION` | `eu` ou `us` (détecté automatiquement si vide) | auto |
| `PCLOUD_AUDIO_FOLDER` | Dossier pCloud pour l'audio | `/Music` |
| `PCLOUD_VIDEO_FOLDER` | Dossier pCloud pour la vidéo | `/Videos` |
| `AUDIO_FORMAT` | `mp3`, `m4a`, `opus`, `flac`… | `mp3` |
| `AUDIO_QUALITY` | `0` (meilleur VBR) ou débit, ex. `320` | `0` |
| `VIDEO_MAX_HEIGHT` | Limite de résolution, ex. `1080` | meilleure |
| `MAX_CONCURRENT` | Téléchargements simultanés | `2` |
| `YTDLP_COOKIES` | Contenu d'un fichier cookies (format Netscape), utile si YouTube bloque le serveur (« not a bot ») | — |
| `YTDLP_PROXY` | Proxy pour les téléchargements (ex. `socks5h://warp:1080`), si les sites bloquent l'IP du serveur. pCloud n'y passe pas | — |
| `YTDLP_AUTO_UPDATE` | Mettre yt-dlp à jour au démarrage | `true` |
| `MUSIC_TAGGING` | Identifier les morceaux audio (MusicBrainz), écrire les tags et ranger selon le modèle | `true` |
| `MUSIC_PATH_TEMPLATE` | Modèle de chemin sous le dossier audio | `%albumartist%/%album%/$num(%track%,2) - %title%` |
| `MUSIC_AUTO_CONFIDENCE` | Confiance minimale pour ranger automatiquement (sinon `_A trier`) | `0.85` |
| `MUSIC_SINGLES_FOLDER` / `MUSIC_TO_SORT_FOLDER` | Noms des dossiers « sans album » et « à vérifier » | `Singles` / `_A trier` |
| `ACOUSTID_API_KEY` | Reconnaissance par empreinte audio (morceaux mal titrés) | — |
| `DISCOGS_TOKEN` | Identification de secours et genres/styles Discogs | — |
| `LASTFM_API_KEY` | Genres d'après les tags Last.fm | — |
| `LYRICS` | Ajouter les paroles (LRCLIB) | `true` |
| `MUSIC_FOLDER_COVER` | Déposer cover.jpg dans les dossiers d'album | `true` |
| `DUPLICATES` | Morceau déjà présent (API sans choix) : `skip`, `replace` ou `keep` | `skip` |
| `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` | Vérifie le jeton Cloudflare Access (recommandé) | — |
| `API_TOKEN` | Jeton alternatif (`Authorization: Bearer …`) | — |

### Sécurité

Sans `CF_ACCESS_*` ni `API_TOKEN`, l'application est ouverte : mets-la derrière Cloudflare Access.
Avec `CF_ACCESS_TEAM_DOMAIN` et `CF_ACCESS_AUD`, chaque requête doit porter un jeton Access valide
(navigateur connecté ou jeton de service), même si quelqu'un contourne Cloudflare.

## Connexion pCloud par jeton (le plus simple)

La connexion par e-mail / mot de passe depuis le serveur peut être refusée par pCloud
(erreur 1022 « Please provide 'code' » : vérification de nouvel appareil ou double authentification).
Pour l'éviter, utilise un jeton :

1. Installe [rclone](https://rclone.org/downloads/) sur ton ordinateur et lance `rclone authorize "pcloud"`.
2. Connecte-toi à pCloud dans le navigateur qui s'ouvre, puis autorise l'accès.
3. Copie la valeur `access_token` affichée et mets-la dans `PCLOUD_ACCESS_TOKEN`. Redéploie.

## Connexion pCloud (OAuth, avec ta propre application pCloud)

1. Sur https://docs.pcloud.com/my_apps/ crée une application, avec comme Redirect URI
   `https://dl.mycloudy.fr/pcloud/callback`.
2. Mets son Client ID et son Client Secret dans `PCLOUD_CLIENT_ID` / `PCLOUD_CLIENT_SECRET`.
3. Ouvre l'application et clique sur « Connecter pCloud ». Le jeton (sans expiration) est gardé dans
   `/data/pcloud.json` : monte un volume persistant sur `/data`.

## API (raccourci iOS, n8n…)

```
POST /api/download   {"url": "...", "mode": "audio|video", "playlist": false, "folder": "/Music"}
GET  /api/jobs       liste des téléchargements
GET  /api/jobs/<id>  état d'un téléchargement
```

Derrière Cloudflare Access avec un jeton de service, ajouter les en-têtes
`CF-Access-Client-Id` et `CF-Access-Client-Secret`.

La page accepte aussi `/?url=<lien>&mode=video` pour préremplir le formulaire.

## Si un site bloque l'IP du serveur

Bandcamp, YouTube… bloquent souvent les IP de datacenter (« Client Challenge », « not a bot »).
Le fichier `warp/docker-compose.yml` lance un proxy Cloudflare WARP gratuit : déploie-le comme
service Coolify (avec « Connect to Predefined Network » activé), puis mets
`YTDLP_PROXY=socks5h://<nom-du-conteneur-warp>:1080` dans l'application.
