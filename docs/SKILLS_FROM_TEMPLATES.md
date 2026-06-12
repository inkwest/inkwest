# Skills

> Implémenté — branche `feat/skills` (MR !65).
> Cette doc décrit l'**état livré**. Les designs intermédiaires (encodage `(...)` du nom,
> `render_skill_md`, slug calculé du nom) ont été remplacés par le modèle « SKILL.md unique » ci-dessous.

## 1. Objectif

Chaque **skill** (table `inkwest.skills`, anciennement `prompt_templates`) est matérialisée comme une skill
Claude dans la sandbox de chaque conversation, déclenchable :

- **explicitement** : `/<slug>` (avec arguments) tapé dans le chat ;
- **automatiquement** : le modèle la mobilise quand sa `description` correspond à la tâche.

## 2. Modèle — une skill = un SKILL.md

Une skill est éditée comme **un seul champ `content`** = un **SKILL.md complet** (frontmatter + corps), au
format natif Claude Code :

```markdown
---
name: summarize-gitlab-mr
description: Summarize a GitLab merge request for a reviewer…
arguments: [mr_url, --publish]
---
<instructions… $ARGUMENTS / $1 / $2>
```

- Le backend parse le frontmatter (`app/core/skill_md.py`, `yaml.safe_load`) et en dérive :
  - `name` → **slug** normalisé (`slugify`, `[a-z0-9-]`) = dossier `skills/<slug>/` = trigger `/<slug>` =
    `name:` réécrit dans le fichier (dossier / trigger / fichier cohérents — « Option B »).
  - `description`, `arguments` → `argument_hint`.
- **Stockage** (table `skills`, **sans migration de schéma**) : `name` (slug, **unique**) + `description`
  **dénormalisés** (liste/recherche rapides) ; `content` = le SKILL.md brut (name normalisé). `slug` et
  `argument_hint` ne sont **pas** stockés → exposés comme **computed fields**.
- Entité renommée `prompt_template → skill` partout : table, API `/api/skills`, modèle `Skill`, types, store,
  composants.

## 3. Backend

### 3.1 Parsing — `app/core/skill_md.py`
- `parse_skill_md(content) -> (slug, description, argument_hint, content_normalisé)` : normalise CRLF→LF,
  extrait le frontmatter, `yaml.safe_load`, **valide** (frontmatter + `name` requis → sinon `ValueError`),
  réécrit le `name:` en slug.
- `frontmatter_argument_hint(content)` : hint depuis `arguments` / `argument-hint` (None si absent/invalide).
- `legacy_to_skill_md(name, body, description, slug=None)` : reconstruit un SKILL.md depuis l'ancien format
  (`name` avec `(...)` + corps), pour la migration ; `slug` explicite pour résoudre une collision.

### 3.2 API — `app/api/routes/skills.py`
- `POST` / `PATCH` reçoivent **`content` seul** → parse → **422** si frontmatter invalide, **409** si le slug
  entre en collision. `name`(slug) / `description` / `content` sont stockés.
- `SkillResponse` (`app/models/schemas.py`) : `name`, `content`, `description`, `is_active`, timestamps +
  **computed** `slug` (= `slugify(name)`) et `argument_hint` (parsé du frontmatter).

### 3.3 Génération — `app/services/session_skills.py`
- `apply_session_skills(session_id, skills)` : écrit chaque `content` **verbatim** dans
  `<session_dir>/claude/skills/<slug>/SKILL.md` (monté sur `/home/sandbox/.claude/skills/…` via
  `CLAUDE_CONFIG_DIR`). Réconciliation desired-state (prune des dossiers orphelins), écriture atomique,
  idempotente, no-op hors sandbox — calqué sur `mcp_config.py`.
- Appelé **à chaque tour** par l'orchestrateur (`get_active_skills` → `apply_session_skills`) : le CLI lit sa
  config au lancement, donc une édition se propage au tour suivant de chaque session ; survit à un rebuild de
  sandbox.

### 3.4 Déclenchement — `app/services/orchestrator_cli.py`
- Le message utilisateur part **verbatim** à `claude -p` → une `/skill` s'expanse **n'importe où** dans le
  message. Le contexte runtime (accès cluster + connecteurs + consigne d'investigation) est dans
  `--append-system-prompt`, **pas** dans le tour user (sinon il polluerait `$ARGUMENTS`).
- ⚠️ `$ARGUMENTS` = **le reste de la ligne** (comportement Claude Code). Un `$` littéral devant un chiffre ou
  `ARGUMENTS` dans le corps doit être échappé `\$` (ex. un prix `\$5`).

### 3.5 Migration legacy — `ensure_skills_frontmatter(db)` (startup)
- Idempotente : convertit les lignes dont le `content` ne commence pas par `---` (ancien format body-only)
  en SKILL.md (frontmatter reconstruit depuis `name (...)` / `description`). **Dédup des slugs** (suffixe
  `-2`, `-3`…) pour ne jamais violer l'unicité de `name`. Appelée dans le lifespan (`app/main.py`) sous
  `try/except` + `rollback` → **ne bloque jamais le démarrage**.

## 4. Frontend

### 4.1 Admin — `pages/admin/SkillsPage.tsx`
- Formulaire = **un seul textarea « SKILL.md »** (modal `size="xl"`, `rows=22`). `name`/`description`/
  `arguments` viennent du frontmatter (parsé serveur) ; `adminApi` Create/Update = `{ content }`. Le `detail`
  d'un 422 (frontmatter invalide) remonte au formulaire (`services/api.ts`).

### 4.2 Chat — `ChatInput.tsx` + `SkillAutocomplete.tsx`
- Autocomplétion `/` **mid-token** (token sous le curseur, n'importe où) ; source = le store skills
  (`useSkillsStore`), filtré sur slug/name/description, chargé au montage. Clavier ↑/↓, Enter (sélectionne,
  **n'envoie pas**), Esc, Tab ; sélection insère `/<slug> `.
- **Ligne de hint** sous le composer quand un `/<slug>` connu est saisi :
  `/<slug> [arg1] [arg2] · <description>` (`formatArgs` met les args entre crochets ; fallback
  « arguments libres » sans hint).
- `SkillPalette.tsx` (ex-`PromptTemplatePalette`) : sélectionner insère `/<slug> ` au curseur.

## 5. Sécurité / robustesse
- `yaml.safe_load` (jamais `load`) sur du contenu admin.
- `slug` ∈ `[a-z0-9-]` (`slugify`) → **pas de path traversal** sur le dossier ; `name:` normalisé.
- Migration : dédup des slugs **+** `try/except`/`rollback` → jamais de crash au boot.
- CRLF normalisé en entrée du parser.
- Contenu **admin-curated** (écriture = admin) ; exécuté dans la sandbox isolée (connecteurs read-only).

## 6. Tests
- `backend/tests/test_session_skills.py` : parse (valide / CRLF / invalide / normalisation / formes de hint),
  `legacy_to_skill_md`, génération verbatim + prune, migration (conversion + **dédup de collision**),
  computed fields.
- `backend/tests/test_api_skills.py` : create/update (content), 422 frontmatter invalide, 409 collision, RBAC.
- `frontend/src/components/chat/ChatInput.test.tsx` : autocomplétion (`/`, filtre, clavier, mid-message),
  ligne de hint (générique + spécifique), insertion.

## 7. Références code

| Sujet | Emplacement |
|---|---|
| Parsing SKILL.md / slug | `backend/app/core/skill_md.py`, `backend/app/core/slug.py` |
| Modèle / schémas | `backend/app/models/skill.py`, `backend/app/models/schemas.py` |
| API | `backend/app/api/routes/skills.py` (`/api/skills`) |
| Génération + migration | `backend/app/services/session_skills.py` ; startup `backend/app/main.py` |
| Orchestrateur | `backend/app/services/orchestrator_cli.py` (verbatim + `--append-system-prompt`) |
| Sandbox | `backend/app/services/sandbox.py` (`CLAUDE_CONFIG_DIR`, `session_dir`) |
| Front | `frontend/src/components/chat/{ChatInput,SkillAutocomplete,SkillPalette}.tsx`, `pages/admin/SkillsPage.tsx`, `store/skills.ts`, `store/admin.ts`, `types/api.ts`, `services/{api,adminApi}.ts` |
