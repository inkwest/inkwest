# Prompt templates → skills Claude

> **Spec figée le 2026-06-11.** Statut : conception validée, implémentation à venir (MR-A/B/C).
> Dépend du ticket sandbox (sandbox isolée par conversation + montage `~/.claude`).
> Références code et faits Claude Code vérifiés (revue adversariale, 2026-06-11).

## 1. Objectif

Transformer chaque **prompt template** (table `prompt_templates`) en une **skill Claude** matérialisée
dans la sandbox de chaque conversation, déclenchable :

- **explicitement** par l'utilisateur via slash command `/<slug>` (avec arguments) ;
- **automatiquement** par le modèle quand la `description` de la skill correspond à la tâche en cours.

Aujourd'hui, sélectionner un template **colle son contenu brut** dans l'input du chat
(`PromptTemplatePalette` → `onSelect(template.content)`, repris par `handleSelectTemplate` dans
`ChatInput.tsx`). Cible : déclencher une skill au lieu de coller du texte.

## 2. Décisions

- **Skills, pas commands.** Les custom commands (`.claude/commands/<name>.md`) ont été fusionnées dans
  les skills ; on génère donc des `SKILL.md`. `/<slug>` reste le déclencheur explicite.
- **Auto-invocation activée** : **pas** de `disable-model-invocation`. La skill est déclenchable par
  `/<slug>` **et** mobilisable par le modèle selon sa `description`.
- **Mapping** `PromptTemplate` → `SKILL.md` :

  | Champ DB | Destination `SKILL.md` |
  |---|---|
  | `name` | nom du **dossier** `skills/<slug>/` = frontmatter `name:` = le `/<slug>` |
  | `description` | frontmatter `description:` — **seul texte lisible** (affiché UI + signal d'auto-invocation) |
  | `content` | **corps** de la skill (instructions / prompt) |

- Le `slug` (dossier / `name:` / trigger) est **dérivé du `name` et autoritaire côté serveur**, exposé à
  l'API pour que front (autocomplétion) et back (nom de fichier) partagent la même valeur.

## 3. Conséquences de l'auto-invocation

1. **`description` à double usage** : ce n'est plus un simple label, c'est le **signal de déclenchement**
   pour le modèle. La rédiger orientée *« quand utiliser »* (ex. « À utiliser pour analyser les logs d'un
   pod en CrashLoopBackOff »). → prévoir un **hint d'écriture** dans le formulaire admin.
2. **Le corps doit tolérer `$ARGUMENTS` vide** : en auto-invocation, il n'y a pas de `/<slug> args`, donc
   pas d'arguments. Le corps doit dégrader proprement (ex. « Si des cibles sont précisées : `$ARGUMENTS` ;
   sinon, déduis-les du contexte »). ⚠️ Le comportement exact de `$ARGUMENTS` en **auto-invocation**
   (vide vs absent) n'est pas documenté côté Claude Code → cette tolérance est défensive.

## 4. Architecture cible

### 4.1 Emplacement

`CLAUDE_CONFIG_DIR` remplace `~/.claude` pour la découverte. La sandbox monte `<session_dir>/claude` sur
`<sandbox_home>/.claude` et exporte `CLAUDE_CONFIG_DIR=<sandbox_home>/.claude` (`sandbox.py`,
provision()). Avec `sandbox_home = /home/sandbox` :

- **host** : `<session_dir>/claude/skills/<slug>/SKILL.md`
- **conteneur** : `/home/sandbox/.claude/skills/<slug>/SKILL.md`

### 4.2 Format `SKILL.md` généré

```markdown
---
name: <slug>
description: <template.description>
argument-hint: <hint>          # uniquement si le nom porte une signature "(...)"
---
<template.content>

$ARGUMENTS
```

- `$ARGUMENTS` est ajouté en fin de corps **si absent** du contenu (sinon Claude Code colle les arguments
  sous forme `ARGUMENTS: …`, moins propre).
- `argument-hint` n'est émis **que** si le nom du template porte une signature `(...)` (voir §5.4).
- Formes d'arguments : `$ARGUMENTS` (chaîne complète), `$ARGUMENTS[N]` ou `$N` (positionnel, `$0` = premier argument).
- ⚠️ **Valider la syntaxe exacte du frontmatter** (`argument-hint`, etc.) contre la version du CLI
  embarquée dans l'image toolbox avant de figer le format.

## 5. Backend

### 5.1 Génération — `app/services/session_skills.py` (calqué sur `app/services/mcp_config.py`)

- `skills_dir(session_id) -> str | None` : `<session_dir>/claude/skills/`, `None` hors sandbox.
- `slugify(name) -> str` : kebab-case, minuscules, caractères sûrs pour un nom de dossier/skill.
- `render_skill_md(t: PromptTemplate) -> str` : frontmatter + corps (voir §4.2).
- `apply_session_skills(session_id, templates) -> None` : **réconciliation desired-state** du dossier
  `skills/` (entièrement géré par Inkwest dans la sandbox) :
  - écrire `skills/<slug>/SKILL.md` pour chaque template actif ;
  - **supprimer** les sous-dossiers `skills/<slug>/` orphelins (template renommé/supprimé/désactivé) ;
  - écriture atomique (`mkstemp` + `os.replace`, mode lisible par l'uid du toolbox), idempotente
    (no-op si rien n'a changé), comme `_write_config_atomic` dans `mcp_config.py`.

### 5.2 Câblage — à chaque tour (`orchestrator_cli.py`)

Juste après l'appel existant à `apply_session_mcp_selection` (branche `settings.sandbox_enabled`) :

```python
templates = await get_active_prompt_templates(db)   # SELECT WHERE is_active
apply_session_skills(session_id_str, templates)
```

**Régénération par tour depuis la DB** : le CLI ne lit sa config qu'au lancement (= prochain tour). Donc
une création / édition / suppression de template est **automatiquement** prise en compte au prochain tour
de chaque session → **pas de sweep cross-session explicite nécessaire**. L'exigence « régénérer toutes les
sessions actives » du ticket est satisfaite, de façon plus robuste (survit aussi à un rebuild de sandbox
qui recrée un dossier `claude/` vide — même rationale que la réconciliation MCP per-turn).

### 5.3 Message verbatim + contexte runtime dans le system prompt

Pour qu'une `/skill` s'expanse **n'importe où** dans le message, l'orchestrateur envoie le message
utilisateur **verbatim** à `claude -p` (Claude Code résout la slash command depuis la chaîne du prompt). Le
contexte runtime — `kube_section` (accès cluster) + hint connecteurs + la consigne « investigate using the
tools / you are NOT on the target system » — est déplacé dans `--append-system-prompt`, **pas** dans le tour
user. Le contexte est ainsi présent à **tous** les tours sans risquer de polluer `$ARGUMENTS`, et le
special-case `is_slash_command` est supprimé.

> Ancien design abandonné : envelopper le message puis ne le passer brut que s'il **commence** par `/`. Trop
> restrictif (une skill en milieu de message n'aurait pas été reconnue) et le wrapper aurait été capturé
> comme arguments.

⚠️ **Frontière de `$ARGUMENTS` = le reste de la ligne** (comportement Claude Code). Une skill est donc
« propre » en **fin de ligne** ou sur sa propre ligne ; `check /diagnose-pod nginx puis résume` capturerait
`nginx puis résume` comme arguments.

### 5.4 Données / API — `slug`

**Choix MR-A : slug calculé, pas de colonne DB.** `slug = slugify(name)` — fonction pure dans
`app/core/slug.py` (strip accents → ASCII, kebab-case, jamais vide), exposée en lecture seule via un
`@model_validator(mode="after")` sur `PromptTemplateResponse` (`app/models/schemas.py`), donc présente dans
`GET /prompt-templates`. La **même** fonction sert au générateur de skills (`session_skills.slugify`) → front
et back dérivent le même slug, **sans migration**.

- Collisions (deux `name` distincts → même slug ; rare vu l'unicité de `name`) : loggées (`skill_slug_collision`)
  et la 2ᵉ skill est ignorée à la génération.
- Évolution possible si unicité forte requise en base : colonne `slug` + migration dbmate
  (`backend/migrations/`, sections `-- migrate:up/down`, schéma `inkwest`).
- **`argument_hint` encodé dans le nom** (pas de colonne) : une signature `(...)` en fin de nom est
  parsée par `parse_skill_name(name) -> (slug, argument_hint)` (`app/core/slug.py`), parse **avant**
  slugify pour que les parenthèses ne fuient pas dans le slug. Ex. `Summarize MR(merge request URL, ticket)`
  → slug `summarize-mr`, hint `merge request URL, ticket`. `argument_hint` est aussi un `@computed_field`
  de `PromptTemplateResponse` (comme `slug`), et alimente le frontmatter `argument-hint` du `SKILL.md`.

## 6. Frontend

### 6.1 Autocomplétion inline `/` — `ChatInput.tsx`

- Détecter le **token sous le curseur** commençant par `/` (slash en début de message OU après un espace) →
  **dropdown** ancré au-dessus du champ. Marche **n'importe où** dans le texte, pas seulement en début.
- **Hint d'arguments** : le `placeholder` natif ne s'affiche que sur champ vide → dès qu'un `/<slug>` est
  inséré il disparaît. Quand le message commence par un `/<slug>` connu (et que le dropdown est fermé),
  afficher sous le composer une ligne discrète : `/<slug> [arg1] [arg2] · <description>` (les arguments en
  crochets juste après le slug, comme dans le dropdown ; `formatArgs` split le hint sur les virgules).
  Fallback `arguments libres` si le template n'a pas de signature `(...)`. `argument_hint` vient de l'API
  (computed, parsé du nom — voir §5.4).
- Source : `usePromptTemplatesStore().templates`, filtré (sur `slug` / `name` / `description`), **chargé au
  montage** du composant (aujourd'hui `fetchTemplates` n'est appelé qu'à l'ouverture de la palette).
- Clavier : ↑/↓ (déplacer), **Enter (sélectionne — n'envoie PAS** tant que le dropdown est ouvert ;
  cohabiter avec le `handleKeyDown` Enter=envoyer existant), Esc (fermer), Tab (compléter).
- Sélection → remplace le token par `/<slug> `, curseur après l'espace, focus conservé → l'utilisateur tape
  les arguments.
- Nouveau composant de présentation `SkillAutocomplete.tsx` pour ne pas alourdir `ChatInput`.

### 6.2 Palette → trigger — `PromptTemplatePalette.tsx`

- `onSelect` passe de `content` à `slug` ; `handleSelectTemplate` insère `/<slug> ` au lieu de coller le
  contenu brut.
- Affichage d'un template : `name` + `description` (seul texte lisible).
- L'historique de conversation affichera `/<slug> <args>` comme message utilisateur (comme Claude Code).

## 7. Tests

- **Backend** : `render_skill_md` (frontmatter `name`/`description`, `$ARGUMENTS`, **absence** du flag
  `disable-model-invocation`), `apply_session_skills` (écrit le set, prune les orphelins, idempotent, no-op
  hors sandbox), `slugify` + collisions, passthrough (`/x` → prompt brut ; texte normal → wrapper), corps
  tolérant à `$ARGUMENTS` vide.
- **Frontend** : autocomplétion (`/` ouvre le dropdown, filtre, nav clavier, Enter sélectionne **sans**
  envoyer, insère `/<slug> `), envoi de `/<slug> <args>`, palette qui déclenche au lieu de coller.

## 8. Séquencement (3 MR)

1. **MR-A — backend** : `slug` + migration → génération `skills/<slug>/SKILL.md` → passthrough slash +
   tests. Livrable et testable indépendamment via un `/<slug>` tapé à la main.
2. **MR-B — frontend** : autocomplétion + palette + tests.
3. **MR-C — docs** : cette spec + finitions.

## 9. Points ouverts / risques

- **Contexte runtime sur tour slash** (§5.3) : system prompt (reco) vs perte en v1.
- **Migration `slug`** : confirmer l'outil de migration.
- **Version du CLI** dans l'image toolbox : valider `$ARGUMENTS` / `argument-hint` / frontmatter réellement
  supportés.
- **Dépendance sandbox** (sandbox + montage `.claude`) : confirmer l'état.
- **Qualité de l'auto-invocation = qualité des `description`** : guidage à l'écriture côté admin.

## 10. Références code

| Sujet | Emplacement |
|---|---|
| Modèle | `backend/app/models/prompt_template.py` (`PromptTemplate`) |
| CRUD + schémas | `backend/app/api/routes/prompt_templates.py`, `backend/app/models/schemas.py` |
| Pattern de réconciliation | `backend/app/services/mcp_config.py` (`apply_session_mcp_selection`, `_write_config_atomic`) |
| Orchestrateur CLI | `backend/app/services/orchestrator_cli.py` (wrapping du prompt ; appel `apply_session_mcp_selection`) |
| Invocation CLI | `backend/app/services/ai_cli_client.py` (`claude -p`, `--append-system-prompt`, `--resume`) |
| Sandbox | `backend/app/services/sandbox.py` (montage `.claude`, `CLAUDE_CONFIG_DIR`, `session_dir`) |
| Front chat | `frontend/src/components/chat/ChatInput.tsx`, `PromptTemplatePalette.tsx` |
| Front store / admin | `frontend/src/store/promptTemplates.ts`, `frontend/src/pages/admin/PromptTemplatesPage.tsx` |
