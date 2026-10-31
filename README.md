# Ghost-Clicker — Web Service Docker

Ghost-Clicker est un outil de **test fonctionnel autorisé**. Chaque lancement utilise l’un des trois modes configurables : un nombre fixe de répétitions, un total tiré dans un intervalle défini, ou une exécution continue jusqu’à l’arrêt manuel. Pour chaque visite, il :

1. crée une nouvelle connexion à la passerelle rotative, sans effectuer de contrôle IP préalable ;
2. ouvre immédiatement la page cible dans Chromium headless ;
3. repère le premier libellé visible exact et mémorise sa position dans le bouton ;
4. clique une première fois ;
5. si aucun second libellé n’est configuré, attend immédiatement la redirection ;
6. si un second libellé est configuré, attend 500 ms, clique une seconde fois exactement au même emplacement, puis attend la redirection ;
7. enregistre le numéro de répétition, le proxy utilisé, l’URL finale, le statut et l’erreur éventuelle ;
8. ferme complètement Chromium avant la visite suivante.

Il effectue exactement un ou deux clics selon la ligne cible, ne remplit aucun formulaire et n’imite pas un comportement humain. Utilisez-le uniquement sur des domaines que vous êtes autorisé à tester.

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

Le fichier des cibles accepte deux formats :

```text
# Un seul clic : le premier bouton redirige directement
https://site-autorise.example/page | Continuer

# Deux clics au même emplacement
https://site-autorise.example/page | Continuer | Confirmer
```

Avec deux champs, le bot clique une fois puis attend immédiatement la redirection. Avec trois champs, il clique deux fois au même emplacement, avec une pause de 500 ms entre les clics. Le second libellé sert à sélectionner le mode à deux clics et reste une valeur de référence : il n’est pas recherché dans la page.

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

Le premier libellé après `|` doit correspondre exactement au texte visible initial du bouton. Omettez le second libellé lorsque ce premier clic redirige directement. Ajoutez un second libellé pour demander un deuxième clic au même emplacement ; sa valeur reste uniquement une référence dans les résultats. Aucun texte traduit ni changement du DOM n’est utilisé pour déclencher le deuxième clic. Aucun redéploiement n’est nécessaire pour remplacer la passerelle ou les cibles.

## Lancer le bot

Deux modes sont disponibles :

- **Lancer maintenant** : exécution manuelle ;
- **Planification** : intervalle configurable entre 60 et 10080 minutes.

Le mode de répétition est choisi dans l’interface :

- **Nombre fixe** : exécute exactement le nombre indiqué ;
- **Intervalle** : tire un nombre entre le minimum et le maximum indiqués ;
- **Continu** : répète la liste jusqu’au clic sur **Arrêter**. La visite en cours se termine proprement avant l’arrêt.

Si plusieurs cibles sont enregistrées, chacune est visitée une fois par répétition. Par exemple, 200 répétitions et 3 cibles produisent 600 visites. En mode continu, les 1 000 résultats les plus récents sont conservés afin de borner l’utilisation de la mémoire.

Une seule exécution peut fonctionner à la fois. La passerelle, les cibles, les paramètres et les résultats sont temporaires : ils disparaissent lors d’un redémarrage ou d’un redéploiement Render, conformément au choix de stockage éphémère.

## Résultats

L’interface affiche :

- date et source de l’exécution ;
- mode d’exécution, numéro de répétition et total choisi lorsqu’il existe ;
- nom de la passerelle et ouverture de connexion ;
- URL initiale ;
- premier libellé et éventuel second libellé du bouton ;
- URL après redirection ;
- réussite ou échec ;
- message d’erreur.

Le rapport temporaire est enregistré après chaque visite : le tableau et le CSV téléchargeable se mettent donc à jour pendant que l’exécution est encore en cours. Une même exécution conserve un seul rapport progressif au lieu de créer des doublons.

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
- protection CSRF des formulaires ;
- jeton administrateur uniquement dans les variables Render ou `.env` ;
- passerelle proxy secrète conservée en mode `0600` dans `/tmp` sur Render ;
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
