# Ghost-Clicker

Ghost-Clicker est un outil Python pour **vérifier des proxys autorisés**, puis ouvrir une liste d’URL publiques dans un Chromium visible. Il ne clique sur aucun bouton, ne remplit aucun formulaire et ne tente pas de contourner les protections des sites.

Utilisez-le uniquement avec un abonnement proxy valide, sur des URL que vous êtes autorisé à consulter, à une cadence conforme aux conditions de Rayobyte et des sites visités.

## 1. Architecture

```text
Ghost-Clicker/
├── main.py              # validation, proxys, Playwright et génération du dashboard
├── links.txt            # URL à visiter, une par ligne
├── proxies.txt          # pays + noms des variables d’environnement (jamais les secrets)
├── .env.example         # modèle sans endpoint ni identifiant réel
├── .env                 # configuration locale à créer, ignorée par Git
├── .gitignore           # exclut secrets, venv, caches et résultats générés
├── requirements.txt     # dépendances Python
├── tests/
│   └── test_main.py     # tests unitaires sans connexion proxy
└── results/
    ├── .gitkeep
    ├── results.csv      # historique complet, généré à l’exécution
    └── dashboard.html   # dashboard local, généré à l’exécution
```

`results.csv`, `dashboard.html` et `.env` sont locaux et ne sont pas envoyés dans Git.

## 2. Fonctionnement de `main.py`

Le programme suit cette séquence :

1. charge `.env` avec `python-dotenv` ;
2. valide les URL de `links.txt` (HTTP/HTTPS seulement, pas d’identifiants intégrés, pas d’adresse locale ou privée) ;
3. lit `proxies.txt`, puis récupère les endpoints et identifiants dans les variables d’environnement ;
4. lance Chromium avec `headless=False` par défaut ;
5. crée un contexte de navigateur isolé pour chaque proxy ;
6. ouvre le service `IP_CHECK_URL` via ce contexte, récupère l’IP et compare le pays détecté à `US` ou `CA` lorsqu’il est fourni ;
7. ferme immédiatement le contexte si le proxy échoue ou si le pays ne correspond pas ;
8. répartit chaque URL valide **une fois**, en round-robin, entre les proxys ayant réussi le contrôle ;
9. suit les redirections normales, puis relève l’URL finale, le statut HTTP et la durée jusqu’à `domcontentloaded` ;
10. ferme les contextes et Chromium, y compris lors d’une interruption ;
11. ajoute les résultats au CSV et reconstruit le dashboard HTML.

Un statut final HTTP compris entre 200 et 399 est compté comme une navigation réussie. Les tests proxy et erreurs de validation sont affichés séparément dans le dashboard.

> Une offre résidentielle rotative peut changer d’IP entre le contrôle et la navigation. Si vous avez besoin d’une session persistante, utilisez uniquement la syntaxe de session officiellement fournie par Rayobyte dans les champs d’authentification ; le programme n’en invente aucune.

## 3. Installation sous Windows avec PowerShell

### Prérequis

- Windows 10 ou 11 ;
- Python 3.10 ou plus récent, accessible avec `py` ;
- un accès Rayobyte résidentiel actif et autorisé.

Depuis le dossier parent du projet :

```powershell
cd C:\chemin\vers\Ghost-Clicker

# Créer l’environnement virtuel
py -3 -m venv .venv

# Autoriser l’activation uniquement pour cette session PowerShell si nécessaire
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# Activer l’environnement
.\.venv\Scripts\Activate.ps1

# Installer les dépendances Python et le navigateur Chromium de Playwright
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium

# Créer la configuration locale protégée
Copy-Item .env.example .env
notepad .env
```

Pour quitter ultérieurement l’environnement virtuel :

```powershell
deactivate
```

## 4. Fichiers, un par un

### `links.txt`

Exemple fourni :

```text
# Une URL HTTP(S) publique par ligne. Les lignes vides et commentaires sont ignorés.
https://example.com/
https://example.org/
```

Remplacez ces domaines de démonstration par vos URL autorisées. Une URL invalide est enregistrée dans les résultats, mais n’est jamais ouverte. Les doublons exacts sont ignorés.

Pour limiter explicitement les navigations, renseignez aussi dans `.env` :

```dotenv
ALLOWED_DOMAINS=monsite.example,autre-site.example
```

Les sous-domaines de chaque domaine indiqué sont acceptés. N’ajoutez ni `https://`, ni chemin, ni port dans cette variable.

### `proxies.txt`

Le fichier fourni est un CSV :

```csv
name,country,server_env,username_env,password_env
rayobyte-us,US,RAYOBYTE_US_PROXY_SERVER,RAYOBYTE_US_USERNAME,RAYOBYTE_US_PASSWORD
rayobyte-ca,CA,RAYOBYTE_CA_PROXY_SERVER,RAYOBYTE_CA_USERNAME,RAYOBYTE_CA_PASSWORD
```

Les colonnes signifient :

- `name` : libellé local non secret et unique ;
- `country` : `US` ou `CA` ;
- `server_env` : nom de la variable `.env` contenant le serveur ;
- `username_env` et `password_env` : noms des variables d’authentification.

Il n’y a volontairement **aucun endpoint Rayobyte fictif** dans le dépôt. Le format accepte :

- des endpoints US et CA distincts ;
- un même endpoint avec des identifiants de ciblage pays distincts, si c’est le format exact fourni dans votre espace Rayobyte ;
- plusieurs endpoints : ajoutez une ligne et trois variables d’environnement par endpoint ;
- une authentification par liste blanche d’IP : laissez `username_env` et `password_env` vides dans la ligne, par exemple `rayobyte-us,US,RAYOBYTE_US_PROXY_SERVER,,`.

Le serveur doit être écrit dans le format attendu par Playwright :

```text
protocole://hote_fourni_par_rayobyte:port_fourni_par_rayobyte
```

Les protocoles acceptés sont `http`, `https` et `socks5`. Utilisez le protocole que Rayobyte indique réellement pour votre produit. Ne mettez jamais `utilisateur:mot_de_passe@` dans cette valeur.

### `.env.example` et `.env`

Copiez `.env.example` vers `.env`. Vous devez récupérer dans votre tableau de bord Rayobyte :

1. le ou les hôtes/IP de passerelle réellement attribués ;
2. le port de chaque endpoint ;
3. le protocole pris en charge ;
4. la méthode d’authentification : utilisateur/mot de passe ou liste blanche d’IP ;
5. le mécanisme officiel de sélection de pays : endpoints distincts ou paramètre dans l’identifiant ;
6. les valeurs propres aux États-Unis et au Canada.

Renseignez ces informations **directement dans votre fichier local `.env`**. Ne communiquez pas votre mot de passe dans une conversation, un ticket ou un commit.

Extrait du modèle, volontairement vide :

```dotenv
RAYOBYTE_US_PROXY_SERVER=
RAYOBYTE_US_USERNAME=
RAYOBYTE_US_PASSWORD=

RAYOBYTE_CA_PROXY_SERVER=
RAYOBYTE_CA_USERNAME=
RAYOBYTE_CA_PASSWORD=
```

Si le même compte est autorisé pour les deux endpoints, les variables peuvent contenir les mêmes identifiants. Si Rayobyte fournit des identifiants différents ou encode le pays dans l’utilisateur, copiez exactement la valeur US dans les variables US et la valeur CA dans les variables CA.

Réglages disponibles :

| Variable | Défaut | Rôle |
|---|---:|---|
| `IP_CHECK_URL` | `https://ipinfo.io/json` | Service JSON retournant `ip` et, si possible, `country` |
| `HEADLESS` | `false` | Maintient Chromium visible |
| `PROXY_CHECK_TIMEOUT_MS` | `20000` | Délai du test proxy |
| `NAVIGATION_TIMEOUT_MS` | `30000` | Délai par URL |
| `DELAY_BETWEEN_URLS_SECONDS` | `3` | Pause entre deux URL |
| `ALLOWED_DOMAINS` | vide | Liste blanche facultative de domaines |
| `IGNORE_HTTPS_ERRORS` | `false` | Ignore les erreurs TLS uniquement si explicitement nécessaire |
| `MAX_DASHBOARD_ROWS` | `2000` | Limite d’affichage HTML ; le CSV reste complet |

`ipinfo.io` est un service tiers avec ses propres limites et conditions. Vous pouvez remplacer `IP_CHECK_URL` par un service autorisé qui renvoie un objet JSON contenant une IP dans `ip`, `query` ou `origin`, et idéalement le pays dans `country`, `country_code` ou `countryCode`.

### `.gitignore`

Il exclut notamment `.env`, les environnements virtuels, les caches Python et tous les fichiers générés dans `results/`. Seuls `.env.example` et `results/.gitkeep` restent versionnés.

### `results/`

Après une exécution :

- `results.csv` contient l’historique de toutes les exécutions ;
- `dashboard.html` présente les indicateurs, taux de réussite, performances par pays, filtres et journal détaillé.

Les colonnes demandées sont présentes : date/heure, URL initiale, URL finale, pays configuré, IP de sortie, statut HTTP, résultat et message d’erreur. Le pays détecté, le nom du proxy, la durée, le type de ligne et l’identifiant d’exécution sont ajoutés pour faciliter le diagnostic.

Les identifiants et mots de passe ne sont jamais écrits dans le CSV ou le dashboard. Les erreurs Playwright passent aussi par un filtre de masquage.

### `requirements.txt`

- `playwright` pilote Chromium ;
- `python-dotenv` charge `.env`.

## 5. Lancer le programme

À chaque nouvelle session PowerShell :

```powershell
cd C:\chemin\vers\Ghost-Clicker
.\.venv\Scripts\Activate.ps1
python .\main.py
```

Puis ouvrir le dashboard :

```powershell
Start-Process .\results\dashboard.html
```

Codes de sortie :

- `0` : tous les contrôles et navigations exécutés ont réussi ;
- `1` : exécution terminée avec au moins un échec ;
- `2` : configuration inutilisable ;
- `130` : interruption avec `Ctrl+C`.

## 6. Procédure de test étape par étape

1. **Vérifier le compte fournisseur**
   Confirmez dans Rayobyte que l’offre résidentielle est active, que l’usage prévu est autorisé et que les sorties US/CA sont disponibles.

2. **Créer `.env`**
   Exécutez `Copy-Item .env.example .env`, puis complétez les endpoints réels, ports, protocoles et identifiants. N’inventez aucune valeur.

3. **Adapter `proxies.txt`**
   Conservez uniquement les pays/endpoints dont vous disposez. Si vous n’avez qu’un endpoint US, supprimez la ligne CA. Pour l’authentification par IP, laissez les deux colonnes d’authentification vides et autorisez au préalable votre IP dans Rayobyte.

4. **Faire un test minimal**
   Conservez seulement `https://example.com/` dans `links.txt`. Ce domaine est destiné aux exemples techniques.

5. **Limiter les domaines**
   Pour ce test, mettez `ALLOWED_DOMAINS=example.com` dans `.env`.

6. **Lancer**
   Exécutez `python .\main.py`. Chromium doit être visible. Chaque proxy est d’abord testé sur `IP_CHECK_URL`.

7. **Observer un proxy refusé**
   Si le service renvoie un pays différent, une erreur d’authentification, un délai dépassé ou aucune IP, ce contexte est fermé et ne visite aucun lien.

8. **Contrôler les sorties**
   Ouvrez `results\dashboard.html`. Vérifiez l’IP de sortie, les pays configuré/détecté, l’URL finale, le statut HTTP et la durée. Le fichier `results\results.csv` contient les mêmes données brutes.

9. **Tester la validation sans ouvrir l’URL**
   Ajoutez temporairement `file:///C:/Windows/System32/` ou `http://127.0.0.1/` dans `links.txt`. La ligne doit apparaître comme `URL_INVALIDE` sans être visitée.

10. **Passer à vos URL**
    Remplacez les exemples par les URL autorisées, mettez à jour `ALLOWED_DOMAINS`, conservez une pause raisonnable et surveillez les conditions d’utilisation et les limites de trafic.

11. **Tester le code sans proxy**
    Les tests unitaires n’ouvrent aucun navigateur et ne nécessitent aucun identifiant :

    ```powershell
    python -m unittest discover -s tests -v
    ```

## 7. Dépannage

### Chromium n’est pas installé

```powershell
python -m playwright install chromium
```

### PowerShell refuse l’activation du venv

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### `CONFIGURATION_INVALIDE`

Vérifiez que :

- `.env` existe bien, et non `.env.txt` ;
- chaque variable citée par `proxies.txt` est remplie ;
- le serveur contient le protocole et le port ;
- les valeurs ne contiennent pas de guillemets superflus ;
- la ligne CA a été retirée si aucun endpoint CA n’est disponible.

### `ECHEC_PAYS`

Le proxy répond, mais le pays retourné par le service de vérification ne correspond pas au pays déclaré dans `proxies.txt`. Vérifiez la syntaxe officielle de ciblage Rayobyte. Le programme ne poursuit pas avec cette connexion.

### `SUCCES_SANS_GEOLOCALISATION`

La connexion et l’IP ont été vérifiées, mais le service choisi n’a fourni aucun champ pays reconnu. Pour rendre la comparaison obligatoire, configurez un service JSON autorisé qui renvoie `country` ou `country_code`.

### Certificat HTTPS

Gardez `IGNORE_HTTPS_ERRORS=false`. Ne passez à `true` que si la documentation de votre offre l’exige et après avoir évalué le risque TLS.
