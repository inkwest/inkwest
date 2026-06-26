import { useEffect } from 'react';
import { SkillsManager } from '@/components/skills/SkillsManager';
import { useSkillStore } from '@/store/admin';
import type { CreateSkillRequest } from '@/services/adminApi';

export function SkillsPage() {
  const {
    templates,
    isLoading,
    error,
    fetchTemplates,
    createTemplate,
    updateTemplate,
    deleteTemplate,
    clearError,
  } = useSkillStore();

  useEffect(() => {
    fetchTemplates(false); // include inactive
  }, [fetchTemplates]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Skills</h1>
        <p className="text-gray-400 mt-1">
          Manage skills — reusable prompts triggered via /&lt;slug&gt; in the chat (or
          auto-invoked by the agent)
        </p>
      </div>

      <SkillsManager
        skills={templates}
        isLoading={isLoading}
        error={error}
        clearError={clearError}
        onCreate={async (content) => {
          await createTemplate({ content } as CreateSkillRequest);
        }}
        onUpdate={async (id, data) => {
          await updateTemplate(id, data);
        }}
        onDelete={(id) => deleteTemplate(id)}
        emptyHint='No skills configured. Click "Add Skill" to create one.'
      />
    </div>
  );
}
