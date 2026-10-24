# Déployer Ghost-Clicker sur Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/IsholaRsk/Ghost-Clicker)

## Avant de commencer

Préparez uniquement :

1. un nouvel utilisateur Rayobyte ;
2. le mot de passe Rayobyte brut ;
3. les URL publiques et autorisées à contrôler.

Réinitialisez tout identifiant déjà publié dans une conversation ou un dépôt.

## Déploiement en un clic

1. Cliquez sur le bouton **Deploy to Render** ci-dessus.
2. Connectez GitHub à Render si nécessaire.
3. Validez le Blueprint proposé.
4. Saisissez les trois variables demandées :

| Variable | Valeur à saisir |
|---|---|
| `RAYOBYTE_US_USERNAME` | utilisateur Rayobyte |
| `RAYOBYTE_US_PASSWORD` | mot de passe brut ; le ciblage `-country-US` est ajouté automatiquement |
| `TARGET_URLS` | tableau JSON contenant les URL |

Exemple de `TARGET_URLS` :

```json
["https://site1.example/", "https://site2.example/page"]
```

Ne mettez pas cet exemple si ce ne sont pas vos URL. Les domaines autorisés sont déduits automatiquement.

5. Lancez la création du service et attendez la fin du build Docker.
6. Dans **Environment**, affichez/copiez la valeur générée de `ADMIN_TOKEN`.
7. Ouvrez l’URL publique du service.
8. Utilisez :
   - utilisateur : `admin` ;
   - mot de passe : valeur de `ADMIN_TOKEN`.
9. Cliquez sur **Lancer le contrôle**.

## Modifier les URL

1. Ouvrez **Render → ghost-clicker → Environment**.
2. Modifiez uniquement `TARGET_URLS`.
3. Enregistrez.
4. Lancez **Manual Deploy → Deploy latest commit**.

Vous pouvez ajouter `ALLOWED_DOMAINS` manuellement si vous souhaitez une liste blanche plus stricte :

```text
site1.example,site2.example
```

## Variables déjà configurées automatiquement

Le Blueprint configure notamment :

- le serveur proxy Rayobyte US ;
- `HEADLESS=true` ;
- Chromium Playwright ;
- 100 profils ZIP avec sessions uniques ;
- au maximum 10 profils actifs par exécution ;
- le service de vérification IP ;
- le serveur Gunicorn sur le port Render ;
- un token administrateur aléatoire.

## Stockage

La configuration actuelle utilise `/tmp` comme demandé. Le dashboard et le CSV peuvent disparaître après un redémarrage ou un redéploiement. Aucun secret n’est conservé dans l’image Docker ou dans Git.

## Diagnostic rapide

- **Écran `ADMIN_TOKEN n'est pas configuré`** : ajoutez ou régénérez `ADMIN_TOKEN` dans Render.
- **Pool non initialisé** : vérifiez l’utilisateur et le mot de passe Rayobyte.
- **Aucune URL** : vérifiez que `TARGET_URLS` est un tableau JSON valide.
- **Échec pays/ZIP** : le ZIP choisi peut manquer d’inventaire Rayobyte.
- **Build long** : l’image Playwright contient Chromium et peut prendre plusieurs minutes lors du premier déploiement.
