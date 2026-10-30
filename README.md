# Ghost-Clicker — Web Service Docker

Ghost-Clicker est un outil de **test fonctionnel autorisé**. Il utilise une passerelle proxy rotative secrète et, pour chaque URL enregistrée :

1. crée une nouvelle connexion à la passerelle rotative ;
2. contrôle son IP de sortie ;
3. recrée une seule fois une connexion neuve à la même passerelle si ce contrôle échoue ;
4. ouvre la page dans Chromium headless ;
5. recherche le bouton par son premier libellé visible exact et clique une fois ;
6. attend que ce bouton affiche le second libellé exact ;
7. clique une seconde fois ;
8. attend que l’URL change ;
9. enregistre le proxy utilisé, l’URL finale, le statut et l’erreur éventuelle ;
10. ferme complètement Chromium avant de passer à l’URL suivante.

Il effectue exactement ces deux clics, ne remplit aucun formulaire et n’imite pas un comportement humain. Utilisez-le uniquement sur des domaines que vous êtes autorisé à tester.

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

### 2. Préparer la passerelle et le fichier des cibles

La passerelle Rayobyte utilise exactement cette valeur, sans lien `mailto:` ni mise en forme Markdown :

```text
username:password@host:port
```

La passerelle doit attribuer une nouvelle IP à chaque connexion. La valeur est saisie dans l’interface authentifiée, stockée uniquement dans `/tmp` et jamais ajoutée à Git. N’utilisez jamais un identifiant déjà publié dans une conversation : réinitialisez-le d’abord chez le fournisseur.

Le fichier des cibles utilise :

```text
https://site-autorise.example/page | Continuer | Confirmer
```

Une ligne par URL, suivie du premier puis du second libellé exact du bouton. Après le premier clic, le bot attend que le second libellé apparaisse avant de cliquer à nouveau.

`ADMIN_TOKEN` est généré automatiquement par Render. Les anciens identifiants publiés dans une conversation doivent être réinitialisés avant utilisation.

### 3. Se connecter au bot

Après le build :

1. ouvrez **Render → Environment** ;
2. copiez la valeur de `ADMIN_TOKEN` ;
3. ouvrez l’URL publique du service ;
4. utilisez :
   - utilisateur : `admin` ;
   - mot de passe : valeur de `ADMIN_TOKEN`.

## Importer la configuration en ligne

Après connexion :

1. saisissez la valeur dans le champ protégé puis cliquez sur **Enregistrer la passerelle** ;
2. importez le fichier des cibles avec **Importer les URL** ;
3. vérifiez que la passerelle est indiquée comme configurée ;
4. vous pouvez ensuite modifier les lignes cibles directement dans la zone **Paramètres en ligne**.

Les deux libellés après les séparateurs `|` doivent correspondre exactement aux textes visibles successifs de l’élément cliquable. Celui-ci peut être un bouton ou un lien. Aucun redéploiement n’est nécessaire pour remplacer la passerelle ou les cibles.

## Lancer le bot

Deux modes sont disponibles :

- **Lancer maintenant** : exécution manuelle ;
- **Planification** : intervalle configurable entre 60 et 10080 minutes.

Une seule exécution peut fonctionner à la fois. La passerelle, les cibles, les paramètres et les résultats sont temporaires : ils disparaissent lors d’un redémarrage ou d’un redéploiement Render, conformément au choix de stockage éphémère.

## Résultats

L’interface affiche :

- date et source de l’exécution ;
- nom de la passerelle et nombre de tentatives de connexion ;
- IP et pays de sortie ;
- URL initiale ;
- premier et second libellés du bouton ;
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

Pour utiliser `main.py` avec une seule URL, créez `proxy-gateway.txt` avec l’unique passerelle Rayobyte indiquée plus haut, puis remplissez aussi :

```dotenv
PROXY_GATEWAY_FILE="proxy-gateway.txt"
TARGET_URL="https://site-autorise.example/page"
BUTTON_TEXT="Continuer"
SECOND_BUTTON_TEXT="Confirmer"
HEADLESS=false
```

`proxy-gateway.txt` est ignoré par Git et doit rester local. Puis :

```powershell
python .\main.py
```

## Sécurité

- authentification HTTP Basic avec `ADMIN_TOKEN` ;
- protection CSRF des formulaires ;
- jeton administrateur uniquement dans les variables Render ou `.env` ;
- passerelle proxy secrète conservée en mode `0600` dans `/tmp` sur Render ;
- rejet des URL locales, privées et réservées ;
- maximum de 50 URL ;
- au maximum deux clics explicites par URL, un pour chaque libellé configuré ;
- fréquence planifiée minimale de 60 minutes ;
- nouvelle connexion à la passerelle rotative pour chaque URL ;
- une seule reprise avec une connexion neuve lorsque le contrôle IP échoue ;
- aucune technique de dissimulation ou simulation de comportement humain.

## Tests

```powershell
python -m unittest discover -s tests -v
```
