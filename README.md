# Ghost-Clicker — Web Service Docker

Ghost-Clicker est un outil de **test fonctionnel autorisé**. Il propose trois modes configurables : un nombre fixe de répétitions, une campagne d’exécutions réparties aléatoirement sur plusieurs jours, ou une exécution continue jusqu’à l’arrêt manuel. Pour chaque visite, il :

1. vérifie une fois par exécution l’IP de sortie, le pays et la réputation de chaque passerelle, puis ne garde que les conformes ;
2. ouvre immédiatement la page cible dans Google Chrome piloté par Playwright, sans nouvelle requête de contrôle ;
3. si aucun premier libellé n’est configuré, ne recherche et ne clique aucun bouton, mais attend une redirection automatique ou l’ouverture d’un nouvel onglet ;
4. sinon, repère le premier libellé visible exact, mémorise sa position et clique une première fois ;
5. si aucun second libellé n’est configuré, attend immédiatement soit le changement d’URL de l’onglet courant, soit l’ouverture d’un nouvel onglet ;
6. si un second libellé est configuré, attend 500 ms, clique une seconde fois exactement au même emplacement, puis accepte ces mêmes deux formes de navigation ;
7. enregistre le numéro de répétition, la passerelle utilisée (alias), l’IP de sortie, la chaîne de redirections complète, l’URL finale, le statut et l’erreur éventuelle ;
8. ferme complètement Google Chrome avant la visite suivante.

Il effectue exactement zéro, un ou deux clics selon la cible, ne remplit aucun formulaire et n’imite pas un comportement humain. Utilisez-le uniquement sur des domaines que vous êtes autorisé à tester.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/IsholaRsk/Ghost-Clicker)

## Architecture

```text
Ghost-Clicker/
├── main.py           # moteur Playwright
├── web.py            # API Flask, interface web et planification
├── database.py       # client de persistance Supabase
├── frontend/src/     # tableau de bord React sombre et responsive
├── static/           # bundle React compilé servi par Flask
├── package.json      # dépendances et scripts du tableau de bord
├── supabase/
│   └── migrations/   # schéma SQL versionné
├── Dockerfile        # Google Chrome/Playwright dans Docker
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
- les 500 visites les plus récentes dans `ghost_clicker_visits`, les plus anciennes étant supprimées automatiquement ;
- un journal d’actions non sensible dans `ghost_clicker_events`.

Le schéma versionné se trouve dans `supabase/migrations/`. Le Blueprint renseigne déjà `SUPABASE_URL`. Dans **Render → Environment**, ajoutez la valeur de `SUPABASE_SECRET_KEY` avec la clé serveur dédiée au bot. Cette clé ne doit jamais être placée dans Git, dans le navigateur ou dans un message.

Les tables utilisent Row Level Security et n’accordent aucun accès aux rôles publics `anon` et `authenticated`. Seule la clé serveur peut lire ou écrire les données du bot. Si Supabase est momentanément indisponible, le service garde un cache local de secours et affiche l’état de la base dans la console.

### 3. Préparer la passerelle et les cibles

Chaque passerelle Rayobyte peut être collée comme valeur brute :

```text
username:password-country-CA@us-east.gw.rayobyte.com:8000
```

ou comme commande `curl` complète fournie par Rayobyte :

```text
curl https://rayobyte.com/ -x username:password-country-US@us-east.gw.rayobyte.com:8000
```

Les options `-x`, `-xVALEUR`, `--proxy` et `--proxy=VALEUR` sont reconnues sans jamais exécuter la commande. Chaque passerelle conserve un pays fixe et doit obligatoirement terminer son mot de passe de routage par `-country-US`, `-country-GB` pour le Royaume-Uni, ou `-country-CA`. Toute passerelle sans pays, ou avec un autre pays, est refusée avant son enregistrement et n’entre jamais dans la rotation. Au début de chaque exécution, le bot ouvre un écho d’IP **à travers chaque passerelle** : l’IP de sortie réelle est relevée, comparée au pays déclaré, puis contrôlée sur des listes noires DNS. Seules les passerelles conformes sont utilisées ; les autres sont affichées comme refusées avec leur motif, sans jamais exposer d’identifiants. Cette contrainte force le routage demandé à Rayobyte sans effectuer de requête de vérification IP préalable. Lorsqu’une messagerie transforme automatiquement le proxy de la commande `curl` en lien Markdown `mailto:`, Ghost Clicker retire uniquement cet emballage après avoir vérifié sa cohérence. Une valeur Markdown isolée, hors d’une commande `curl`, reste refusée.

Le contrôle d’entrée se règle avec `PROXY_VERIFICATION_ENABLED` (activé par défaut), `IP_ECHO_URLS` (échos JSON interrogés dans l’ordre), `IP_COUNTRY_URL_TEMPLATE` et `DNSBL_ZONES`. Une panne d’écho ou de DNS ne casse jamais le bot : la passerelle concernée est simplement écartée, et l’exécution s’arrête avec un message clair s’il n’en reste aucune de conforme.

La bibliothèque de passerelles accepte jusqu’à 20 entrées avec un pseudo. Toutes les passerelles valides tournent automatiquement : chaque visite tire une passerelle éligible au hasard, sans sélection manuelle. Le pseudo et l’identifiant interne persistent dans les réglages Supabase. Les identifiants Rayobyte sont conservés durablement et chiffrés dans **Supabase Vault** ; ils ne sont jamais inclus dans les réglages, les réponses de l’API, les journaux ou Git. Un cache privé en mode `0600` dans `/tmp/ghost-clicker-proxy-gateways.json` sert uniquement à l’exécution locale. Après leur première saisie avec cette version, les passerelles restent disponibles après les redémarrages et redéploiements. Chaque passerelle de la rotation doit attribuer une nouvelle IP à chaque connexion. N’utilisez jamais un identifiant déjà publié dans une conversation : réinitialisez-le d’abord chez le fournisseur.

Les cibles ne nécessitent plus de fichier texte. La **Bibliothèque d’URL** permet d’ajouter chaque cible avec :

- un pseudo lisible ;
- l’URL publique, avec ou sans préfixe `https://` ; une adresse comme `getallink.com/butter` est automatiquement enregistrée en HTTPS ;
- un premier libellé exact facultatif ;
- un éventuel second libellé ;
- une case indiquant si la cible doit participer aux prochains lancements.

Sans aucun libellé, le bot charge l’URL sans rechercher ni cliquer de bouton, puis attend une redirection automatique ou l’ouverture d’un nouvel onglet. Si aucune destination différente n’apparaît avant le délai configuré, la visite échoue. Avec uniquement le premier libellé, il clique une fois puis attend immédiatement la navigation. Avec deux libellés, il clique deux fois au même emplacement, avec une pause de 500 ms. Dans tous les modes, une modification de l’URL actuelle ou l’ouverture d’un nouvel onglet est acceptée. Le second libellé sélectionne le mode à deux clics et reste une référence : il n’est pas recherché dans la page. Un second libellé sans premier est refusé.

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

Après connexion, l’interface React se présente comme un tableau de bord sombre et responsive, avec des surfaces anthracite, des accents bleu ciel et une navigation inspirée des applications mobiles premium. Les états vides (URLs, passerelles, résultats) sont de vraies cartes d’action, sans débordement horizontal de 320 px à 1440 px. Elle conserve cinq sections : **Accueil**, **URLs**, **Réseau**, **Résultats** et **Réglages**. La navigation est latérale sur grand écran et flottante en bas sur mobile. Le changement de section ne recharge pas la page. Les formulaires de cible et de passerelle s’ouvrent dans une fenêtre dédiée, et les mutations restent protégées par le jeton CSRF Flask.

1. ouvrez **Réseau** et ajoutez une ou plusieurs passerelles avec leur pseudo : elles entrent toutes dans la rotation ;
2. ouvrez **URL** et ajoutez les cibles une par une dans la bibliothèque ;
3. utilisez les pseudos pour retrouver, consulter, modifier ou supprimer chaque entrée ;
4. cochez les URL à utiliser, ou employez **Tout sélectionner** et **Tout désélectionner** ;
5. revenez à **Accueil** et lancez le scénario : les exécutions manuelles et planifiées utilisent uniquement les URL cochées et une passerelle tirée au hasard dans la rotation.

Laissez les deux libellés vides pour une visite sans clic qui attend une redirection automatique. Si un premier libellé est saisi, il doit correspondre exactement au texte visible initial du bouton. Omettez le second libellé lorsque le premier clic redirige directement. Ajoutez-le pour demander un deuxième clic au même emplacement. Aucun texte traduit ni changement du DOM n’est utilisé pour déclencher ce deuxième clic.

L’ancien import texte, toujours accepté par les routes de compatibilité, reconnaît les trois formats `URL`, `URL | premier libellé` et `URL | premier libellé | second libellé`. Il refuse un second libellé lorsque le premier est vide.

Les formulaires, la sélection, le lancement, l’arrêt, le statut et les résultats utilisent l’API JSON intégrée : la page d’administration n’est pas entièrement rechargée après chaque action.

## Lancer le bot

Deux déclenchements sont disponibles :

- **Lancer maintenant** : démarre le mode choisi ;
- **Planification récurrente** : pour les modes fixe et continu, relance selon un intervalle configurable entre 60 et 10080 minutes.

Le mode d’exécution est choisi dans l’interface :

- **Nombre fixe** : exécute exactement le nombre de répétitions indiqué ;
- **Intervalle** : l’administrateur choisit un nombre d’exécutions souhaité `N` et une durée en jours. Au démarrage, le bot tire une seule fois un total compris entre `N − 10` et `N + 10`, dans les limites autorisées, puis crée des horaires aléatoires répartis sur toute la durée. Chaque horaire lance une exécution indépendante, avec son propre rapport et le nombre de répétitions configuré pour chaque cible sélectionnée ;
- **Continu** : fonctionne par cycles successifs de 5 minutes. Au début de chaque cycle, un nouveau nombre de répétitions est tiré entre le minimum et le maximum configurés. Le bot continue jusqu’au clic sur **Arrêter** et termine proprement la visite en cours.

Le plan de la campagne Intervalle est enregistré dans les réglages persistants : il reprend donc après un redémarrage ou un redéploiement. Si plusieurs horaires sont dépassés pendant une indisponibilité, ils sont repris successivement, jamais en parallèle. **Arrêter** annule les horaires restants et demande l’arrêt propre de l’éventuelle visite en cours.

Chaque cible cochée est visitée une fois par répétition, y compris lors de chaque exécution Intervalle ; les cibles non sélectionnées restent enregistrées mais ne sont pas ouvertes. Par exemple, 200 répétitions fixes et 3 cibles sélectionnées produisent 600 visites. Une seule exécution Google Chrome peut fonctionner à la fois.

Les cibles, paramètres, passerelles complètes, rotation, campagnes, exécutions et informations de planification sont persistants et survivent aux redémarrages et redéploiements Render. Les identifiants Rayobyte sont chiffrés séparément dans Supabase Vault et ne sont jamais exposés dans les métadonnées de configuration.

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

Le rapport persistant est enregistré après chaque visite : le tableau, le CSV téléchargeable et les indicateurs d’accueil se mettent donc à jour pendant que l’exécution est encore en cours. Une même exécution conserve un seul résumé progressif au lieu de créer des doublons, tandis que chaque visite dispose de sa propre ligne dans Supabase. La rétention est limitée automatiquement aux **500 visites les plus récentes** : à partir de la 501e, les lignes les plus anciennes sont supprimées de Supabase, du tableau et du CSV. Le cache local applique la même limite.

Le tableau de bord conserve un compteur quotidien persistant pour chaque URL. Une visite réussie vaut exactement un clic, qu’aucun, un ou deux boutons aient été configurés ; une visite échouée ne compte jamais. La semaine va du lundi 00:00 au dimanche 23:59 selon l’heure de Cotonou (`UTC+1`) et se remet automatiquement à zéro au changement de semaine. Pendant la semaine, le solde affiché est une projection fondée sur la moyenne des jours déjà écoulés depuis lundi. Le paiement est prévu chaque lundi, en crypto, pour les pays éligibles US, UK et CA.

Les plages ambiguës de la grille d’origine sont rendues continues : 100–149 clics/jour rapportent 15 $, 150–201 rapportent 20 $, 202–251 rapportent 26 $, 252–301 rapportent 36 $, 302–451 rapportent 46 $, 452–601 rapportent 56 $, 602–701 rapportent 71 $, 702–901 rapportent 86 $, 902–1151 rapportent 106 $, 1152–1501 rapportent 126 $, 1502–1751 rapportent 146 $, 1752–2001 rapportent 176 $, 2002–2251 rapportent 206 $, et le palier maximal à partir de 2252 rapporte 246 $. Chaque carte URL affiche les clics de la semaine, la moyenne quotidienne, le solde projeté et la progression vers le prochain palier.

La suppression de lignes de l’historique ne supprime pas les métriques de rémunération déjà enregistrées. Lors de la mise à niveau, les résultats encore disponibles initialisent les compteurs par URL ; les anciennes visites sans libellé marquées réussies malgré une URL finale identique restent exclues.

Depuis la console, l’administrateur peut supprimer une visite, plusieurs éléments cochés, une exécution complète — y compris une exécution sans visite — ou tout l’historique. Ces suppressions sont effectuées directement dans Supabase et restent disponibles pendant que le bot fonctionne ; la suppression d’une exécution efface aussi ses visites associées. Une exécution toujours active peut naturellement enregistrer de nouvelles visites après la suppression.

## Navigateur sur Render

Render n’a pas d’écran graphique. Google Chrome utilise donc obligatoirement le mode headless :

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
python -m playwright install chrome
Copy-Item .env.example .env
notepad .env
```

Pour utiliser `main.py` en direct, créez `proxy-gateway.txt` avec une ou plusieurs passerelles Rayobyte séparées par une ligne vide, puis remplissez aussi :

```dotenv
PROXY_GATEWAY_FILE="proxy-gateway.txt"
TARGET_URL="https://site-autorise.example/page"
# Laissez les deux valeurs vides pour une simple visite sans clic.
BUTTON_TEXT="Continuer"
# Laissez uniquement cette valeur vide pour un clic unique.
SECOND_BUTTON_TEXT="Confirmer"
EXECUTION_MODE="fixed" # fixed ou continuous en lancement direct
REPETITION_COUNT=10
REPETITION_MIN=5
REPETITION_MAX=20
HEADLESS=false
```

Chaque visite de ce lancement direct tire elle aussi une passerelle au hasard dans le fichier. La campagne **Intervalle** sur plusieurs jours dépend du planificateur persistant du service web et se configure donc dans l’interface, pas dans ce lancement direct de `main.py`. `proxy-gateway.txt` est ignoré par Git et doit rester local. Puis :

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
- zéro clic lorsque les libellés sont vides, un clic avec le seul premier libellé, ou deux clics au même emplacement lorsque les deux sont configurés ;
- fréquence planifiée minimale de 60 minutes ;
- nouvelle passerelle tirée au hasard et nouveau navigateur pour chaque visite ;
- contrôle d’entrée par exécution : IP de sortie, pays attendu (US/GB/CA) et réputation DNSBL, puis navigation immédiate vers la cible sans autre requête de contrôle par visite ;
- nombre fixe, campagne Intervalle à horaires aléatoires sur plusieurs jours, ou exécution continue avec arrêt manuel ;
- conservation de la chaîne de redirection de chaque visite (URL initiale, étapes, destination finale) dans Supabase, le CSV et l’interface ;
- suppression automatique des visites les plus anciennes au-delà de 500 lignes ;
- aucune technique de dissimulation ou simulation de comportement humain.

## Développement du tableau de bord

Les fichiers compilés de `static/` sont versionnés afin que le conteneur Flask puisse les servir sans installer Node.js en production. Après une modification de `frontend/src/` :

```powershell
npm ci
npm run check
npm run build
```

## Tests

```powershell
python -m unittest discover -s tests -v
npm run check
npm run build
```
