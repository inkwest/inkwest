# Proposition d'architecture — Sandbox `claude` par conversation (avec Squid dédié)

> **Statut : proposition (non implémentée).** Objectif : exécuter `claude` dans une
> image Docker **isolée filesystem + réseau**, dont **tout l'egress passe par un
> Squid propre à la conversation**, avec une **whitelist par conversation** (chaque
> conversation peut avoir des règles différentes, alignées sur les connecteurs
> accordés).

## 1. Motivation

Aujourd'hui `claude` tourne dans le conteneur backend partagé → il voit `/app`,
`~/.claude`, et a un egress libre. Problèmes : pas d'isolation filesystem/réseau,
pas de contrôle des destinations.

Cible : **un bac à sable jetable par conversation**, où :
- l'agent ne voit ni la source d'Inkwest ni l'état des autres conversations ;
- **tout accès réseau sortant est forcé via un Squid**, qui n'autorise que des
  domaines explicitement whitelistés ;
- la **whitelist est propre à chaque conversation** : elle reflète les connecteurs
  réellement accordés (ex. conv. A = modèle + K8s + AWS + GitLab ; conv. B = modèle
  + K8s + Coralogix uniquement).

## 2. Principe clé sur la communication backend ↔ runner

**On ne réécrit pas le pipeline.** Le backend continue de **lancer un process et de
lire son `stdout`** (stream-json) ; on change uniquement *quel* process :

| Aujourd'hui | Proposition |
|---|---|
| `create_subprocess_exec("claude", "-p", …)` | `create_subprocess_exec("docker", "exec", "-e", …, "runner-<sid>", "claude", "-p", …)` |

→ `parse_cli_response` est **inchangé** : la « communication avec l'autre conteneur »
= le `stdout` de `docker exec`. Pas de protocole réseau custom à écrire.

On retient la variante **runner long-vécu par conversation** (`docker exec` par
message) plutôt que `docker run` éphémère : l'état `~/.claude` (auth + `--resume`)
persiste naturellement dans le runner, et il n'y a pas de coût de démarrage par
message.

## 3. Architecture cible

```mermaid
flowchart TB
    BE["Backend (inkwest-backend)<br/>+ sandbox_manager · accès socket Docker"]

    subgraph CONV["Sandbox conversation &lt;sid&gt; — réseau internal: true (pas de route directe)"]
      RUNNER["runner-&lt;sid&gt; : claude + kubectl + aws + curl<br/>HTTP(S)_PROXY = squid-&lt;sid&gt;:3128"]
      SQUID["squid-&lt;sid&gt; : whitelist propre à la conversation"]
    end

    EXT["Egress autorisé (selon whitelist) :<br/>API modèle · K8s API · gitlab.com · *.amazonaws.com · coralogix"]

    BE -->|"docker exec runner claude -p … (lit stdout)"| RUNNER
    BE -.->|"socket Docker : create / exec / reload / teardown"| CONV
    RUNNER ==>|"tout l'egress via le proxy"| SQUID
    SQUID --> EXT
    RUNNER -. "tentative directe → bloquée (réseau internal)" .-> EXT
```

Seul **squid-<sid>** est bi-réseau (internal + externe). Le runner n'a **aucune
route directe** : son seul moyen de sortir est le proxy.

## 4. Cycle de vie

```mermaid
sequenceDiagram
    autonumber
    actor U as Utilisateur
    participant FE as Frontend
    participant BE as Backend (chat.py / orchestrator_cli)
    participant MGR as sandbox_manager
    participant SQ as squid-<sid>
    participant RN as runner-<sid>

    Note over U,RN: Ouverture de conversation
    U->>FE: ouvre la session
    FE->>BE: WS connect
    BE->>MGR: ensure_sandbox(sid, whitelist initiale)
    MGR->>SQ: crée réseau internal + squid (whitelist de base : modèle + K8s)
    MGR->>RN: crée runner (HTTPS_PROXY=squid, .claude vol, kubeconfig RO)

    Note over U,RN: Message
    U->>FE: message
    FE->>BE: WS message
    BE->>MGR: exec_claude(sid, prompt, creds_env)
    MGR->>RN: docker exec -e AWS_*/GITLAB_TOKEN runner claude -p … stream-json
    RN-->>MGR: stdout (stream-json)
    MGR-->>BE: lignes → ChatEvent
    BE-->>FE: ChatEvent (streaming)

    Note over U,RN: Connecteur accordé en cours de conversation
    U->>FE: « Donner accès à AWS »
    FE->>BE: OAuth → CredentialStore
    BE->>MGR: update_rules(sid, +aws)
    MGR->>SQ: régénère squid.conf + reload (ajoute *.amazonaws.com)

    Note over U,RN: Fermeture / inactivité
    BE->>MGR: teardown(sid)
    MGR->>RN: stop + rm
    MGR->>SQ: stop + rm + suppression du réseau
```

## 5. Contrôle d'egress + whitelist par conversation

```mermaid
flowchart LR
    R["runner : claude / aws / curl / kubectl"] -->|"HTTP(S)_PROXY"| S{"squid-&lt;sid&gt;<br/>domaine ∈ whitelist ?"}
    S -->|oui| OK["destination autorisée"]
    S -->|non| KO["deny 403 (loggé, sans secret)"]
    R -. "tentative directe" .-> X["bloquée : réseau internal, aucune route"]
```

**Whitelist dérivée des connecteurs accordés** (le levier « règles par conversation ») :

| Toujours | Si AWS accordé | Si GitLab accordé | Si Coralogix accordé |
|---|---|---|---|
| endpoint API du modèle, API server K8s | `*.amazonaws.com` (sts, sso, sso-oidc, eks, ec2, logs…) | `gitlab.com` | endpoint Coralogix |

À chaque `grant`/`revoke` de connecteur, `sandbox_manager.update_rules()` régénère le
`squid.conf` de la conversation et recharge Squid (`squid -k reconfigure`). La
whitelist réseau **suit** ainsi le moindre privilège déjà appliqué au niveau des
creds.

> HTTPS : Squid filtre sur le domaine via `CONNECT`/SNI (pas d'interception TLS, pas
> de déchiffrement) — un `acl dstdomain` suffit.

### 5.1 Exemple détaillé — « Donner accès à GitLab »

Accorder un connecteur agit sur **deux couches complémentaires** : le **token**
(couche creds) **et** la **whitelist réseau** (couche Squid). Les deux sont ajoutés au
grant et retirés au revoke → défense en profondeur.

```mermaid
sequenceDiagram
    autonumber
    actor U as Utilisateur
    participant FE as Frontend
    participant BE as Backend (routes/connectors + orchestrator_cli)
    participant GL as gitlab.com
    participant STORE as CredentialStore (RAM)
    participant MGR as sandbox_manager
    participant SQ as squid-<sid>
    participant RN as runner-<sid>

    Note over U,RN: 1) Consentement (OAuth Authorization Code + PKCE)
    U->>FE: clic « Donner accès à GitLab »
    FE->>BE: POST /connectors/<sid>/gitlab/initiate
    BE->>BE: génère state + PKCE (S256), persiste connector_pending_states
    BE-->>FE: redirect_url
    FE->>GL: /oauth/authorize (scopes read_api read_repository)
    U->>GL: consent
    GL-->>BE: GET /connectors/callback?code&state
    BE->>GL: exchange_code (code_verifier)
    GL-->>BE: access_token (+ refresh, ~2 h)
    BE->>STORE: put(uid, sid, gitlab)  — token en RAM
    BE->>BE: audit "grant" (sans secret)

    Note over U,RN: 2) La whitelist réseau suit le grant
    BE->>MGR: update_rules(sid, +gitlab)
    MGR->>SQ: ajoute « acl dstdomain gitlab.com » + squid -k reconfigure
    BE-->>FE: status = connected (+ expiration)

    Note over U,RN: 3) Message suivant : l'agent lit le code
    U->>FE: « montre-moi le README du projet X »
    FE->>BE: WS message
    BE->>MGR: docker exec -e GITLAB_TOKEN=… -e GITLAB_BASE_URL=… runner claude -p …
    MGR->>RN: lance claude (token dans l'env du process)
    RN->>SQ: curl -H "Authorization: Bearer $GITLAB_TOKEN" gitlab.com/api/v4/... (via HTTPS_PROXY)
    SQ->>SQ: gitlab.com ∈ whitelist ? → OUI
    SQ->>GL: CONNECT gitlab.com
    GL-->>RN: code / fichiers
    RN-->>BE: stdout stream-json → ChatEvent
    BE-->>FE: réponse (streaming)
```

**Étape par étape :**
1. **Consentement** : bouton → `initiate` (génère `state` + PKCE, stocke le
   pending-state) → redirection vers `gitlab.com/oauth/authorize` (scopes
   **`read_api read_repository`** uniquement) → l'utilisateur consent.
2. **Callback** : `exchange_code` échange le `code` contre un **access token** (~2 h)
   + refresh ; le token est rangé **en RAM** (`CredentialStore`, clé `(user, session,
   gitlab)`) ; ligne d'**audit** « grant » sans secret.
3. **Whitelist** : le backend appelle `sandbox_manager.update_rules(sid, +gitlab)` qui
   **ajoute `gitlab.com`** au `squid.conf` de **cette** conversation et recharge Squid.
   → l'egress vers GitLab devient possible **pour cette conversation seulement**.
4. **UI** : le badge GitLab passe à **connected** (avec expiration affichée).
5. **Usage** : au message suivant, `orchestrator_cli` injecte `GITLAB_TOKEN` +
   `GITLAB_BASE_URL` dans l'`env` du `docker exec`. L'agent fait un `curl` vers l'API
   GitLab **via le proxy Squid** ; Squid voit `gitlab.com` dans la whitelist →
   laisse passer → l'agent lit le code.

**Les deux verrous (défense en profondeur) :**

| Couche | Ajoutée au grant | Effet si manquante |
|---|---|---|
| **Token** (`GITLAB_TOKEN` injecté par session) | oui | sans token → `401` côté GitLab |
| **Whitelist Squid** (`gitlab.com`) | oui | sans whitelist → la requête n'atteint **jamais** GitLab (egress bloqué par le réseau internal) |

→ Même si un token fuyait, le runner ne pourrait pas joindre `gitlab.com` sans
l'entrée whitelist ; et inversement.

**Révocation / expiration :** au `revoke` (ou à l'expiration), le backend
(1) retire le token du store et (2) appelle `update_rules(sid, -gitlab)` qui **enlève
`gitlab.com`** de la whitelist + reload. Les deux verrous se referment ensemble.

> Note : ce schéma est l'extension « sandbox » du flux GitLab **déjà fonctionnel**
> aujourd'hui (étapes 1-2-5). Le **nouveau** est l'étape 3 (whitelist Squid dynamique)
> et le passage du token via `docker exec -e` au lieu de l'env du process backend.

### 5.2 Scénario complet de bout en bout

Ouverture de session → message → accès GitLab → accès AWS → message → fermeture.

```mermaid
sequenceDiagram
    autonumber
    actor U as Utilisateur
    participant FE as Frontend
    participant BE as Backend (chat / connectors / orchestrator_cli)
    participant ST as CredentialStore (RAM)
    participant MGR as sandbox_manager
    participant SQ as squid (conv)
    participant RN as runner (conv)
    participant EXT as Cibles externes (K8s / gitlab.com / AWS)

    Note over U,EXT: ① OUVERTURE DE SESSION
    U->>FE: ouvre une session
    FE->>BE: WS connect
    BE->>MGR: ensure_sandbox(sid, base = API modèle + K8s API)
    MGR->>SQ: crée réseau internal + squid (whitelist de base)
    MGR->>RN: crée runner (HTTPS_PROXY=squid, vol .claude, kubeconfig RO)
    BE-->>FE: prêt

    Note over U,EXT: ② MESSAGE #1 (aucun connecteur accordé)
    U->>FE: « décris l'état du cluster »
    FE->>BE: WS message
    BE->>ST: build_connector_env(sid) → {} (rien d'accordé)
    BE->>MGR: docker exec runner claude -p … stream-json
    MGR->>RN: lance claude
    RN->>SQ: kubectl … (via proxy)
    SQ->>EXT: K8s API (whitelisté) OK
    EXT-->>RN: réponse
    RN-->>BE: stdout → ChatEvent
    BE-->>FE: réponse (streaming)

    Note over U,EXT: ③ DONNER ACCÈS À GITLAB
    U->>FE: clic « Donner accès à GitLab »
    FE->>BE: initiate (PKCE) → redirect
    U->>EXT: consent gitlab.com (read_api, read_repository)
    EXT-->>BE: callback (code)
    BE->>EXT: exchange_code
    EXT-->>BE: access_token (~2 h)
    BE->>ST: put(sid, gitlab) — token en RAM
    BE->>MGR: update_rules(sid, +gitlab)
    MGR->>SQ: + acl dstdomain gitlab.com + reload
    BE-->>FE: GitLab = connected

    Note over U,EXT: ④ DONNER ACCÈS À AWS
    U->>FE: clic « Donner accès à AWS »
    FE->>BE: initiate (PKCE) → redirect Identity Center
    U->>EXT: consent SSO
    EXT-->>BE: callback (code)
    BE->>EXT: exchange_code → token SSO + ListAccounts/ListAccountRoles
    EXT-->>BE: comptes / rôles autorisés
    BE-->>FE: needs_selection (liste compte+rôle)
    U->>FE: choisit compte + rôle
    FE->>BE: aws/select
    BE->>EXT: GetRoleCredentials → STS (~1 h)
    EXT-->>BE: creds STS
    BE->>ST: put(sid, aws) — STS en RAM
    BE->>MGR: update_rules(sid, +aws)
    MGR->>SQ: + acl dstdomain *.amazonaws.com + reload
    BE-->>FE: AWS = connected (compte/rôle + expiration)

    Note over U,EXT: ⑤ MESSAGE #2 (GitLab + AWS accordés)
    U->>FE: « corrèle le code GitLab et l'infra AWS du cluster »
    FE->>BE: WS message
    BE->>ST: build_connector_env(sid) → {GITLAB_TOKEN, AWS_*}
    BE->>MGR: docker exec -e GITLAB_TOKEN -e AWS_* runner claude -p …
    MGR->>RN: lance claude (creds dans l'env)
    RN->>SQ: kubectl / aws / curl gitlab (via proxy)
    SQ->>EXT: K8s OK · *.amazonaws.com OK · gitlab.com OK
    EXT-->>RN: résultats
    RN-->>BE: stdout → ChatEvent
    BE-->>FE: réponse (streaming)

    Note over U,EXT: ⑥ FERMETURE / INACTIVITÉ
    BE->>MGR: teardown(sid)
    MGR->>RN: stop + rm
    MGR->>SQ: stop + rm + suppression du réseau
    BE->>ST: revoke_session(sid) — creds purgés
```

**Lecture rapide :**
- ① la sandbox démarre avec une whitelist **minimale** (modèle + K8s) ;
- ③/④ chaque grant **étend** la whitelist Squid de la conversation (gitlab.com, puis
  `*.amazonaws.com`) **en plus** de ranger le credential en RAM ;
- ⑤ au message suivant, les creds sont injectés par `docker exec -e` et l'agent
  joint K8s + AWS + GitLab — chaque sortie passant le filtre Squid ;
- ⑥ à la fermeture, runner + squid + réseau sont détruits et les creds purgés.

## 6. Composants à créer / modifier

| Élément | Rôle |
|---|---|
| `app/services/sandbox_manager.py` *(nouveau)* | `ensure_sandbox(sid, targets)`, `exec_claude(sid, cmd, env)`, `update_rules(sid, targets)`, `teardown(sid)` — pilote réseau + squid + runner via le socket Docker |
| `app/services/ai_cli_client.py` *(modif)* | derrière `CLAUDE_CLI_SANDBOX=true`, construit `["docker","exec",…,"runner-<sid>","claude",…]` au lieu de `["claude",…]` ; `stdout` inchangé |
| `app/services/orchestrator_cli.py` *(modif)* | appelle `ensure_sandbox` à la 1re interaction, `update_rules` sur grant/revoke |
| Image `inkwest-claude-runner` *(nouveau Dockerfile)* | minimale : `claude` + `kubectl` + `aws` + `curl`, **sans** la source backend |
| `squid.conf` template *(nouveau)* | base + `acl dstdomain` générés par connecteur |
| `docker-compose` / réseaux | réseau `internal: true` par conversation ; backend monte `/var/run/docker.sock` |

Esquisse du dispatch (inchangé pour le parsing) :

```python
# ai_cli_client.execute_claude_cli (mode sandbox)
if settings.claude_cli_sandbox:
    cmd = ["docker", "exec",
           *sum((["-e", f"{k}={v}"] for k, v in extra_env.items()), []),
           f"inkwest-runner-{session_id}",
           "claude", "--dangerously-skip-permissions", "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           *(["--resume", resume_session_id] if resume_session_id else [])]
else:
    cmd = ["claude", ...]   # comportement actuel
process = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
```

## 7. Sécurité

- **Isolation filesystem** : le runner ne monte **pas** `/app` → l'agent ne voit pas
  la source d'Inkwest.
- **Isolation réseau** : réseau `internal: true` + `HTTPS_PROXY=squid` → **aucune
  sortie hors whitelist**, par conversation.
- **Socket Docker** : seul le **backend** (de confiance) l'utilise ; le runner ne
  l'a pas → l'agent ne peut pas piloter Docker ni s'évader.
- **Secrets** : creds connecteurs passés par `-e` au `docker exec` (par message,
  depuis le store RAM) → cloisonnés par session ; jamais dans le prompt/logs.
- **Auth & historique** : `~/.claude` (auth + `--resume`) dans un volume **par
  conversation** → isolation des transcripts entre conversations.

## 8. Limites & points d'attention

1. **Coût ressources** : N conversations actives = N (squid + runner). Prévoir un
   **teardown sur inactivité** (TTL) + plafond de sandboxes concurrentes.
2. **Whitelist du CLI `claude`** : le binaire contacte plusieurs domaines (API
   Anthropic + télémétrie type statsig/sentry) ; à calibrer dans Squid sinon le CLI
   peut échouer. **Piège pratique n°1.**
3. **Latence d'ouverture** : création réseau + squid + runner au démarrage de
   conversation (quelques secondes, une seule fois).
4. **DooD** : acceptable en dev ; en **prod**, préférer l'équivalent Kubernetes
   (cf. §9).
5. **Nettoyage** : noms déterministes (`inkwest-runner-<sid>`, `inkwest-squid-<sid>`,
   réseau `inkwest-conv-<sid>`) + reaper en cas de crash backend.

## 9. Équivalent production (Kubernetes)

- 1 **Pod par conversation** (ou par message) avec le conteneur runner ;
- **NetworkPolicy** « default-deny egress » + egress autorisé uniquement vers un
  **proxy Squid** (sidecar ou service) dont la conf est générée par conversation
  (ConfigMap) ;
- pas de socket Docker — l'API K8s (ServiceAccount restreint) crée/détruit les Pods.

## 10. Plan de mise en œuvre (par phases)

1. **Image runner** (`inkwest-claude-runner`) + auth `claude` validée dedans.
2. **`sandbox_manager`** : create/exec/teardown (whitelist statique de base d'abord).
3. **Flag `CLAUDE_CLI_SANDBOX`** + bascule dans `ai_cli_client` (stdout inchangé).
4. **Squid dynamique** : génération `squid.conf` par connecteur + `reload` sur
   grant/revoke.
5. **Lifecycle** : teardown idle/fermeture + reaper + plafond.
6. **Durcissement** : logs d'egress Squid (audit réseau), tests d'isolation
   (vérifier qu'une destination hors whitelist est bien refusée).

---

### Récapitulatif de la décision de comms
La brique qui « parle à l'autre conteneur » reste un **spawn de process + lecture
stdout** (`docker exec … claude -p … stream-json`). Aucun protocole réseau custom :
le changement est concentré dans `ai_cli_client` (construction de la commande) et un
nouveau `sandbox_manager` (cycle de vie réseau/squid/runner).
```
