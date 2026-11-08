# Plan : tags musicaux, rangement et nettoyage

Décisions prises avec Olivier (novembre 2026). Lecteur cible : pCloud + **Evermusic Pro** (iPhone).

## Décisions

| Sujet | Choix |
|---|---|
| Version des tags | **ID3v2.3** (la plus compatible, lue par Evermusic) |
| Format audio | **MP3 V0** (`AUDIO_QUALITY=0`), pas de changement : la source (YouTube, SoundCloud) limite la qualité |
| Chemin | `/Music/%albumartist%/%album%/$num(%track%,2) - %title%` (variable `MUSIC_PATH_TEMPLATE`, relatif au dossier choisi) |
| Pas d'album / single | `/Music/%albumartist%/Singles/%title%` |
| Pas de n° de piste | pas de préfixe `00 -` |
| Genre | **un seul** par morceau (Evermusic n'en lit qu'un), choisi dans la liste d'Olivier |
| Meditation / Meditative | restent séparés. Meditation = méditation guidée, avec voix. Meditative = musique pour méditer |
| Validation | automatique si la confiance est élevée. Sinon `/Music/_A trier/` avec les tags trouvés, et le morceau reste « à valider » sur le site |
| Pochette | pochette officielle (Cover Art Archive), sinon une autre source, sinon miniature recadrée en carré ; JPEG ~1000 px ; on garde une vraie pochette déjà présente |
| Nettoyage de l'existant | fonction séparée, **100 morceaux max** par passage |

## Liste de genres (départ, modifiable)

Genres d'Olivier : Ambient · Ecstatic Dance · Experimental · Mantra · Medicine Music · Meditation · Meditative

Ajouts proposés :
- Sacré / dévotionnel : Kirtan · Devotional · Chant · Shamanic · Sound Healing
- Instruments : Handpan · Piano · Neo-Classical · Acoustic
- Danse / électro organique : Organic House · Downtempo · Tribal · World Electronica · Chillout
- Calme : New Age · Nature Sounds · Sleep · Yoga
- Racines : World · Folk · Afro · Latin · Celtic
- Général : Pop · Rock · Electronic · Hip-Hop · Jazz · Classical · Soundtrack · Chanson française

Fichiers : `app/data/genres.txt` (liste) et `app/data/genre_map.txt` (correspondances genre des bases → genre de la liste). Copiés au premier démarrage dans `/data/library.json`, puis modifiables depuis le site.

## Étapes

### Étape 1 : tags et rangement des nouveaux téléchargements ✅ (livrée)
- Identification : infos fournies par le site (yt-dlp), nettoyage du titre (« (Official Video) », « - Topic »…), recherche **MusicBrainz** (1 requête/s, sans clé).
- Score de confiance : titre, artiste, durée, statut officiel, type de sortie (album > EP > single > compilation).
- Tags ID3v2.3 avec mutagen : titre, artiste, artiste de l'album, album, piste/total, disque/total, année, genre, label, ISRC, compilation, identifiants MusicBrainz, lien d'origine en commentaire, pochette.
- Pochette : Cover Art Archive, sinon miniature recadrée en carré.
- Genre : genres MusicBrainz → correspondances → liste ; mémoire par artiste ; règle Meditation / Meditative.
- Chemin selon le modèle ; noms de fichiers nettoyés.
- Stockage persistant : `/data/library.json`.

### Étape 2 : validation et suggestions de genres
- Écran « À valider » : tags modifiables, choix de l'édition de l'album, pochettes au choix, genre (liste déroulante, choix unique), aperçu du chemin.
- Validation : déplacement du fichier de `_A trier` vers son chemin final, et mémoire par artiste mise à jour.
- Suggestion d'un nouveau genre quand ≥ 2 sources s'accordent sur un même morceau, ou quand il revient sur 3 morceaux d'au moins 2 artistes. Trois choix : **Ajouter** / **Associer à un genre existant** / **Ignorer**. Section « Genres suggérés » pour les suggestions en attente.
- Proposer la mise à jour des morceaux déjà enregistrés quand un genre est ajouté plus tard.

### Étape 3 : genres et cas difficiles ✅ (livrée)
- **Discogs** (genres et styles) et **Last.fm** (tags) pour mieux suggérer le genre.
- **AcoustID** (empreinte audio `fpcalc`) pour les titres mal nommés.
- Clés à saisir dans Coolify : `ACOUSTID_API_KEY`, `DISCOGS_TOKEN`, `LASTFM_API_KEY`.

### Étape 4 : nettoyage de la bibliothèque existante
- Choix d'un dossier pCloud et des corrections : pochettes, tags, genre, renommage et rangement.
- Analyse sans rien modifier : lecture du seul début des fichiers (tags et pochette), puis identification.
- Rapport avant/après : la confiance élevée est pré-cochée, le reste est à valider.
- Application : renommage et déplacement directement dans pCloud ; tags réécrits sans réencodage de l'audio.
- Journal des modifications + « Annuler ce nettoyage » ; cas douteux signalés, jamais écrasés.
- 100 morceaux max par passage ; les fichiers déjà propres sont sautés au passage suivant.

### Étape 5 : options
- Paroles (LRCLIB), ReplayGain, gestion des doublons (sauter / remplacer / renommer), `cover.jpg` par dossier d'album (à tester avec Evermusic).
