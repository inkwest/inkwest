-- migrate:up

-- prompt templates are now surfaced as Claude skills (triggered via
-- /<slug>); rename the table + its index to match the domain. Data preserved.
ALTER TABLE inkwest.prompt_templates RENAME TO skills;
ALTER INDEX inkwest.idx_prompt_templates_active RENAME TO idx_skills_active;

-- migrate:down

ALTER INDEX inkwest.idx_skills_active RENAME TO idx_prompt_templates_active;
ALTER TABLE inkwest.skills RENAME TO prompt_templates;
