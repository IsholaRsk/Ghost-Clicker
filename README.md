# Ghost-Clicker — Web Service Docker

Ghost-Clicker est un outil de **test fonctionnel autorisé**. Il utilise une liste secrète de proxys et, pour chaque URL enregistrée :

1. choisit un proxy aléatoirement ;
2. contrôle son IP de sortie ;
3. essaie une seule fois un second proxy distinct si ce contrôle échoue ;
4. ouvre la page dans Chromium headless ;
5. recherche un bouton par son texte visible exact ;
6. effectue un seul clic ;
7. attend que l’URL change ;
8. enregistre le proxy utilisé, l’URL finale, le statut et l’erreur éventuelle ;
9. ferme le contexte de navigation avant de passer à l’URL suivante.

Il n’effectue aucun clic supplémentaire, ne remplit aucun formulaire et n’imite pas un comportement humain. Utilisez-le uniquement sur des domaines que vous êtes autorisé à tester.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/IsholaRsk/Ghost-Clicker)

## Architecture

```text
Ghost-Clicker/
├── main.py           # moteur Playwright
├── web.py            # interface web et planification
├── Dockerfile        # Chromium/Playwright dans Docker
├── render.yaml       # Blueprint Render
├── .env.example      # modèle sans secret
├── .dockerignore
├── .gitignore
├── requirements.txt
└── tests/
    └── test_main.py
```

## Déploiement Render

### 1. Créer le service

Cliquez sur **Deploy to Render** ci-dessus, ou choisissez **New → Blueprint** dans Render et sélectionnez ce dépôt.

Le runtime est :

```text
Docker
```

Le Blueprint utilise le plan Render **Starter** afin que la planification interne reste active. Un Web Service gratuit peut se mettre en veille et ne convient donc qu’aux lancements manuels.

### 2. Renseigner seulement les variables demandées

Render demande :

| Variable | Valeur |
|---|---|
| `PROXY_LIST_JSON` | liste JSON secrète des proxys autorisés |
| `ALLOWED_DOMAINS` | domaines que vous êtes autorisé à tester, séparés par des virgules |

Exemple de structure pour `PROXY_LIST_JSON` (remplacez les champs vides directement dans Render, jamais dans Git) :

```json
[
  {"name":"us-1","server":"http://us-east.gw.rayobyte.com:8000","username":"","password":""},
  {"name":"us-2","server":"http://us-east.gw.rayobyte.com:8000","username":"","password":""}
]
```

Les noms doivent être uniques. Conservez dans chaque mot de passe les paramètres officiels Rayobyte nécessaires à la connexion.

Exemple de domaines :

```text
site1.example,site2.example
```

N’ajoutez ni `https://`, ni chemin dans `ALLOWED_DOMAINS`.

`ADMIN_TOKEN` est généré automatiquement. La liste de proxys reste uniquement dans les variables secrètes Render.

Les anciens identifiants publiés dans une conversation doivent être réinitialisés avant utilisation.

### 3. Se connecter au bot

Après le build :

1. ouvrez **Render → Environment** ;
2. copiez la valeur de `ADMIN_TOKEN` ;
3. ouvrez l’URL publique du service ;
4. utilisez :
   - utilisateur : `admin` ;
   - mot de passe : valeur de `ADMIN_TOKEN`.

## Modifier la liste des URL en ligne

Dans **Paramètres en ligne**, utilisez une ligne par visite :

```text
URL | texte exact du bouton
```

Exemple :

```text
https://site1.example/page | Continuer
https://site2.example/offre | Voir le résultat
```

Le texte doit correspondre exactement au nom accessible d’un élément ayant le rôle `button`. Une URL hors de `ALLOWED_DOMAINS` est refusée.

Cliquez sur **Enregistrer**. Aucun redéploiement n’est nécessaire pour modifier les chemins ou les textes des boutons.

## Lancer le bot

Deux modes sont disponibles :

- **Lancer maintenant** : exécution manuelle ;
- **Planification** : intervalle configurable entre 60 et 10080 minutes.

Une seule exécution peut fonctionner à la fois. La planification et les résultats sont temporaires : ils disparaissent lors d’un redémarrage ou d’un redéploiement Render, conformément au choix de stockage éphémère.

## Résultats

L’interface affiche :

- date et source de l’exécution ;
- nom du proxy choisi et nombre de tentatives ;
- IP et pays de sortie ;
- URL initiale ;
- texte du bouton ;
- URL après redirection ;
- réussite ou échec ;
- message d’erreur.

Un bouton permet de télécharger le CSV.

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

Pour utiliser `main.py` avec une seule URL, remplissez aussi :

```dotenv
TARGET_URL="https://site-autorise.example/page"
BUTTON_TEXT="Continuer"
HEADLESS=false
```

Puis :

```powershell
python .\main.py
```

## Sécurité

- authentification HTTP Basic avec `ADMIN_TOKEN` ;
- protection CSRF des formulaires ;
- secrets uniquement dans les variables Render ou `.env` ;
- liste blanche obligatoire de domaines ;
- rejet des URL locales, privées et réservées ;
- redirections hors liste blanche bloquées ;
- maximum de 50 URL ;
- un clic explicite maximum par URL ;
- fréquence planifiée minimale de 60 minutes ;
- choix aléatoire limité au pool explicitement autorisé ;
- une seule reprise avec un proxy distinct lorsque le contrôle IP échoue ;
- aucune technique de dissimulation ou simulation de comportement humain.

## Tests

```powershell
python -m unittest discover -s tests -v
```
