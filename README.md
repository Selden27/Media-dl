# Media DL

Petite application web qui télécharge n'importe quel lien audio ou vidéo (YouTube, SoundCloud, Vimeo, X, Instagram, liens directs… : tout ce que [yt-dlp](https://github.com/yt-dlp/yt-dlp) sait lire) et range le fichier dans **pCloud**.

- **Audio** : extraction en MP3 (meilleure qualité), titre/artiste et pochette intégrés.
- **Vidéo** : MP4.
- **Playlists** : option « Playlist entière », rangée dans un sous-dossier au nom de la playlist.
- Envoi vers pCloud en streaming (pas de limite de taille liée à la mémoire), fichiers locaux supprimés après envoi.
- yt-dlp mis à jour automatiquement à chaque démarrage du conteneur.

## Déploiement (Coolify)

Application « Public repository », build pack **Dockerfile**, port **8000**.

### Variables d'environnement

| Variable | Rôle | Défaut |
|---|---|---|
| `PCLOUD_USERNAME` / `PCLOUD_PASSWORD` | Identifiants pCloud | — |
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
| `YTDLP_AUTO_UPDATE` | Mettre yt-dlp à jour au démarrage | `true` |
| `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` | Vérifie le jeton Cloudflare Access (recommandé) | — |
| `API_TOKEN` | Jeton alternatif (`Authorization: Bearer …`) | — |

### Sécurité

Sans `CF_ACCESS_*` ni `API_TOKEN`, l'application est ouverte : mets-la derrière Cloudflare Access.
Avec `CF_ACCESS_TEAM_DOMAIN` et `CF_ACCESS_AUD`, chaque requête doit porter un jeton Access valide
(navigateur connecté ou jeton de service), même si quelqu'un contourne Cloudflare.

## API (raccourci iOS, n8n…)

```
POST /api/download   {"url": "...", "mode": "audio|video", "playlist": false, "folder": "/Music"}
GET  /api/jobs       liste des téléchargements
GET  /api/jobs/<id>  état d'un téléchargement
```

Derrière Cloudflare Access avec un jeton de service, ajouter les en-têtes
`CF-Access-Client-Id` et `CF-Access-Client-Secret`.

La page accepte aussi `/?url=<lien>&mode=video` pour préremplir le formulaire.
