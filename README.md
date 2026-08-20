# Gares — bot Telegram SNCF

Choisir un train, se faire prévenir en cas de retard.

**Ouvre `prototype.html` avant le code** : six étapes cliquables, chacune avec
la décision d'interface qui la justifie.

## Le parcours

```text
1  gare        « rennes »                    → désambiguïsation si besoin
2  jour        [Aujourd'hui] [Demain] [Autre date]
3  tableau     6 départs, un bouton par train
4  train       fiche + 🔔 Suivre ce train
⟳  alertes     retard · retour à l'heure · disparition du tableau
```

L'unité du produit est un **train**. L'alerte est la seule chose qu'une
application ne fait pas — venir à toi. Tout le reste est du chemin vers elle.

## Les décisions, et pourquoi

**Les boutons portent l'heure et le numéro**, pas un index. Sélectionner
« le train n° 3 » obligerait à faire l'aller-retour entre le tableau et une
liste. On touche directement la ligne qu'on regarde.

**Les départs sont triés sur l'heure réelle**, pas théorique. Sans cela un
train retardé se déplace dans le tableau mais pas dans les boutons, et le
bouton ne désigne plus sa ligne. Le tableau et les boutons partagent le même
tri, par construction.

**« Suivre », pas « épingler ».** Épingler range un raccourci ; suivre promet
quelque chose. Le verbe engage le bot.

**Aujourd'hui est le premier bouton.** La question de la date est posée, mais
la réponse la plus probable est déjà à portée de pouce.

**Aucun appel API tant que le départ est à plus de 90 minutes.** Un train suivi
la veille ne coûte rien jusqu'au matin. Le bot l'annonce à l'activation, ce qui
est à la fois honnête et rassurant sur le fait que le suivi tourne.

**Une alerte n'arrive jamais seule.** Un retard sans solution ne fait
qu'avancer l'angoisse d'une minute. Le prochain train dans la même direction
est joint au message, extrait de la même réponse d'API — aucun appel de plus.

**Une disparition du tableau n'est pas une preuve de suppression**, et le
message le dit ainsi plutôt que d'affirmer une annulation.

**Pas deux alertes pour le même état.** Un retard n'est signalé que s'il change,
et seulement à partir de 3 min — la gigue d'une minute ne réveille personne.

**Les suivis survivent au redémarrage.** Ils sont relus au démarrage et leurs
tâches reprogrammées : sans cela, une alerte promise disparaîtrait au premier
`docker compose up`.

**Favoris et récents sont deux listes distinctes.** Les récentes se
remplissent seules et s'oublient (3 gares) ; un favori est un choix explicite
et reste jusqu'à ce qu'on le retire. `/start` affiche les favoris en tête,
étoilés, puis les récentes qui n'y figurent pas déjà.

## Mise en route & Déploiement

L'architecture est unifiée avec un seul fichier `docker-compose.yml` conçu pour le développement local et la production sur VPS (via le GitHub Container Registry).

### 1. Prérequis et Configuration
1. **Token API SNCF (gratuit)** : https://numerique.sncf.com/startup/api/token-developpeur/
   (150 000 requêtes/mois, 5 000/jour. Compter 5 min avant qu'il soit actif).
2. **Bot Telegram** : @BotFather → `/newbot`.
3. **Variables d'environnement** :
   ```bash
   cp .env.example .env
   chmod 600 .env
   ```
   Renseignez `TELEGRAM_TOKEN` et `SNCF_TOKEN` dans le fichier `.env`. Vous pouvez aussi y définir `ALLOWED_USER_IDS` (pour restreindre l'usage à certains comptes) et `TZ` (fuseau horaire par défaut : Europe/Paris).

### 2. Lancement en Local (Développement)
Le fichier `docker-compose.yml` intègre une directive `build: .` pour compiler automatiquement votre code à la volée.
```bash
docker compose up -d --build
docker compose logs -f
```

### 3. Lancement en Production (VPS)
Le projet est pensé pour être déployé via GitHub Actions. À chaque push, une image Docker est compilée et poussée sur le GHCR (`ghcr.io/votre-compte/gares_bot`).
Sur votre serveur, utilisez le même fichier `docker-compose.yml`. Grâce à la directive `image:`, exécutez simplement :
```bash
# Télécharge la dernière version compilée depuis le registre
docker compose pull

# Relance le bot en conservant les données (state.json) via le volume local
docker compose up -d
```

## Commandes

| | |
|---|---|
| *texte libre* | nom d'une gare |
| `/suivis` | trains surveillés, et bouton pour arrêter |
| `/favoris` | gares favorites, et bouton pour en retirer |
| `/start` | recommencer (propose les favoris, puis les dernières gares) |

## Quota

| Action | Appels |
|---|---|
| Recherche de gare | 1 |
| Tableau des départs | 1 (mis en cache 45 s) |
| Ouvrir une fiche train | 1, souvent 0 grâce au cache |
| Train suivi, à plus de 90 min | **0** |
| Train suivi, dernière heure et demie | 1 toutes les 3 min, soit ~30 au total |

5 suivis simultanés maximum par personne, 25 requêtes/minute. Un usage
personnel reste loin des 5 000 appels/jour ; un bot laissé public peut y
arriver — d'où `ALLOWED_USER_IDS`.

## Limites des données

- **Suppressions** : un train supprimé disparaît du tableau au lieu d'y figurer
  barré. Le bot signale la disparition sans affirmer l'annulation.
- **Numéro de voie** : non exposé de façon fiable par l'API, donc non affiché.
- **Identifiants de gare** : Navitia ne garantit pas leur stabilité dans le
  temps ; un 404 invite à retaper le nom de la gare.
- **Horizon** : 180 jours, mais les horaires ne sont réellement publiés que
  quelques mois à l'avance.
- **Périmètre** : couverture `sncf` (trains). Pour métros et bus, viser une
  autre couverture Navitia (`fr-idf`) ou l'API Île-de-France Mobilités.

## Sécurité

Lecture seule sur donnée publique. Le bot applique les standards de sécurité Docker en production (`cap_drop`, `no-new-privileges`) et suit deux règles de code :

- Le `SNCF_TOKEN` ne quitte pas le conteneur ; les logs `httpx` sont en
  `WARNING`, car les URLs appelées transiteraient par les journaux.
- La saisie utilisateur ne construit jamais un chemin d'URL : elle passe en
  paramètre de requête, et seuls les identifiants renvoyés par l'API sont
  réutilisés ensuite.

## Ce qui reste à vérifier au premier lancement

Aucun appel réseau n'a pu être testé à l'écriture. Ont été vérifiés hors ligne :
le rendu, le tri, l'alignement boutons/lignes, l'analyse des dates, les
fenêtres de suivi et le cycle complet des alertes (retard, aggravation, retour
à l'heure, disparition, anti-répétition). **Restent à confirmer en conditions
réelles** : la forme exacte des réponses de l'API et le comportement de
`trip_short_name` selon les transporteurs — c'est la clé sur laquelle repose
l'identification d'un train d'un appel à l'autre.