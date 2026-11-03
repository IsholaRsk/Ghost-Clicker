# Ghost-Clicker — Web Service Docker

Ghost-Clicker est un outil de **test fonctionnel autorisé**. Chaque lancement utilise l’un des trois modes configurables : un nombre fixe de répétitions, un total tiré dans un intervalle défini, ou une exécution continue jusqu’à l’arrêt manuel. Pour chaque visite, il :

1. crée une nouvelle connexion à la passerelle rotative, sans effectuer de contrôle IP préalable ;
2. ouvre immédiatement la page cible dans Chromium headless ;
3. repère le premier libellé visible exact et mémorise sa position dans le bouton ;
4. clique une première fois ;
5. si aucun second libellé n’est configuré, attend immédiatement soit le changement d’URL de l’onglet courant, soit l’ouverture d’un nouvel onglet ;
6. si un second libellé est configuré, attend 500 ms, clique une seconde fois exactement au même emplacement, puis accepte ces mêmes deux formes de navigation ;
7. enregistre le numéro de répétition, le proxy utilisé, l’URL finale, le statut et l’erreur éventuelle ;
8. ferme complètement Chromium avant la visite suivante.

Il effectue exactement un ou deux clics selon la ligne cible, ne remplit aucun formulaire et n’imite pas un comportement humain. Utilisez-le uniquement sur des domaines que vous êtes autorisé à tester.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/IsholaRsk/Ghost-Clicker)

## Architecture

```text
Ghost-Clicker/
├── main.py           # moteur Playwright
├── web.py            # interface web et planification
├── database.py       # client de persistance Supabase
├── supabase/
│   └── migrations/   # schéma SQL versionné
├── Dockerfile        # Chromium/Playwright dans Docker
├── render.yaml       # Blueprint Render
├── .env.example      # modèle sans secret
├── .dockerignore
├── .gitignore
├── requirements.txt
└── tests/
    ├── test_main.py
    └── test_web.py
```

## Déploiement Render

### 1. Créer le service

Cliquez sur **Deploy to Render** ci-dessus, ou choisissez **New → Blueprint** dans Render et sélectionnez ce dépôt.

Le runtime est :

```text
Docker
```

Le Blueprint utilise le plan Render **Starter** afin que la planification interne reste active. Un Web Service gratuit peut se mettre en veille et ne convient donc qu’aux lancements manuels.

### 2. Relier Supabase

Le projet Supabase dédié contient les données persistantes suivantes :

- la configuration, les cibles et les pseudos de passerelles dans `ghost_clicker_settings` ;
- les identifiants de passerelles chiffrés dans Supabase Vault, associés par `ghost_clicker_gateway_secrets` ;
- le résumé de chaque exécution dans `ghost_clicker_reports` ;
- chaque visite, sans troncature de l’historique, dans `ghost_clicker_visits` ;
- un journal d’actions non sensible dans `ghost_clicker_events`.

Le schéma versionné se trouve dans `supabase/migrations/`. Le Blueprint renseigne déjà `SUPABASE_URL`. Dans **Render → Environment**, ajoutez la valeur de `SUPABASE_SECRET_KEY` avec la clé serveur dédiée au bot. Cette clé ne doit jamais être placée dans Git, dans le navigateur ou dans un message.

Les tables utilisent Row Level Security et n’accordent aucun accès aux rôles publics `anon` et `authenticated`. Seule la clé serveur peut lire ou écrire les données du bot. Si Supabase est momentanément indisponible, le service garde un cache local de secours et affiche l’état de la base dans la console.

### 3. Préparer la passerelle et les cibles

Chaque passerelle Rayobyte utilise exactement cette valeur, sans lien `mailto:` ni mise en forme Markdown :

```text
username:password@host:port
```

La bibliothèque de passerelles accepte jusqu’à 20 entrées avec un pseudo. Une seule entrée est active à la fois. Le pseudo, l’identifiant interne et la sélection active persistent dans les réglages Supabase. Les identifiants Rayobyte sont conservés durablement et chiffrés dans **Supabase Vault** ; ils ne sont jamais inclus dans les réglages, les réponses de l’API, les journaux ou Git. Un cache privé en mode `0600` dans `/tmp/ghost-clicker-proxy-gateways.json` sert uniquement à l’exécution locale. Après leur première saisie avec cette version, les passerelles restent disponibles après les redémarrages et redéploiements. La passerelle active doit attribuer une nouvelle IP à chaque connexion. N’utilisez jamais un identifiant déjà publié dans une conversation : réinitialisez-le d’abord chez le fournisseur.

Les cibles ne nécessitent plus de fichier texte. La **Bibliothèque d’URL** permet d’ajouter chaque cible avec :

- un pseudo lisible ;
- l’URL publique ;
- le premier libellé exact ;
- un éventuel second libellé ;
- une case indiquant si la cible doit participer aux prochains lancements.

Sans second libellé, le bot clique une fois puis attend immédiatement la navigation. Avec un second libellé, il clique deux fois au même emplacement, avec une pause de 500 ms. Dans les deux cas, une modification de l’URL actuelle ou l’ouverture d’un nouvel onglet est acceptée. Le second libellé sélectionne le mode à deux clics et reste une référence : il n’est pas recherché dans la page.

`ADMIN_TOKEN` est généré automatiquement par Render. Les anciens identifiants publiés dans une conversation doivent être réinitialisés avant utilisation.

### 4. Se connecter au bot

Après le build :

1. ouvrez **Render → Environment** ;
2. copiez la valeur de `ADMIN_TOKEN` ;
3. ouvrez l’URL publique du service ;
4. utilisez :
   - utilisateur : `admin` ;
   - mot de passe : valeur de `ADMIN_TOKEN`.

## Gérer la configuration en ligne

Après connexion, l’interface se présente comme une application sombre et lisible en cinq pages : **Accueil**, **URL**, **Passerelles**, **Résultats** et **Réglages**. La barre de navigation à icônes reste fixée en bas. Le changement de page se fait obligatoirement avec ses boutons — aucun balayage horizontal n’est utilisé — et ne provoque pas de rechargement complet.

1. ouvrez **Passerelles**, ajoutez une ou plusieurs entrées avec leur pseudo et activez-en exactement une ;
2. ouvrez **URL** et ajoutez les cibles une par une dans la bibliothèque ;
3. utilisez les pseudos pour retrouver, consulter, modifier ou supprimer chaque entrée ;
4. cochez les URL à utiliser, ou employez **Tout sélectionner** et **Tout désélectionner** ;
5. revenez à **Accueil** et lancez le scénario : les exécutions manuelles et planifiées utilisent uniquement les URL cochées et la passerelle active.

Le premier libellé doit correspondre exactement au texte visible initial du bouton. Omettez le second libellé lorsque le premier clic redirige directement. Ajoutez-le pour demander un deuxième clic au même emplacement. Aucun texte traduit ni changement du DOM n’est utilisé pour déclencher ce deuxième clic.

Les formulaires, la sélection, le lancement, l’arrêt, le statut et les résultats utilisent l’API JSON intégrée : la page d’administration n’est pas entièrement rechargée après chaque action.

## Lancer le bot

Deux modes sont disponibles :

- **Lancer maintenant** : exécution manuelle ;
- **Planification** : intervalle configurable entre 60 et 10080 minutes.

Le mode de répétition est choisi dans l’interface :

- **Nombre fixe** : exécute exactement le nombre indiqué ;
- **Intervalle** : tire un nombre entre le minimum et le maximum indiqués ;
- **Continu** : fonctionne par cycles successifs de 5 minutes. Au début de chaque cycle, un nouveau nombre de répétitions est tiré entre le minimum et le maximum configurés. Le bot continue jusqu’au clic sur **Arrêter** et termine proprement la visite en cours.

Chaque cible cochée est visitée une fois par répétition ; les cibles non sélectionnées restent enregistrées mais ne sont pas ouvertes. Par exemple, 200 répétitions et 3 cibles sélectionnées produisent 600 visites. En mode continu, les 1 000 résultats les plus récents restent en mémoire afin de borner l’utilisation du processus, mais chaque visite terminée est enregistrée séparément dans Supabase et reste disponible dans l’export complet.

Une seule exécution peut fonctionner à la fois. Les cibles, paramètres, passerelles complètes, sélection active, exécutions, visites et informations de planification sont persistants et survivent aux redémarrages et redéploiements Render. Les identifiants Rayobyte sont chiffrés séparément dans Supabase Vault et ne sont jamais exposés dans les métadonnées de configuration.

## Résultats

L’interface affiche :

- date et source de l’exécution ;
- pseudo et identifiant de la cible ;
- mode d’exécution, numéro de répétition et total choisi lorsqu’il existe ;
- nom de la passerelle et ouverture de connexion ;
- URL initiale ;
- premier libellé et éventuel second libellé du bouton ;
- URL après redirection ;
- réussite ou échec ;
- message d’erreur.

Le rapport persistant est enregistré après chaque visite : le tableau et le CSV téléchargeable se mettent donc à jour pendant que l’exécution est encore en cours. Une même exécution conserve un seul résumé progressif au lieu de créer des doublons, tandis que chaque visite dispose de sa propre ligne durable dans Supabase. L’interface charge un historique récent pour rester réactive ; le CSV demande l’ensemble des rapports et visites disponibles.

Depuis la console, l’administrateur peut supprimer une visite, plusieurs éléments cochés, une exécution complète — y compris une exécution sans visite — ou tout l’historique. Ces suppressions sont effectuées directement dans Supabase et restent disponibles pendant que le bot fonctionne ; la suppression d’une exécution efface aussi ses visites associées. Une exécution toujours active peut naturellement enregistrer de nouvelles visites après la suppression.

## Navigateur sur Render

Render n’a pas d’écran graphique. Chromium utilise donc obligatoirement :

```dotenv
HEADLESS=true
```

Le navigateur s’ouvre réellement dans le conteneur, mais aucune fenêtre n’est visible. Les résultats sont consultables dans l’interface web.

## Exécution locale facultative

Sous Windows :

```powershell
py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
notepad .env
```

Pour utiliser `main.py` avec une seule URL, créez `proxy-gateway.txt` avec l’unique passerelle Rayobyte indiquée plus haut, puis remplissez aussi :

```dotenv
PROXY_GATEWAY_FILE="proxy-gateway.txt"
TARGET_URL="https://site-autorise.example/page"
BUTTON_TEXT="Continuer"
# Laissez vide pour un clic unique avec redirection directe.
SECOND_BUTTON_TEXT="Confirmer"
EXECUTION_MODE="fixed" # fixed, range ou continuous
REPETITION_COUNT=10
REPETITION_MIN=5
REPETITION_MAX=20
HEADLESS=false
```

`proxy-gateway.txt` est ignoré par Git et doit rester local. Puis :

```powershell
python .\main.py
```

## Sécurité

- authentification HTTP Basic avec `ADMIN_TOKEN` ;
- protection CSRF des formulaires et mutations de l’API JSON ;
- jeton administrateur uniquement dans les variables Render ou `.env` ;
- clé serveur Supabase uniquement dans les variables secrètes Render ou `.env` ;
- RLS activée sur toutes les tables, sans politique d’accès public ;
- passerelles persistées durablement, avec identifiants chiffrés dans Supabase Vault et cache d’exécution privé en mode `0600` dans `/tmp` ;
- rejet des URL locales, privées et réservées ;
- maximum de 50 URL ;
- exactement un clic par visite sans second libellé, ou deux clics au même emplacement lorsqu’il est configuré ;
- fréquence planifiée minimale de 60 minutes ;
- nouvelle connexion à la passerelle rotative et nouveau navigateur pour chaque visite ;
- aucune requête de contrôle IP avant l’ouverture de la cible ;
- nombre fixe, intervalle personnalisé ou exécution continue avec arrêt manuel ;
- aucune technique de dissimulation ou simulation de comportement humain.

## Tests

```powershell
python -m unittest discover -s tests -v
```
