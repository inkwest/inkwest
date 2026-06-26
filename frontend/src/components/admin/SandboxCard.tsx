import { Box } from 'lucide-react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui';
import { SandboxLimitsSection } from './SandboxLimitsSection';
import { RunningSandboxesSection } from './RunningSandboxesSection';

// One Sandbox section combining the runtime limits and the live running-sandboxes list,
// so the two closely-related concerns live under a single header on the System Info page.
export function SandboxCard() {
  return (
    <Card variant="bordered">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Box className="w-5 h-5 text-blue-400" />
          Sandbox
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-6 divide-y divide-gray-800 [&>*:not(:first-child)]:pt-6">
        <SandboxLimitsSection />
        <RunningSandboxesSection />
      </CardContent>
    </Card>
  );
}
