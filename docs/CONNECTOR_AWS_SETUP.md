# Mise en place du connecteur AWS (IAM Identity Center)

Runbook pour activer l'accès AWS d'Inkwest : **Device Authorization Grant** vers
**IAM Identity Center** → sélection **multi-comptes/rôles** → **STS temporaire (~1 h)**.

> Pourquoi le device grant et pas le flux redirect ? Les clients publics
> d'Identity Center n'acceptent que des redirect URIs **loopback** (et de manière
> très contrainte). Le device grant ne nécessite **aucun redirect URI** — c'est le
> flux qu'utilise `aws sso login`, robuste et supporté.
>
> Le **read-only** est garanti par le **rôle / permission set** choisi (pas par une
> allowlist en code). Utilise des permission sets en lecture seule.

---

## 0. Prérequis

- **IAM Identity Center activé** sur l'organisation AWS.
- Un (ou des) **permission set read-only** **assigné(s) à ton utilisateur** sur les
  comptes à investiguer (sinon la liste compte/rôle sera vide). Les
  **users/groupes/permissions existants du SSO sont réutilisés** — rien à créer.
- Un accès AWS pour enregistrer le client OIDC (compte de gestion / CloudShell).
- Le binaire `aws` CLI est présent dans l'image backend (fait).

---

## 1. Enregistrer le client OIDC (→ `client_id` + `client_secret`)

Un **client public simple** suffit (le device grant ne prend ni redirect URI ni
grant-types explicites). Depuis **AWS CloudShell** (compte de gestion) :

```bash
aws sso-oidc register-client \
  --client-name "inkwest" \
  --client-type public \
  --region eu-west-1            # région de ton Identity Center
```

La réponse contient **`clientId`** et **`clientSecret`** → ce sont
`AWS_SSO_OIDC_CLIENT_ID` et `AWS_SSO_OIDC_CLIENT_SECRET`.

> ⚠️ Le `clientSecret` expire (~90 jours) → il faudra ré-enregistrer un client et
> mettre à jour le `.env` à ce moment-là (pas d'API de suppression : les vieux
> clients expirent seuls).

Récupère aussi l'**URL du portail d'accès** : console → **IAM Identity Center →
Settings → AWS access portal URL** (ex. `https://d-xxxxxxxxxx.awsapps.com/start`).

---

## 2. Configurer Inkwest (`.env` à la racine du repo)

```bash
CONNECTORS_ENABLED=true

AWS_SSO_ENABLED=true
AWS_SSO_REGION=eu-west-1                              # région de l'Identity Center
AWS_SSO_START_URL=https://d-xxxxxxxxxx.awsapps.com/start
AWS_SSO_OIDC_CLIENT_ID=<clientId de l'étape 1>
AWS_SSO_OIDC_CLIENT_SECRET=<clientSecret de l'étape 1>   # nom exact : ..._OIDC_CLIENT_SECRET
```

Appliquer (recrée le backend pour relire l'`.env`) :

```bash
docker compose up -d backend
```

> Pas besoin de `CONNECTOR_REDIRECT_BASE` pour AWS (aucun redirect). Cette variable
> ne sert qu'aux connecteurs OAuth-redirect comme GitLab.

---

## 3. Utilisation (UI)

1. Session de chat → bandeau **External access** → ligne **AWS** → **Give access**.
2. Une **petite popup** s'ouvre sur la page de vérification AWS + un **code** est
   affiché → connecte-toi et **valide le code** (la popup se ferme automatiquement
   une fois approuvé).
3. La **modale de sélection** s'ouvre : **cases à cocher**, **groupées par compte**
   (triées), **recherche** en haut, **tout cocher/décocher**. Choisis **un ou
   plusieurs** comptes/rôles.
4. **Donner accès** → statut **connected** (~1 h). Un **profil AWS** est créé par
   couple sélectionné.
5. Dans le chat, l'agent peut :
   ```
   aws configure list-profiles
   aws --profile <compte>-<rôle> eks list-clusters
   aws s3 ls                       # profil par défaut = 1er sélectionné
   ```

---

## 4. Comment ça marche (technique)

```
Give access AWS
  → StartDeviceAuthorization (clientId/secret/startUrl) → userCode + verificationUri
  → l'utilisateur approuve dans le navigateur
  → polling CreateToken (grant device_code) → access token SSO
  → ListAccounts + ListAccountRoles → comptes/rôles autorisés (modale multi-select)
  → GetRoleCredentials × N (un par couple) → N jeux STS (~1 h) stockés EN RAM
  → à chaque tour : projection RAM → tmpfs ~/.aws/{credentials,config} (un profil
    par couple + [default]) ; AWS_SHARED_CREDENTIALS_FILE/AWS_CONFIG_FILE injectés
```

**Stockage** : les STS vivent dans le **store RAM** Python (source de vérité). À
l'exécution, ils sont projetés dans **`/dev/shm`** (tmpfs **RAM**, mode `0600`),
**jamais sur le disque persistant**, et **supprimés à la révocation**.

Fichiers : `backend/app/services/connectors/aws.py` (device grant + multi-select),
`backend/app/tools/connector_exec.py` (`write_aws_profiles` / tmpfs),
`backend/app/api/routes/connectors.py` (endpoints `aws/device/start`,
`aws/device/poll`, `aws/options`, `aws/select`).

---

## 5. Sécurité / moindre privilège

- **Read-only au niveau IAM** : choisis des permission sets en lecture seule
  (pas d'allowlist en code côté `aws_call`).
- **STS ~1 h** : temporaires, en RAM (tmpfs à l'exécution), jamais en
  log/prompt/navigateur ; argv ne contient jamais le secret (seulement le chemin
  du fichier tmpfs).
- **Plafonné par le SSO** de l'utilisateur : pas d'escalade.
- **Cloisonnement par session** : un fichier `~/.aws` tmpfs par session.
- **Révocation** : bouton Revoke → creds purgés du store + fichiers tmpfs supprimés
  (les STS expirent de toute façon à ~1 h).

---

## 6. Dépannage

| Symptôme | Piste |
|---|---|
| Connecteur AWS absent de l'UI | `AWS_SSO_ENABLED`/`AWS_SSO_REGION`/`AWS_SSO_START_URL`/`AWS_SSO_OIDC_CLIENT_ID`/`AWS_SSO_OIDC_CLIENT_SECRET` manquants → backend recréé après l'`.env` ? |
| `register-client` : « unknown options » | CLI trop vieux → utilise **CloudShell** (CLI récent) |
| `register-client` : « Could not connect to endpoint » | faute de frappe région (ex. `eu-west1` au lieu de `eu-west-1`) |
| Reste « en attente d'approbation » | tu n'as pas encore validé le code dans la popup AWS |
| Liste comptes/rôles vide | aucun permission set assigné à ton utilisateur sur les comptes |
| `aws` échoue « no credentials » dans le chat | accès AWS non accordé / expiré → re-cliquer « Give access » |
| `clientSecret` expiré (~90 j) | ré-enregistrer un client + mettre à jour le `.env` |
