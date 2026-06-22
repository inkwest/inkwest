import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getLoginUrl } from '@/services/adminApi';

export function AuthCallbackPage() {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Success sets an HttpOnly cookie server-side and redirects straight to '/',
    // so this page only ever handles the error fragment. Anything else → home.
    const params = new URLSearchParams(window.location.hash.substring(1));
    const err = params.get('error');

    if (err) {
      setError(decodeURIComponent(err));
    } else {
      navigate('/', { replace: true });
    }
  }, [navigate]);

  if (!error) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <p className="text-muted-foreground">Signing in…</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-screen gap-4">
      <p className="text-destructive">Login failed: {error}</p>
      <a href={getLoginUrl()} className="text-primary underline">
        Try again
      </a>
    </div>
  );
}
