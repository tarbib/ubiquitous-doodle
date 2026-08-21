# 🚂 Gares — Bot Telegram SNCF

Ce bot Telegram open source permet de consulter facilement les horaires des trains SNCF et de recevoir des alertes de retard en temps réel.

## 🌟 Fonctionnalités

* **Recherche intuitive** de gares avec gestion automatique des gares récentes et favorites.
* **Affichage clair** du tableau des départs pour le jour même, le lendemain ou une date ultérieure.
* **Suivi personnalisé** permettant de surveiller jusqu'à 5 trains simultanément par utilisateur.
* **Notifications automatiques** envoyées si le retard dépasse 3 minutes ou si le train disparaît de l'affichage.

## 🚀 Déploiement

1. **Prérequis** : Le bot nécessite un Token API SNCF (Navitia) et un Token Telegram fourni par BotFather.
2. **Configuration** : Créez un fichier `.env` centralisant les identifiants (`TELEGRAM_TOKEN`, `SNCF_TOKEN`, `ALLOWED_USER_IDS`, `TZ`).
3. **Lancement** : Le projet inclut un fichier `docker-compose.yml` prêt à l'emploi. Lancez simplement la commande `docker compose up -d --build`.

## 💻 Commandes

| Commande | Action |
| :--- | :--- |
| *Texte libre* | Recherche une gare par son nom. |
| `/start` | Initialise le bot et affiche les favoris ou les recherches récentes. |
| `/gares` | Ouvre la liste des gares favorites pour les consulter ou les retirer. |
| `/suivis` | Affiche les trains actuellement surveillés avec une option pour annuler l'alerte. |

## 🔒 Sécurité et Technique

* **Quotas** : Le programme respecte les quotas de l'API avec une limite stricte de 25 requêtes par minute.
* **Confidentialité** : Les clés d'authentification sont masquées et ne transitent jamais dans les requêtes URL ou les journaux d'erreurs.
* **Sécurité Docker** : Le conteneur limite les privilèges systèmes via les paramètres de sécurité Docker (`cap_drop`, `no-new-privileges`).
* **Technologies** : Le code est développé en Python 3.11 en s'appuyant sur les librairies `httpx` et `python-telegram-bot`.
